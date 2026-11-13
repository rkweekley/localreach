"""Contract tests for the Hermes research queue.

The queue is how the dashboard uses the agent without opening a port: the UI
enqueues a domain, the worker claims it, and the worker's result lands as a real
lead plus a grounded draft. These tests pin the parts that would silently rot -
idempotent enqueue, atomic claim, one lead per domain, and contacts that stay
unverified.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)


@pytest.fixture(scope="module", autouse=True)
def _schema() -> None:
    """The queue table is new; make sure the test database has it."""
    init_db()

SLUG = "queue-co"
PROFILE = {
    "business_name": "Buckeye Ridge Roofing",
    "what_they_do": "Roof repair and replacement for Columbus homeowners.",
    "industry": "Roofing",
    "customers": "Homeowners",
    "service_area": "Columbus, OH",
    "talking_point": "Family owned since 1998.",
    "services": ["Storm damage repair", "Gutter installation"],
    "differentiators": ["Family owned"],
    "emails": ["office@buckeyeridge.com"],
    "phones": ["+161****0142"],
    "url": "https://buckeyeridge.com",
    "domain": "buckeyeridge.com",
}
DRAFT = {"subject": "Roof inspections in Columbus", "body": "Short grounded note.", "grounded_on": "Storm damage repair"}


@pytest.fixture(scope="module")
def ws() -> str:
    rid = c.post("/api/workspaces", json={
        "name": "Queue Co", "slug": SLUG, "physical_address": "1 Main St, Lisbon OH",
    }).json()["id"]
    yield rid
    conn = connect()
    try:
        conn.execute("DELETE FROM research_leads WHERE workspace_id = ?", (rid,))
        conn.execute("DELETE FROM research_queue WHERE workspace_id = ?", (rid,))
        conn.execute("DELETE FROM list_members WHERE list_id IN (SELECT id FROM lists WHERE workspace_id = ?)", (rid,))
        conn.execute("DELETE FROM lists WHERE workspace_id = ?", (rid,))
        conn.execute("DELETE FROM businesses WHERE source = 'agent_lead'")
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM businesses WHERE domain = ?", (PROFILE["domain"],))]
        for biz_id in ids:
            conn.execute("DELETE FROM contacts WHERE business_id = ?", (biz_id,))
            conn.execute("DELETE FROM list_members WHERE business_id = ?", (biz_id,))
            conn.execute("DELETE FROM businesses WHERE id = ?", (biz_id,))
        conn.execute("DELETE FROM lists WHERE workspace_id = ?", (rid,))
        conn.execute("DELETE FROM workspaces WHERE id = ?", (rid,))
        conn.commit()
    finally:
        conn.close()


def test_queue_requires_the_token():
    anon = TestClient(app)
    r = anon.post("/api/research/queue", json={"workspace_id": "x", "url": "acme.com"})
    assert r.status_code == 401


def test_enqueue_rejects_a_bare_word(ws):
    r = c.post("/api/research/queue", json={"workspace_id": ws, "url": "not-a-domain"})
    assert r.status_code == 422
    assert "domain" in r.json()["detail"].lower()


def test_enqueue_normalizes_and_dedupes_while_open(ws):
    first = c.post("/api/research/queue", json={"workspace_id": ws, "url": "https://www.BuckeyeRidge.com/contact"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["duplicate"] is False
    assert body["item"]["domain"] == "www.buckeyeridge.com"
    assert body["item"]["status"] == "pending"

    again = c.post("/api/research/queue", json={"workspace_id": ws, "url": "www.buckeyeridge.com"})
    assert again.json()["duplicate"] is True
    assert again.json()["item"]["id"] == body["item"]["id"]


def test_list_reports_what_is_still_open(ws):
    body = c.get("/api/research/queue", params={"workspace_id": ws}).json()
    assert body["pending"] >= 1
    assert any(i["domain"] == "www.buckeyeridge.com" for i in body["items"])


def test_claim_is_atomic_so_two_workers_never_take_the_same_row(ws):
    got = c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5}).json()
    assert got["count"] == 1
    assert got["claimed"][0]["domain"] == "www.buckeyeridge.com"
    assert got["claimed"][0]["attempts"] == 1

    second = c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5}).json()
    assert second["claimed"] == []


def test_complete_creates_one_unverified_lead_and_keeps_the_draft(ws):
    qid = c.get("/api/research/queue", params={"workspace_id": ws}).json()["items"][0]["id"]
    r = c.post(f"/api/research/queue/{qid}/complete",
               json={"status": "done", "profile": PROFILE, "draft": DRAFT})
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["status"] == "done"
    assert item["profile"]["business_name"] == "Buckeye Ridge Roofing"
    assert item["draft"]["subject"] == DRAFT["subject"]
    assert item["business_id"]

    conn = connect()
    try:
        contact = conn.execute(
            "SELECT email, email_status, verified_at FROM contacts WHERE business_id = ?",
            (item["business_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert contact["email"] == "office@buckeyeridge.com"
    assert contact["email_status"] == "unverified"
    assert contact["verified_at"] is None


def test_completing_twice_does_not_duplicate_the_lead(ws):
    qid = c.get("/api/research/queue", params={"workspace_id": ws}).json()["items"][0]["id"]
    first = c.post(f"/api/research/queue/{qid}/complete", json={"status": "done", "profile": PROFILE}).json()
    assert first["already"] is True

    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) n FROM businesses WHERE domain = ?",
                             (PROFILE["domain"],)).fetchone()["n"]
    finally:
        conn.close()
    assert count == 1


def test_worker_error_is_visible_instead_of_stuck(ws):
    item = c.post("/api/research/queue", json={"workspace_id": ws, "url": "stuck-site.com"}).json()["item"]
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    r = c.post(f"/api/research/queue/{item['id']}/complete",
               json={"status": "error", "error": "site returned 403"})
    assert r.status_code == 200
    assert r.json()["item"]["status"] == "error"
    assert "403" in r.json()["item"]["error"]


def test_a_nameless_profile_fails_loudly(ws):
    item = c.post("/api/research/queue", json={"workspace_id": ws, "url": "nameless.example.net"}).json()["item"]
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    r = c.post(f"/api/research/queue/{item['id']}/complete",
               json={"status": "done", "profile": {"domain": "nameless.example.net"}})
    assert r.json()["item"]["status"] == "error"
    assert "no company name" in r.json()["item"]["error"]


LEADS = [
    {
        "name": "Scioto Valley Builders",
        "domain": "https://www.sciotovalleybuilders.com/about",
        "why_now": "Posting for two equipment operators this month, so they are staffing up for site work.",
        "signal_kind": "hiring",
        "signal_detail": "Careers page lists 2x Heavy Equipment Operator, posted 3 days ago.",
        "signal_source": "https://sciotovalleybuilders.com/careers",
        "contact_hint": "Dana Whitfield, office manager (careers page)",
        "fit": 88,
    },
    {
        "name": "Groveport Presbyterian Church",
        "domain": "groveportpres.org",
        "why_now": "Approved a parking lot expansion at last week's session meeting.",
        "signal_kind": "expansion",
        "signal_detail": "Session minutes approve lot restriping and a 40-space addition.",
        "signal_source": "https://groveportpres.org/session-minutes",
        "contact_hint": "trustee",
        "fit": 61,
    },
    {
        # No signal_source: the agent read nothing, so this must not become a lead.
        "name": "Buckeye Ridge Roofing",
        "domain": "buckeyeridge.com",
        "why_now": "They are a roofer, they must need roofers.",
        "signal_kind": "other",
        "fit": 90,
    },
]


def _completed_with_leads(ws, profile=None):
    item = c.post("/api/research/queue", json={"workspace_id": ws, "url": "buckeyeridge.com"}).json()["item"]
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    body = dict(PROFILE if profile is None else profile)
    body.setdefault("leads", LEADS)
    r = c.post(f"/api/research/queue/{item['id']}/complete", json={"status": "done", "profile": body})
    assert r.status_code == 200, r.text
    return r.json()["item"]


def test_agent_leads_are_saved_for_the_example_business(ws):
    item = _completed_with_leads(ws)
    assert item["status"] == "done", item.get("error")
    profile = item["profile"]
    # One unsourced lead was dropped, so two survive.
    assert profile["leads_found"] == 2
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM research_leads WHERE client_business_id = ? ORDER BY fit DESC",
            (item["business_id"],),
        ).fetchall()
        assert [r["name"] for r in rows] == ["Scioto Valley Builders", "Groveport Presbyterian Church"]
        assert all(r["queue_id"] == item["id"] for r in rows)
        assert all(r["client_business_id"] == item["business_id"] for r in rows)
        assert rows[0]["signal_kind"] == "hiring"
        assert rows[0]["signal_source"].endswith("/careers")
        # A bare URL's host is stored without scheme or www.
        assert rows[0]["domain"] == "sciotovalleybuilders.com"
        # Leads are real businesses so they can be sequenced like any other.
        biz = conn.execute("SELECT * FROM businesses WHERE domain = ?", ("sciotovalleybuilders.com",)).fetchone()
        assert biz is not None and biz["id"] == rows[0]["business_id"]
        assert biz["source"] == "agent_lead"
    finally:
        conn.close()


def test_the_example_domain_never_becomes_its_own_lead(ws):
    # The agent is hunting peers of the example; if it echoes the example back it
    # would be a lead for itself, and the outbound sequence would email the sample.
    item = _completed_with_leads(
        ws,
        profile=dict(PROFILE, leads=[
            dict(LEADS[0], name="Buckeye Ridge Roofing", domain="www.buckeyeridge.com",
                 signal_source="https://www.buckeyeridge.com/careers"),
        ]),
    )
    assert item["profile"]["leads_found"] == 0
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM research_leads WHERE queue_id = ?", (item["id"],)
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_agent_leads_land_in_a_list_named_after_the_example(ws):
    item = _completed_with_leads(ws)
    list_id = item["profile"]["leads_list_id"]
    assert list_id
    conn = connect()
    try:
        lst = conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        assert lst["name"] == "Leads like Buckeye Ridge Roofing"
        n = conn.execute("SELECT COUNT(*) n FROM list_members WHERE list_id = ?", (list_id,)).fetchone()["n"]
        assert n == 2
    finally:
        conn.close()


def test_a_second_pass_updates_instead_of_duplicating(ws):
    first = _completed_with_leads(ws)
    second = _completed_with_leads(ws)
    assert second["profile"]["leads_found"] == 2
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) n FROM research_leads WHERE client_business_id = ?",
            (first["business_id"],),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 2


def test_agent_leads_are_readable_over_the_api(ws):
    item = _completed_with_leads(ws)
    r = c.get("/api/research/agent-leads", params={"workspace_id": ws,
                                                  "client_business_id": item["business_id"]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["items"][0]["name"] == "Scioto Valley Builders"
    assert body["items"][0]["why_now"]


def test_a_lead_without_a_site_still_dedupes_on_name(ws):
    # Production runs repeat the same example; a lead with no domain of its own
    # would otherwise be re-added every time and clutter the list.
    first = _completed_with_leads(ws)
    conn = connect()
    try:
        conn.execute(
            "UPDATE research_leads SET domain = NULL WHERE client_business_id = ? AND name = ?",
            (first["business_id"], "Groveport Presbyterian Church"),
        )
        conn.commit()
    finally:
        conn.close()
    _completed_with_leads(ws)
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) n FROM research_leads WHERE client_business_id = ? AND name = ?",
            (first["business_id"], "Groveport Presbyterian Church"),
        ).fetchone()["n"]
        assert n == 1
    finally:
        conn.close()


def test_a_lead_never_invents_a_contact(ws):
    item = _completed_with_leads(ws)
    conn = connect()
    try:
        for lead in conn.execute("SELECT * FROM research_leads WHERE client_business_id = ?",
                                 (item["business_id"],)).fetchall():
            contacts = conn.execute("SELECT COUNT(*) n FROM contacts WHERE business_id = ?",
                                    (lead["business_id"],)).fetchone()["n"]
            assert contacts == 0, "lead discovery must not fabricate contacts"
    finally:
        conn.close()
