"""The gap audit is the "open issues" signal. If it lies, we email people about
problems they do not have, which is the fastest way to burn a domain's reputation.
These tests pin the checks to concrete HTML, not to a network.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import audit
from app.db import connect, init_db, new_id, utcnow
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)


@pytest.fixture(autouse=True)
def _schema():
    init_db()
    yield

GOOD = """<html><head><title>Lowell Excavating</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Earthwork in Lowell, Ohio">
<script type="application/ld+json">{"@type":"LocalBusiness"}</script>
<script async src="https://www.googletagmanager.com/gtag/js"></script>
</head><body><form action="/quote"><input name="email"></form>
<p>&copy; 2026 Lowell Excavating</p></body></html>"""

BAD = """<html><head><title></title></head><body>
<table width="900"><tr><td>Welcome to our website</td></tr></table>
<p>&copy; 2014 Some Old Company</p><a href="/about">About</a></body></html>"""


def _page(url="https://x.com", status=200, text="", elapsed=0.4, headers=None, error=None):
    return audit.Page(url=url, status=status, text=text, elapsed=elapsed,
                      headers=headers or {}, error=error)


def _zone(pages: dict[str, audit.Page]):
    def _fetch(url: str) -> audit.Page:
        for suffix, page in pages.items():
            if url.endswith(suffix):
                return page
        return _page(url=url, status=404, text="", error=None)

    return _fetch


# --- pure checks ----------------------------------------------------------

def test_a_healthy_site_has_no_findings():
    pages = {"https://x.com": _page(text=GOOD),
             "/robots.txt": _page(text="User-agent: *\nSitemap: https://x.com/sitemap.xml"),
             "/sitemap.xml": _page(text="<urlset/>")}
    assert audit.audit_page(pages["https://x.com"], None, {
        "robots": pages["/robots.txt"], "sitemap": pages["/sitemap.xml"]}) == []


def test_broken_site_flags_the_things_we_can_sell():
    home = _page(text=BAD)
    findings = audit.audit_page(home, None, {"sitemap": _page(status=404)})
    keys = {f["key"] for f in findings}
    assert {"no_viewport", "no_contact_form", "stale_site", "no_meta",
            "no_schema", "no_analytics", "no_sitemap"} <= keys
    stale = next(f for f in findings if f["key"] == "stale_site")
    assert "2014" in stale["evidence"]  # evidence, not a verdict


def test_one_finding_per_problem_no_double_counting():
    findings = audit.audit_page(_page(text=BAD), None, {})
    keys = [f["key"] for f in findings]
    assert len(keys) == len(set(keys))


def test_https_missing_is_high_value_and_named():
    findings = audit.audit_page(_page(url="http://x.com", text=GOOD), None, {})
    https = next(f for f in findings if f["key"] == "no_https")
    assert https["severity"] == "high"
    assert "http://" in https["evidence"]


def test_unreachable_site_is_a_single_honest_finding():
    findings = audit.audit_page(_page(status=0, error="ConnectTimeout: timed out"), None, {})
    assert [f["key"] for f in findings] == ["site_down"]
    assert "timed out" in findings[0]["evidence"]


def test_copyright_current_year_is_not_stale():
    html = GOOD.replace("2026", "2026")
    findings = audit.audit_page(_page(text=html), None,
                                {"sitemap": _page(text="<urlset/>")}, now_year=2026)
    assert "stale_site" not in {f["key"] for f in findings}


# --- audit_site (fetch level) --------------------------------------------

def test_audit_site_scores_and_names_services():
    good = f"""<html><head><title>T</title><meta name="viewport" content="width=device-width">
    <meta name="description" content="d"><script type="application/ld+json">{{}}</script>
    <script src="https://www.googletagmanager.com/gtag/js"></script></head>
    <body><form></form>&copy; {utcnow()[:4]}</body></html>"""
    fetch = _zone({
        "https://ok-co.com": _page(url="https://ok-co.com", text=good),
        "/robots.txt": _page(text="Sitemap: https://ok-co.com/sitemap.xml"),
        "/sitemap.xml": _page(text="<urlset/>"),
    })
    res = audit.audit_site("ok-co.com", _fetch=fetch)
    assert res["site_ok"] is True
    assert res["score"] == 0 and res["services"] == []


def test_a_dead_https_tries_http_before_calling_it_dead():
    """Half the small-business sites we audit have no certificate at all. Calling
    them 'down' would be false, and would also hide the no_https finding."""
    bad = BAD
    fetch = _zone({
        "https://no-cert.com": _page(url="https://no-cert.com", status=0, error="SSLError: no cert"),
        "http://no-cert.com": _page(url="http://no-cert.com", text=bad),
        "/robots.txt": _page(status=404),
        "/sitemap.xml": _page(status=404),
    })
    res = audit.audit_site("no-cert.com", _fetch=fetch)
    keys = {f["key"] for f in res["findings"]}
    assert res["site_ok"] is True
    assert "no_https" in keys and "site_down" not in keys


def test_audit_normalises_a_pasted_url():
    seen: list[str] = []

    def _fetch(url: str) -> audit.Page:
        seen.append(url)
        return _page(url=url, text=GOOD)

    audit.audit_site("  HTTPS://Example.com/contact  ", _fetch=_fetch)
    assert seen[0] == "https://example.com"


def test_score_is_capped_and_ordered_by_weight():
    res = audit.audit_site("x.com", _fetch=_zone({"https://x.com": _page(text=BAD),
                                                  "/sitemap.xml": _page(status=404)}))
    weights = [f["weight"] for f in res["findings"]]
    assert weights == sorted(weights, reverse=True)
    assert 0 < res["score"] <= 100


# --- route + worker -------------------------------------------------------

def test_route_audits_a_domain(monkeypatch):
    monkeypatch.setattr(audit, "fetch", _zone({"https://x.com": _page(text=BAD)}))
    r = c.post("/api/audit/site", json={"domain": "x.com"})
    assert r.status_code == 200, r.text
    assert r.json()["score"] > 0


def test_route_persists_the_score_on_the_lead(monkeypatch):
    monkeypatch.setattr(audit, "fetch", _zone({"https://x.com": _page(text=BAD)}))
    conn = connect()
    try:
        ws_row = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
        if ws_row is None:
            ws = c.post("/api/workspaces", json={
                "name": "Audit Co", "slug": "audit-co",
                "physical_address": "100 Main St, Lowell, OH 45744"}).json()["id"]
        else:
            ws = ws_row["id"]
        client = new_id()
        conn.execute("INSERT INTO businesses (id, name, domain, source, created_at, updated_at)"
                     " VALUES (?,?,?,?,?,?)",
                     (client, "Client Co", "client-co.com", "manual", utcnow(), utcnow()))
        lead = new_id()
        conn.execute(
            "INSERT INTO research_leads (id, workspace_id, client_business_id, name, domain,"
            " signal_kind, signal_source, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (lead, ws, client, "X Co", "x.com", "news", "https://news.example/x", utcnow()))
        conn.commit()
    finally:
        conn.close()

    r = c.post("/api/research/leads/audit", json={"client_business_id": client, "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checked"] == 1 and body["best"]["score"] > 0

    conn = connect()
    try:
        row = conn.execute("SELECT audit_score, audit_detail, signal_kind FROM research_leads"
                           " WHERE id = ?", (lead,)).fetchone()
    finally:
        conn.close()
    assert row["audit_score"] > 0 and row["audit_detail"]
    # The audit adds evidence; it does not overwrite the signal that found the lead.
    assert row["signal_kind"] == "news"


def test_route_skips_already_audited_unless_rescored(monkeypatch):
    calls: list[str] = []

    def _fetch(url: str) -> audit.Page:
        calls.append(url)
        return _page(url=url, status=404, error="down")

    monkeypatch.setattr(audit, "fetch", _fetch)
    conn = connect()
    try:
        lead = conn.execute("SELECT id FROM research_leads WHERE audit_score IS NOT NULL"
                            " LIMIT 1").fetchone()
    finally:
        conn.close()
    if not lead:
        pytest.skip("no audited lead available")
    first = c.post("/api/research/leads/audit", json={"limit": 1})
    assert first.json()["checked"] == 0
    c.post("/api/research/leads/audit", json={"limit": 1, "rescore": True})
    assert calls  # rescore actually re-fetches


def test_worker_audit_hook_never_raises(monkeypatch):
    import app.routes_research as rr

    def _boom(*a, **k):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(rr.audit, "audit_site", _boom)
    assert rr.audit_pending(1) == 0
