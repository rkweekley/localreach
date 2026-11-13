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
        conn.execute("DELETE FROM research_queue WHERE workspace_id = ?", (rid,))
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
