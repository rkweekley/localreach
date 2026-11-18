"""Provenance tests: a lead may only carry a domain we can prove, and a hint may
never carry an address.

These pin the failure that put ``legacy740.com`` on a Marietta gym: research was
told to read a source, and instead built a URL from the business name and a
mailbox from the URL. Everything here is offline - the site is handed in.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import leadguard
from app.db import connect, init_db
from app.main import app
from app.routes_research import _store_research_leads, discover_pending

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)

CHAMBER = ("https://business.mariettachamber.com/events/details/"
           "ribbon-cutting-legacy-740-604145")
GALLERY = ("<html><head><title>AD360</title></head><body><p>A sophisticated "
           "digital gallery designed for architects and creatives. Login "
           "Register</p></body></html>")


class Page:
    def __init__(self, text: str = "", ok: bool = True) -> None:
        self.text = text
        self.ok = ok


def fake_site(pages: dict[str, str]):
    """Fetch stand-in: only the given URLs resolve, and every call is recorded."""
    calls: list[str] = []

    def fetch(url: str):
        calls.append(url)
        if url in pages:
            return Page(pages[url])
        return Page(ok=False)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


@pytest.fixture(scope="module", autouse=True)
def _schema() -> None:
    init_db()


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("https://WWW.Example.com/about", "example.com"),
    ("example.com:8443/x", "example.com"),
    ("http://site.co.uk?q=1", "site.co.uk"),
    ("  Example.COM. ", "example.com"),
    ("not a domain", ""),
    ("localhost", ""),
    (None, ""),
])
def test_bare_host(raw, expected):
    assert leadguard.bare_host(raw) == expected


def test_hint_loses_the_address_but_keeps_the_phone():
    hint = ("General mailbox staff@legacy740.com and phone (740) 434-7613 "
            "listed on their contact page")
    text, dropped = leadguard.sanitize_hint(hint)
    assert dropped == ["staff@legacy740.com"]
    assert "@" not in text
    assert "(740) 434-7613" in text
    assert "contact page" in text
    assert len(text) <= 300


def test_hint_without_address_is_left_alone():
    text, dropped = leadguard.sanitize_hint("Ask for Dana at the front desk")
    assert dropped == []
    assert text == "Ask for Dana at the front desk"


# --------------------------------------------------------------------------- #
# check_lead - the domain has to be legible as this business
# --------------------------------------------------------------------------- #
def test_name_derived_domain_is_dropped():
    """The production failure, replayed: chamber page prints no URL, and the
    domain that was stored serves an unrelated gallery."""
    fetch = fake_site({
        CHAMBER: ("Ribbon Cutting - Legacy 740. Join us Dec 3 at 220 3rd St "
                  "for the opening of a 24 hour gym."),
        "https://legacy740.com": GALLERY,
    })
    verdict = leadguard.check_lead(
        {"name": "Legacy 740", "domain": "legacy740.com", "signal_source": CHAMBER},
        locality="Marietta", _fetch=fetch)
    assert verdict["domain"] == ""
    assert "dropped legacy740.com" in verdict["note"]
    assert "digital gallery" in verdict["note"]


def test_domain_printed_on_source_needs_no_site_read():
    fetch = fake_site({CHAMBER: "Legacy 740 - see https://legacy740.com for hours"})
    verdict = leadguard.check_lead(
        {"name": "Legacy 740", "domain": "legacy740.com", "signal_source": CHAMBER},
        _fetch=fetch)
    assert verdict["domain"] == "legacy740.com"
    assert "printed on" in verdict["note"]
    assert "https://legacy740.com" not in fetch.calls  # no need to crawl


def test_two_distinctive_words_on_the_site_confirm_it():
    fetch = fake_site({
        "https://bakernbaker.com": ("<h1>Baker &amp; Baker Jewelers</h1>"
                                    "<p>Downtown since 1948.</p>"),
    })
    verdict = leadguard.check_lead(
        {"name": "Baker & Baker Jewelers", "domain": "bakernbaker.com"},
        _fetch=fetch)
    assert verdict["domain"] == "bakernbaker.com"
    assert "names baker, jewelers" in verdict["note"]


def test_one_word_needs_the_town():
    site = "https://legacy740.com"
    ok = fake_site({site: "<p>Legacy - a gym in Marietta, Ohio.</p>"})
    assert leadguard.check_lead({"name": "Legacy 740", "domain": site},
                                locality="Marietta", _fetch=ok)["domain"] == "legacy740.com"

    # Same page, no town: one strong word is not enough to rule out a namesake.
    thin = fake_site({site: "<p>Legacy - a gym.</p>"})
    assert leadguard.check_lead({"name": "Legacy 740", "domain": site},
                                locality="Marietta", _fetch=thin)["domain"] == ""


def test_unreachable_site_drops_the_domain():
    verdict = leadguard.check_lead({"name": "Legacy 740", "domain": "legacy740.com"},
                                   _fetch=fake_site({}))
    assert verdict["domain"] == ""
    assert "unreachable" in verdict["note"]


def test_lead_without_a_domain_is_a_no_op():
    fetch = fake_site({})
    assert leadguard.check_lead({"name": "X", "domain": ""}, _fetch=fetch) == {
        "domain": "", "note": ""}
    assert fetch.calls == []


# --------------------------------------------------------------------------- #
# the write point: the guard runs, the note lands, a cleared domain clears
# --------------------------------------------------------------------------- #
SLUG = "leadguard-co"


@pytest.fixture(scope="module")
def workspace() -> str:
    return c.post("/api/workspaces", json={
        "name": "Leadguard Co", "slug": SLUG, "physical_address": "1 Main St, Marietta OH",
    }).json()["id"]


def _client_business(ws: str) -> str:
    conn = connect()
    try:
        bid = "lgclient" + ws[:8]
        conn.execute(
            """INSERT OR REPLACE INTO businesses (id, name, city, state, domain,
                   source, created_at, updated_at)
               VALUES (?,?,?,?,?,'manual',?,?)""",
            (bid, "Cyberal Solutions", "Marietta", "OH", "cyberalsolutions.com",
             "2026-11-17", "2026-11-17"))
        conn.commit()
        return bid
    finally:
        conn.close()


def _lead_row(lead_name: str, client_bid: str):
    conn = connect()
    try:
        return conn.execute(
            "SELECT domain, contact_hint, evidence_note FROM research_leads"
            " WHERE client_business_id = ? AND lower(name) = lower(?)",
            (client_bid, lead_name)).fetchone()
    finally:
        conn.close()


def _cleanup(conn, ws: str, client_bid: str) -> None:
    """Drop everything these tests created, children before parents."""
    conn.execute("DELETE FROM client_leads WHERE workspace_id = ?", (ws,))
    conn.execute("DELETE FROM list_members WHERE list_id IN"
                 " (SELECT id FROM lists WHERE workspace_id = ?)", (ws,))
    conn.execute("DELETE FROM research_leads WHERE client_business_id = ?", (client_bid,))
    conn.execute("DELETE FROM lists WHERE workspace_id = ?", (ws,))
    conn.execute("DELETE FROM workspace_businesses WHERE workspace_id = ?", (ws,))
    conn.execute("DELETE FROM businesses WHERE source = 'agent_lead'")
    conn.execute("DELETE FROM businesses WHERE id = ?", (client_bid,))
    conn.commit()


def test_store_drops_unproven_domain_and_sanitises_hint(workspace):
    client_bid = _client_business(workspace)
    seen = {}

    def guard(lead, locality=None):
        seen["locality"] = locality
        return {"domain": "", "note": "dropped legacy740.com: site does not name this business"}

    conn = connect()
    try:
        summary = _store_research_leads(conn, workspace, None, client_bid, {"leads": [{
            "name": "Legacy 740",
            "domain": "legacy740.com",
            "signal_kind": "opening",
            "signal_source": CHAMBER,
            "contact_hint": "General mailbox staff@legacy740.com and phone (740) 434-7613",
            "fit": 88,
        }]}, guard=guard)
        conn.commit()
        assert summary["created"] == 1
        row = _lead_row("Legacy 740", client_bid)
        assert row["domain"] is None, "an unproven domain must not be stored"
        assert "@" not in (row["contact_hint"] or "")
        assert "1 address(es) removed from hint" in (row["evidence_note"] or "")
        assert "does not name this business" in (row["evidence_note"] or "")
        assert seen["locality"] == "Marietta", "guard needs the client's town as tie-breaker"
    finally:
        _cleanup(conn, workspace, client_bid)
        conn.close()


def test_a_cleared_domain_clears_on_the_second_run(workspace):
    """Re-research of the same company must not leave the old guess in place."""
    client_bid = _client_business(workspace)
    lead = {"name": "Legacy 740", "domain": "legacy740.com", "signal_kind": "opening",
            "signal_source": CHAMBER, "contact_hint": "no mailbox listed", "fit": 80}
    conn = connect()
    try:
        _store_research_leads(conn, workspace, None, client_bid, {"leads": [lead]})
        conn.commit()
        assert _lead_row("Legacy 740", client_bid)["domain"] == "legacy740.com"

        _store_research_leads(conn, workspace, None, client_bid, {"leads": [lead]},
                              guard=lambda l, locality=None: {"domain": "", "note": "dropped: namesake"})
        conn.commit()
        row = _lead_row("Legacy 740", client_bid)
        assert row["domain"] is None
        assert "namesake" in (row["evidence_note"] or "")
    finally:
        _cleanup(conn, workspace, client_bid)
        conn.close()


# --------------------------------------------------------------------------- #
# the worker must not hammer one dead site forever
# --------------------------------------------------------------------------- #
def test_discover_pending_skips_recent_attempts_and_never_drops_a_lead(monkeypatch, workspace):
    ids, biz_ids = [], []
    conn = connect()
    try:
        for i, attempted in enumerate([None, "2026-01-01T00:00:00+00:00",
                                       "2999-01-01T00:00:00+00:00"]):
            lid, bid = f"dspend{i}", f"dsbiz{i}"
            ids.append(lid)
            biz_ids.append(bid)
            conn.execute("INSERT INTO businesses (id, name, source, created_at, updated_at)"
                         " VALUES (?,?,'manual',?,?)", (bid, f"Pending {i}", "2026-01-01", "2026-01-01"))
            conn.execute(
                """INSERT INTO research_leads (id, workspace_id, client_business_id, name,
                       domain, signal_kind, signal_source, fit, created_at, discover_attempted_at)
                   VALUES (?, ?, ?, ?, ?, 'other', 'src', 10, ?, ?)""",
                (lid, workspace, bid, f"Pending {i}", f"pending{i}.com", f"2026-01-0{i + 1}", attempted))
        conn.commit()
    finally:
        conn.close()

    tried: list[str] = []
    monkeypatch.setattr("app.routes_research.discover_for_lead",
                        lambda conn, lead, fetch=None: tried.append(lead["id"]) or {})
    try:
        discover_pending(limit=2, retry_after_days=14)
        # never-tried first, then the oldest attempt; the future-dated row is skipped
        assert tried == ["dspend0", "dspend1"]
    finally:
        conn = connect()
        conn.execute("DELETE FROM research_leads WHERE id IN (?,?,?)", tuple(ids))
        conn.execute("DELETE FROM businesses WHERE id IN (?,?,?)", tuple(biz_ids))
        conn.commit()
        conn.close()
