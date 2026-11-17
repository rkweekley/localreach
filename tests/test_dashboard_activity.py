"""The Overview has to answer "what happened?" on a quiet day.

The report that started this was one sentence: *none of the items are clickable,
there isn't any recent activity*. Both halves were true for the same reason - the
Overview was a report, not a dashboard. The tiles counted things and went
nowhere; the activity panel read `messages`, so a workspace that had imported
prospects and assigned leads but never sent an email looked dead.

These tests pin the two properties that make the screen honest:

  * every tile destination the markup promises actually resolves (a tile that
    navigates to a view that does not exist blanks the app)
  * the feed is built from more than the outbox, and no single bulk event is
    allowed to flood it - one import must not bury a reply
  * the Suppressed tile has a list behind it, scoped to the workspace whose
    count it displays
"""

import os

import pytest

from app.db import connect, init_db, new_id, utcnow
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', 'test-token')}"}

_CLEAN = {"ws": set(), "biz": set()}


@pytest.fixture(autouse=True)
def _schema():
    init_db()
    yield


@pytest.fixture(scope="module", autouse=True)
def _teardown():
    yield
    conn = connect()
    try:
        ws_ids = tuple(_CLEAN["ws"]) or ("",)
        biz_ids = tuple(_CLEAN["biz"]) or ("",)
        marks = ",".join("?" * len(ws_ids))
        bmarks = ",".join("?" * len(biz_ids))
        for stmt in [
            f"DELETE FROM messages WHERE workspace_id IN ({marks})",
            f"DELETE FROM suppressions WHERE workspace_id IN ({marks})",
            f"DELETE FROM workspace_businesses WHERE workspace_id IN ({marks})",
            f"DELETE FROM client_leads WHERE workspace_id IN ({marks})",
            f"DELETE FROM workspace_clients WHERE workspace_id IN ({marks})",
            f"DELETE FROM lists WHERE workspace_id IN ({marks})",
            f"DELETE FROM workspaces WHERE id IN ({marks})",
        ]:
            conn.execute(stmt, ws_ids)
        for stmt in [
            f"DELETE FROM client_leads WHERE business_id IN ({bmarks})",
            f"DELETE FROM client_leads WHERE client_business_id IN ({bmarks})",
            f"DELETE FROM contacts WHERE business_id IN ({bmarks})",
            f"DELETE FROM scores WHERE business_id IN ({bmarks})",
            f"DELETE FROM businesses WHERE id IN ({bmarks})",
        ]:
            conn.execute(stmt, biz_ids)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(app, headers=AUTH)


def _workspace(client, slug):
    rows = client.get("/api/workspaces").json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    for w in rows:
        if w.get("slug") == slug:
            return w["id"]
    r = client.post("/api/workspaces", json={
        "name": slug.replace("-", " ").title(),
        "slug": slug,
        "physical_address": "9 Operator Way, Lowell, OH 45744",
    })
    assert r.status_code < 300, r.text
    ws_id = r.json()["id"]
    _CLEAN["ws"].add(ws_id)
    return ws_id


def _biz(name):
    conn = connect()
    try:
        bid = new_id()
        now = utcnow()
        conn.execute(
            """INSERT INTO businesses (id, name, city, state, source, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?)""",
            (bid, name, "Marietta", "OH", "manual", now, now),
        )
        conn.commit()
    finally:
        conn.close()
    _CLEAN["biz"].add(bid)
    return bid


def _lead(ws, client_biz, biz, fit=80):
    conn = connect()
    try:
        now = utcnow()
        conn.execute(
            """INSERT INTO client_leads (id, workspace_id, client_business_id, business_id,
                                         fit, origin, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (new_id(), ws, client_biz, biz, fit, "import", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def test_the_feed_shows_work_that_is_not_email(client):
    """Imports and assignments must appear even when nothing was ever sent."""
    ws = _workspace(client, "feed-quiet-workspace")
    owner = _biz("Quiet Owner Co")
    for i in range(6):
        _lead(ws, owner, _biz(f"Quiet Prospect {i}"))

    body = client.get(f"/api/overview?workspace_id={ws}").json()
    activity = body.get("activity") or []
    assert activity, "a workspace with six assigned leads must not look dead"
    assert any(a["kind"] == "lead" for a in activity)


def test_no_single_kind_floods_the_feed(client):
    """One bulk import is one event, not six rows that hide the reply."""
    ws = _workspace(client, "feed-flood-workspace")
    owner = _biz("Flood Owner Co")
    for i in range(10):
        _lead(ws, owner, _biz(f"Flood Prospect {i}"))

    activity = client.get(f"/api/overview?workspace_id={ws}").json().get("activity") or []
    per_kind = {}
    for a in activity:
        per_kind[a["kind"]] = per_kind.get(a["kind"], 0) + 1
    assert per_kind.get("lead", 0) <= 2, per_kind


def test_every_tile_destination_is_a_real_view():
    """The markup cannot offer a destination the app does not handle.

    Most tiles switch view. `suppressions` is a panel rather than a view - the
    count belongs to a modal, because a refreshable list of opt-outs is a panel,
    not a screen you navigate to. Both are legitimate; an unknown string is not.
    """
    from pathlib import Path

    html = Path("app/static/index.html").read_text()
    views = {"overview", "leads", "research", "sequences", "outbox", "voice"}
    panels = {"suppressions"}
    import re

    for view in re.findall(r'card\([^;]*?"([a-z]+)"', html, re.S):
        assert view in views | panels, f"tile navigates to unknown destination {view!r}"


def test_the_suppressed_tile_has_a_list_behind_it(client):
    ws = _workspace(client, "suppression-list-workspace")
    conn = connect()
    try:
        now = utcnow()
        conn.execute(
            """INSERT INTO suppressions (id, workspace_id, email, domain, reason,
                                         scope, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (new_id(), ws, "gone@example.com", "example.com", "unsubscribed", "workspace", now),
        )
        conn.commit()
    finally:
        conn.close()

    rows = client.get(f"/api/suppressions?workspace_id={ws}").json()
    rows = rows if isinstance(rows, list) else rows.get("results", [])
    assert [r["email"] for r in rows] == ["gone@example.com"]

    other = _workspace(client, "suppression-other-workspace")
    other_rows = client.get(f"/api/suppressions?workspace_id={other}").json()
    other_rows = other_rows if isinstance(other_rows, list) else other_rows.get("results", [])
    assert other_rows == [], "opt-outs must never leak between workspaces"
