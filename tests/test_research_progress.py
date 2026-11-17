"""Contract tests for run visibility: progress, heartbeat, staleness, retry.

The defects these pin were all about a run being invisible rather than wrong:
nothing was written between claim and completion, and the page forgot the run
the moment it was closed. Every assertion here is about what a human can see,
or what stops a dead claim from holding a domain forever.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)

SLUG = "progress-co"


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module", autouse=True)
def _schema() -> None:
    init_db()


@pytest.fixture(scope="module")
def ws() -> str:
    rid = c.post("/api/workspaces", json={
        "name": "Progress Co", "slug": SLUG, "physical_address": "1 Main St, Lisbon OH",
    }).json()["id"]
    yield rid
    conn = connect()
    try:
        conn.execute("DELETE FROM research_leads WHERE workspace_id = ?", (rid,))
        conn.execute("DELETE FROM research_queue WHERE workspace_id = ?", (rid,))
        conn.execute("DELETE FROM workspaces WHERE id = ?", (rid,))
        conn.commit()
    finally:
        conn.close()


def _enqueue(ws, url):
    r = c.post("/api/research/queue", json={"workspace_id": ws, "url": url})
    assert r.status_code == 200, r.text
    return r.json()["item"]


def _row(ws, qid):
    body = c.get("/api/research/queue", params={"workspace_id": ws}).json()
    return next(i for i in body["items"] if i["id"] == qid)


def test_progress_written_by_the_worker_is_readable_on_the_page_route(ws):
    item = _enqueue(ws, "progress-one.com")
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    r = c.post(f"/api/research/queue/{item['id']}/progress",
               json={"stage": "reading", "note": "the agent is reading progress-one.com"})
    assert r.status_code == 200, r.text
    row = _row(ws, item["id"])
    assert row["progress_stage"] == "reading"
    assert "progress-one.com" in row["progress_note"]
    assert row["progress_age_seconds"] == 0
    assert row["stalled"] is False


def test_a_claimed_row_with_a_fresh_progress_note_is_not_stale(ws):
    # The claim itself is old, but the worker is still reporting: reaping this
    # would yank the domain out from under a live run.
    item = _enqueue(ws, "progress-two.com")
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    conn = connect()
    try:
        conn.execute("UPDATE research_queue SET claimed_at = ? WHERE id = ?",
                     (_ago(3600), item["id"]))
        conn.commit()
    finally:
        conn.close()
    c.post(f"/api/research/queue/{item['id']}/progress", json={"stage": "reading", "note": "still going"})
    row = _row(ws, item["id"])
    assert row["stalled"] is False


def test_a_claimed_row_that_stopped_reporting_is_shown_as_stalled(ws):
    item = _enqueue(ws, "progress-three.com")
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    conn = connect()
    try:
        conn.execute("UPDATE research_queue SET claimed_at = ?, progress_at = ? WHERE id = ?",
                     (_ago(3600), _ago(3600), item["id"]))
        conn.commit()
    finally:
        conn.close()
    row = _row(ws, item["id"])
    assert row["stalled"] is True
    assert row["silent_seconds"] >= 3600
    assert row["can_retry"] is True


def test_a_dead_claim_is_reaped_by_the_next_claim_and_runs_again(ws):
    item = _enqueue(ws, "progress-four.com")
    conn = connect()
    try:
        conn.execute(
            "UPDATE research_queue SET status = 'claimed', claimed_at = ?, progress_at = ?,"
            " attempts = 1 WHERE id = ?",
            (_ago(3600), _ago(3600), item["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    got = c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5}).json()
    # Older stalled rows from earlier tests may be reaped in the same sweep; the
    # contract is that this dead claim comes back and its attempt is counted.
    claimed = {i["domain"]: i for i in got["claimed"]}
    assert "progress-four.com" in claimed
    assert claimed["progress-four.com"]["attempts"] == 2


def test_a_claim_that_dies_twice_is_failed_instead_of_looping(ws):
    item = _enqueue(ws, "progress-five.com")
    conn = connect()
    try:
        conn.execute(
            "UPDATE research_queue SET status = 'claimed', claimed_at = ?, progress_at = ?,"
            " attempts = 2 WHERE id = ?",
            (_ago(3600), _ago(3600), item["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    got = c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5}).json()
    assert "progress-five.com" not in [i["domain"] for i in got["claimed"]]
    row = _row(ws, item["id"])
    assert row["status"] == "error"
    assert "twice" in row["error"].lower()


def test_requeue_refuses_a_live_run_and_restarts_a_dead_one(ws):
    live = _enqueue(ws, "progress-six.com")
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    c.post(f"/api/research/queue/{live['id']}/progress", json={"stage": "reading", "note": "busy"})
    r = c.post(f"/api/research/queue/{live['id']}/requeue")
    assert r.status_code == 409

    conn = connect()
    try:
        conn.execute("UPDATE research_queue SET claimed_at = ?, progress_at = ?, attempts = 1 WHERE id = ?",
                     (_ago(3600), _ago(3600), live["id"]))
        conn.commit()
    finally:
        conn.close()
    r = c.post(f"/api/research/queue/{live['id']}/requeue")
    assert r.status_code == 200, r.text
    item = r.json()["item"]
    assert item["status"] == "pending"
    # A human retry earns a full try, or the second death would fail instantly.
    assert item["attempts"] == 0
    assert item["progress_stage"] == "requeued"


def test_re_enqueuing_a_stalled_domain_re_runs_it_instead_of_saying_duplicate(ws):
    item = _enqueue(ws, "progress-seven.com")
    conn = connect()
    try:
        conn.execute(
            "UPDATE research_queue SET status = 'claimed', claimed_at = ?, progress_at = ?,"
            " attempts = 1 WHERE id = ?",
            (_ago(3600), _ago(3600), item["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    body = c.post("/api/research/queue", json={"workspace_id": ws, "url": "progress-seven.com"}).json()
    assert body["duplicate"] is False
    assert body["retried"] is True
    assert body["item"]["status"] == "pending"
    assert "died" in body["note"].lower()


def test_the_page_can_tell_a_live_worker_from_an_absent_one(ws):
    before = c.get("/api/research/queue", params={"workspace_id": ws}).json()["worker"]
    assert before["alive"] in (True, False)  # shape is the contract here

    r = c.post("/api/research/heartbeat", json={"name": "hermes", "detail": "idle - queue empty"})
    assert r.status_code == 200, r.text

    body = c.get("/api/research/queue", params={"workspace_id": ws}).json()
    worker = body["worker"]
    assert worker["name"] == "hermes"
    assert worker["alive"] is True
    assert worker["age_seconds"] == 0
    assert worker["detail"] == "idle - queue empty"


def test_a_stale_heartbeat_reads_as_dead(ws):
    c.post("/api/research/heartbeat", json={"name": "hermes", "detail": "idle"})
    conn = connect()
    try:
        conn.execute("UPDATE worker_heartbeat SET seen_at = ? WHERE name = 'hermes'", (_ago(3600),))
        conn.commit()
    finally:
        conn.close()
    worker = c.get("/api/research/queue", params={"workspace_id": ws}).json()["worker"]
    assert worker["alive"] is False
    assert worker["age_seconds"] >= 3600


def test_progress_aimed_at_a_pending_row_is_ignored(ws):
    # Nothing has claimed this row, so 'the agent is reading it' would be a lie.
    item = _enqueue(ws, "progress-nine.com")
    r = c.post(f"/api/research/queue/{item['id']}/progress", json={"stage": "reading", "note": "nope"})
    assert r.status_code == 200
    assert r.json().get("ignored") == "pending"
    assert _row(ws, item["id"])["progress_stage"] != "reading"


def test_progress_after_a_finish_is_ignored_not_fatal(ws):
    # A worker's ticker can fire once after completion; that must not 4xx and
    # must not rewrite the stage of a finished run.
    item = _enqueue(ws, "progress-eight.com")
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    c.post(f"/api/research/queue/{item['id']}/complete", json={"status": "error", "error": "no page"})
    r = c.post(f"/api/research/queue/{item['id']}/progress", json={"stage": "saving", "note": "late"})
    assert r.status_code == 200
    assert r.json().get("ignored") == "error"
    assert _row(ws, item["id"])["progress_stage"] != "saving"
