"""Tests for domain research and grounded email drafting.

The expensive, flaky, and dangerous parts are all faked here: no network, no
model. What is asserted is the contract the UI depends on — refuse private
addresses, degrade to heuristics, and never produce an ungrounded email.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import ai, research
from app.db import init_db
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)

FIXTURE = """
<!doctype html><html><head>
<title>Buckeye Ridge Roofing | Columbus Roof Repair &amp; Replacement</title>
<meta name="description" content="Family owned roof repair and replacement in Columbus Ohio since 1998.">
<meta property="og:site_name" content="Buckeye Ridge Roofing">
</head><body>
<h1>Roofing done right the first time</h1>
<h2>Services</h2>
<ul>
  <li>Storm damage repair</li>
  <li>Gutter installation</li>
  <li>Free roof inspections</li>
  <li>Home</li>
  <li>Contact</li>
</ul>
<p>We serve homeowners in Columbus, OH and the surrounding counties.</p>
<a href="mailto:office@buckeyeridge.com">Email us</a>
<a href="tel:+16145550142">Call</a>
<a href="https://www.facebook.com/buckeyeridge">Facebook</a>
</body></html>
"""


# ------------------------------------------------------------------ url safety

@pytest.mark.parametrize("raw,url,domain", [
    ("buckeyeridge.com", "https://buckeyeridge.com", "buckeyeridge.com"),
    ("  www.acme.com/about/ ", "https://www.acme.com/about", "www.acme.com"),
    ("http://acme.com", "http://acme.com", "acme.com"),
])
def test_normalize_url_accepts_what_people_type(raw, url, domain):
    assert research.normalize_url(raw) == (url, domain)


@pytest.mark.parametrize("raw", ["", "   ", "not a domain.com", "ftp://acme.com", "localhost"])
def test_normalize_url_explains_bad_input(raw):
    with pytest.raises(research.ResearchError):
        research.normalize_url(raw)


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "10.0.0.5", "192.168.0.244", "169.254.169.254", "0.0.0.0",
])
def test_private_and_metadata_addresses_are_refused(host):
    """The container must never be usable as a proxy into the host LAN."""
    with pytest.raises(research.ResearchError):
        research._assert_public(host)


def test_private_host_rejected_before_any_socket_is_opened(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fetch opened a connection to a private host")

    monkeypatch.setattr(research.urllib.request, "urlopen", boom)
    with pytest.raises(research.ResearchError):
        research.fetch("http://192.168.0.1/")


# ------------------------------------------------------------------- extraction

def test_extract_pulls_the_facts_an_email_can_cite():
    f = research.extract(FIXTURE, "https://buckeyeridge.com")
    assert f["name"] == "Buckeye Ridge Roofing"
    assert "roof repair" in f["meta_description"].lower()
    assert "Storm damage repair" in f["services"]
    assert "Gutter installation" in f["services"]
    # navigation never leaks into the pitch
    assert "Home" not in f["services"]
    assert "Contact" not in f["services"]
    assert f["emails"] == ["office@buckeyeridge.com"]
    assert f["phones"] and "6145550142" in f["phones"][0]
    assert "Columbus, OH" in f["places"]
    assert "facebook.com" in f["socials"]


def test_research_degrades_to_heuristics_without_a_model(monkeypatch):
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    monkeypatch.setattr(research, "fetch", lambda url, *a, **k: (FIXTURE, url))
    p = research.research("buckeyeridge.com", deep=False)
    assert p["error"] is None
    assert p["used_llm"] is False
    assert p["business_name"] == "Buckeye Ridge Roofing"
    assert p["services"]
    assert p["emails"] == ["office@buckeyeridge.com"]
    assert p["fetched_at"]


def test_research_reports_remote_failure_instead_of_raising(monkeypatch):
    def dead(url, *a, **k):
        raise research.ResearchError("Could not load https://dead.example: timed out")

    monkeypatch.setattr(research, "fetch", dead)
    with pytest.raises(research.ResearchError):
        research.research("dead.example")


def test_parse_json_object_tolerates_fences_and_chatter():
    assert research._parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert research._parse_json_object('sure: {"a": {"b": 2}} done') == {"a": {"b": 2}}
    assert research._parse_json_object("no json here") is None
    assert research._parse_json_object("") is None


def test_brief_is_compact_and_citable():
    b = research.brief({
        "what_they_do": "Roof repair and replacement",
        "services": ["Storm damage", "Gutters", "Inspections", "Siding", "Skylights"],
        "service_area": "Columbus, OH",
        "talking_point": "family owned since 1998",
    })
    assert b.startswith("Roof repair and replacement")
    assert "Storm damage" in b and "Siding" in b
    assert "Skylights" not in b   # capped at four services
    assert "family owned since 1998" in b
    assert len(b) <= 600
    assert research.brief(None) == ""
    assert research.brief("{}") == ""


# ---------------------------------------------------------------- email drafting

def test_draft_email_falls_back_to_grounded_copy_without_a_model(monkeypatch):
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    d = ai.draft_email(
        {"offer": "We handle follow-up for roofers", "proof_points": '["Twelve roofers in Ohio"]'},
        {"name": "Buckeye Ridge Roofing", "city": "Columbus", "state": "OH"},
        {"full_name": "Dana Reed"},
        "Roof repair and replacement; services: storm damage, gutters",
        signature="Ryan",
    )
    assert d["used_llm"] is False
    assert d["grounded"] is True
    assert "Hi Dana," in d["body"]
    assert "Roof repair and replacement" in d["body"]   # cites their site
    assert "Twelve roofers in Ohio" in d["body"]
    assert d["body"].rstrip().endswith("Ryan")
    assert d["subject"] and len(d["subject"]) <= 160
    assert "!" not in d["body"]


def test_draft_email_scrubs_banned_phrases():
    d = ai.draft_email(
        {"offer": "We do things", "banned_phrases": '["synergy", "circle back"]'},
        {"name": "Acme"}, None, "they sell widgets", signature="Ryan",
    )
    assert "synergy" not in d["body"].lower()


def test_llm_health_reports_unconfigured_instead_of_pretending(monkeypatch):
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    h = ai.llm_health()
    assert h["configured"] is False and h["reachable"] is False
    assert "not set" in (h["error"] or "")


# ------------------------------------------------------------------------ API

_MADE = ("Buckeye Ridge Roofing", "Empty Offer LLC")


@pytest.fixture(scope="module", autouse=True)
def _db():
    """Leave the shared test database exactly as we found it.

    test_smoke asserts on absolute row counts, so pollution here shows up as a
    failure in an unrelated file — which is a bad way to learn about it.
    """
    init_db()
    yield
    from app.db import connect

    conn = connect()
    try:
        marks = ",".join("?" * len(_MADE))
        ids = [r["id"] for r in conn.execute(
            f"SELECT id FROM businesses WHERE name IN ({marks})", _MADE)]
        for biz_id in ids:
            conn.execute("DELETE FROM contacts WHERE business_id = ?", (biz_id,))
            conn.execute("DELETE FROM list_members WHERE business_id = ?", (biz_id,))
            conn.execute("DELETE FROM enrollments WHERE business_id = ?", (biz_id,))
            conn.execute("DELETE FROM messages WHERE business_id = ?", (biz_id,))
            conn.execute("DELETE FROM businesses WHERE id = ?", (biz_id,))
        conn.execute("DELETE FROM ai_profiles WHERE workspace_id IN "
                     "(SELECT id FROM workspaces WHERE slug IN ('research-co', 'no-profile-co'))")
        conn.execute("DELETE FROM workspaces WHERE slug IN ('research-co', 'no-profile-co')")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def ws() -> str:
    """One workspace for the API tests: import is global, lists are per-workspace."""
    return c.post("/api/workspaces", json={
        "name": "Research Co", "slug": "research-co", "physical_address": "1 Main St, Lisbon OH",
    }).json()["id"]


def _import(records: list[dict]) -> None:
    r = c.post("/api/businesses/import", json=records)
    assert r.status_code == 200, r.text


def test_status_endpoint_tells_the_ui_whether_ai_is_live():
    r = c.get("/api/research/status")
    assert r.status_code == 200
    body = r.json()
    assert "llm" in body and "configured" in body["llm"]
    assert "researched" in body and "businesses" in body


def test_research_endpoint_refuses_a_private_address():
    r = c.post("/api/research/domain", json={"url": "http://192.168.0.244:8080"})
    assert r.status_code in (403, 422)
    assert "private" in r.json()["detail"].lower()


def test_research_endpoint_requires_the_token():
    anon = TestClient(app)
    r = anon.post("/api/research/domain", json={"url": "example.com"})
    assert r.status_code == 401


def test_research_endpoint_returns_a_profile_and_caches_it(monkeypatch, ws):
    monkeypatch.setattr(research, "fetch", lambda url, *a, **k: (FIXTURE, url))
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    _import([{
        "name": "Buckeye Ridge Roofing", "category": "roofing", "city": "Columbus",
        "state": "OH", "website": "https://buckeyeridge.com", "source": "manual",
    }])
    biz_id = _find_business("Buckeye Ridge Roofing")
    r = c.post("/api/research/domain", json={"url": "buckeyeridge.com", "business_id": biz_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"]["business_name"] == "Buckeye Ridge Roofing"
    assert body["brief"]
    cached = c.get(f"/api/research/business/{biz_id}").json()
    assert cached["profile"]["domain"] == "buckeyeridge.com"
    assert cached["needs_research"] is False


def test_pulling_leads_uses_the_domain_as_a_seed(monkeypatch, ws):
    monkeypatch.setattr(research, "fetch", lambda url, *a, **k: (FIXTURE, url))
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    r = c.post("/api/research/leads", json={
        "workspace_id": ws, "url": "buckeyeridge.com", "limit": 5,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"]["domain"] == "buckeyeridge.com"
    assert body["filters"]["state"] == "OH"
    assert isinstance(body["results"], list)


def test_drafting_without_an_ai_profile_explains_the_fix():
    empty_ws = c.post("/api/workspaces", json={
        "name": "No Profile Co", "slug": "no-profile-co", "physical_address": "1 Main St, Lisbon OH",
    }).json()["id"]
    _import([{
        "name": "Empty Offer LLC", "category": "roofing", "city": "Columbus",
        "state": "OH", "website": "", "source": "manual",
    }])
    biz_id = _find_business("Empty Offer LLC")
    r = c.post("/api/research/email", json={"workspace_id": empty_ws, "business_id": biz_id})
    assert r.status_code == 422
    assert "Voice" in r.json()["detail"]


def test_site_contact_is_stored_unverified_so_it_cannot_send_yet(ws):
    biz_id = _find_business("Buckeye Ridge Roofing")
    r = c.post("/api/research/contact", json={
        "business_id": biz_id, "email": "Office@BuckeyeRidge.com", "full_name": "Dana Reed",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email_status"] == "unverified"


def _find_business(name: str) -> str:
    from app.db import connect

    conn = connect()
    try:
        row = conn.execute("SELECT id FROM businesses WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    assert row, f"{name} not imported"
    return row["id"]
