"""Contact discovery: the rule is that every address came off a page the prospect
published. If this module ever starts guessing `first.last@domain`, a 9% bounce
rate follows and the sending domain gets suspended. So the tests here are mostly
about what must be *rejected*, and about the path from "scraped address" to a
contact row that the enrollment gate will actually accept.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import audit, discover, verify
from app.db import connect, init_db, new_id, utcnow
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)


@pytest.fixture(autouse=True)
def _schema():
    init_db()
    yield


def _page(url="https://x.com", status=200, text="", error=None):
    return audit.Page(url=url, status=status, text=text, elapsed=0.3,
                      headers={"content-type": "text/html"}, error=error)


def _site(pages: dict[str, str]):
    def _fetch(url: str) -> audit.Page:
        for suffix, html in pages.items():
            if url.endswith(suffix):
                return _page(url=url, text=html)
        return _page(url=url, status=404, text="")

    return _fetch


# --- extraction -----------------------------------------------------------

def test_mailto_and_plain_text_are_both_found():
    html = ('<a href="mailto:owner@acme-dirt.com?subject=Quote">Email</a>'
            '<p>Or write to office@acme-dirt.com</p>')
    got = [e for e, _ in discover.extract_emails(html, "https://acme-dirt.com/contact")]
    assert got == ["owner@acme-dirt.com", "office@acme-dirt.com"]


def test_sprite_and_script_noise_is_rejected():
    html = ('<img src="logo@2x.png"><img src="hero@3x.jpg">'
            '<script src="app@v2.js"></script><p>sentry@wixpress.com</p>'
            '<p>you@example.com</p><p>no-reply@acme-dirt.com</p>'
            '<p>abuse@acme-dirt.com</p><p>real@acme-dirt.com</p>')
    got = [e for e, _ in discover.extract_emails(html, "https://acme-dirt.com")]
    assert got == ["real@acme-dirt.com"]


def test_duplicates_collapse_and_case_is_normalised():
    html = ('<a href="mailto:Owner@Acme-Dirt.com">a</a>'
            '<a href="mailto:owner@acme-dirt.com">b</a>'
            '<p>OWNER@ACME-DIRT.COM</p>')
    got = [e for e, _ in discover.extract_emails(html, "https://acme-dirt.com")]
    assert got == ["owner@acme-dirt.com"]


def test_every_address_carries_the_url_it_came_from():
    """The evidence is the point. A contact nobody can source is a contact nobody
    can defend when the recipient says 'I never gave you this address'."""
    got = discover.extract_emails("<p>x@acme-dirt.com</p>", "https://acme-dirt.com/about")
    assert got == [("x@acme-dirt.com", "https://acme-dirt.com/about")]


# --- ordering -------------------------------------------------------------

def test_personal_beats_role_and_contact_page_beats_about():
    fetch = _site({
        "https://acme-dirt.com/about": "<p>info@acme-dirt.com</p>",
        "https://acme-dirt.com/contact": "<a href='mailto:info@acme-dirt.com'>i</a>"
                                         "<a href='mailto:ryan@acme-dirt.com'>r</a>",
    })
    res = discover.discover("acme-dirt.com", _fetch=fetch)
    assert res["candidates"][0]["email"] == "ryan@acme-dirt.com"
    assert res["candidates"][0]["is_role"] is False
    # The role mailbox is still returned — it is real, and a human may decide.
    assert any(x["is_role"] for x in res["candidates"])


def test_a_gmail_address_ranks_below_the_company_domain():
    fetch = _site({"https://acme-dirt.com/contact":
                   "<p>acme.dirt.llc@gmail.com</p><p>ryan@acme-dirt.com</p>"})
    res = discover.discover("acme-dirt.com", _fetch=fetch)
    assert res["candidates"][0]["email"] == "ryan@acme-dirt.com"


def test_no_page_no_candidates_no_guesses():
    res = discover.discover("dead-site.com", _fetch=_site({}))
    assert res["candidates"] == []
    assert res["pages_checked"] == []


def test_domain_input_is_normalised():
    seen: list[str] = []

    def _fetch(url: str) -> audit.Page:
        seen.append(url)
        return _page(url=url, text="<p>a@x.com</p>")

    discover.discover("  HTTPS://X.com/contact ", _fetch=_fetch)
    assert seen[0] == "https://x.com"


# --- lead -> contact ------------------------------------------------------

def _seed_lead(domain: str = "acme-dirt.com") -> str:
    conn = connect()
    try:
        ws_row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        if ws_row is None:
            ws = c.post("/api/workspaces", json={
                "name": "Disc Co", "slug": "disc-co",
                "physical_address": "1 Main St, Lowell, OH 45744"}).json()["id"]
        else:
            ws = ws_row["id"]
        client = new_id()
        conn.execute("INSERT INTO businesses (id, name, domain, source, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?)",
                     (client, "Client Co", "client-co.com", "manual", utcnow(), utcnow()))
        bid = new_id()
        conn.execute("INSERT INTO businesses (id, name, domain, source, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?)",
                     (bid, "Acme Dirt", domain, "manual", utcnow(), utcnow()))
        lead = new_id()
        conn.execute(
            "INSERT INTO research_leads (id, workspace_id, client_business_id, business_id,"
            " name, domain, signal_kind, signal_source, fit, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (lead, ws, client, bid, "Acme Dirt", domain, "hiring",
             "https://news.example/acme", 80, utcnow()))
        conn.commit()
        return lead
    finally:
        conn.close()


def test_discovery_writes_a_contact_the_enroll_gate_can_use(monkeypatch):
    lead_id = _seed_lead("acme-dirt.com")
    monkeypatch.setattr(discover, "fetch", _site({
        "https://acme-dirt.com/contact": "<a href='mailto:ryan@acme-dirt.com'>r</a>"}))
    monkeypatch.setattr(verify, "verify_address",
                        lambda email, source="website": {"status": "verified",
                                                         "reason": "mx_ok",
                                                         "detail": {"mx": ["mx.example"]}})

    r = c.post("/api/research/leads/discover", json={"limit": 5})
    assert r.status_code == 200, r.text
    assert r.json()["contacts_found"] >= 1

    conn = connect()
    try:
        row = conn.execute("SELECT business_id, email, email_status, email_source, is_primary"
                           " FROM contacts WHERE email = 'ryan@acme-dirt.com'").fetchone()
        lead = conn.execute("SELECT business_id FROM research_leads WHERE id = ?",
                            (lead_id,)).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["email_status"] == "verified"
    assert row["email_source"] == "website"
    # Attached to the prospect, not to the researched business.
    assert row["business_id"] == lead["business_id"]


def test_a_dns_blip_stores_the_address_as_unverified_not_invalid(monkeypatch):
    _seed_lead("blip-co.com")
    monkeypatch.setattr(discover, "fetch", _site({
        "https://blip-co.com/contact": "<p>owner@blip-co.com</p>"}))
    monkeypatch.setattr(verify, "verify_address",
                        lambda email, source="website": {"status": "unverified",
                                                         "reason": "dns_unavailable",
                                                         "detail": {}})
    c.post("/api/research/leads/discover", json={"limit": 5})
    conn = connect()
    try:
        row = conn.execute("SELECT email_status, verify_detail FROM contacts"
                           " WHERE email = 'owner@blip-co.com'").fetchone()
    finally:
        conn.close()
    assert row["email_status"] == "unverified"
    assert "dns_unavailable" in row["verify_detail"]


def test_lead_with_no_published_address_is_reported_not_faked(monkeypatch):
    _seed_lead("nopage-co.com")
    monkeypatch.setattr(discover, "fetch", _site({}))
    body = c.post("/api/research/leads/discover", json={"limit": 5}).json()
    reasons = {r.get("reason") for r in body["results"]}
    assert "no_address_published" in reasons
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) n FROM contacts WHERE email LIKE '%nopage-co.com'"
                         ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 0


def test_the_same_address_is_never_inserted_twice(monkeypatch):
    _seed_lead("dup-co.com")
    monkeypatch.setattr(discover, "fetch", _site({
        "https://dup-co.com/contact": "<p>owner@dup-co.com</p>"}))
    monkeypatch.setattr(verify, "verify_address",
                        lambda email, source="website": {"status": "verified",
                                                         "reason": "mx_ok", "detail": {}})
    c.post("/api/research/leads/discover", json={"limit": 5, "rediscovar": True})
    c.post("/api/research/leads/discover", json={"limit": 5, "rediscovar": True})
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) n FROM contacts WHERE email = 'owner@dup-co.com'"
                         ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_discovery_skips_leads_that_already_have_a_contact(monkeypatch):
    """Without `rediscovar`, a lead we already have an address for must not be
    re-scraped — that is how a tool gets itself rate-limited by its own prospects."""
    lead_id = _seed_lead("hascontact-co.com")
    conn = connect()
    try:
        bid = conn.execute("SELECT business_id FROM research_leads WHERE id = ?",
                           (lead_id,)).fetchone()["business_id"]
        conn.execute("INSERT INTO contacts (id, business_id, email, email_status, created_at)"
                     " VALUES (?,?,?,?,?)",
                     (new_id(), bid, "known@hascontact-co.com", "verified", utcnow()))
        conn.commit()
    finally:
        conn.close()

    calls: list[str] = []

    def _fake(domain, _fetch=None):
        calls.append(domain)
        return {"domain": domain, "pages_checked": [], "candidates": []}

    monkeypatch.setattr(discover, "discover", _fake)
    c.post("/api/research/leads/discover", json={"limit": 5})
    assert "hascontact-co.com" not in calls
