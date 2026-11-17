"""Dashboard numbers must describe relationships, and the tiles must be reachable.

Three separate bugs on the same screen all had one shape: a number counted
registry rows and was rendered under a label that promised a relationship. The
"Screened" figure was named for the refinement step that no longer exists, the
"Leads" tile counted every client as a lead, and the research pill divided by
the size of the business registry so a workspace with two clients and no leads
read "0 of 2 researched".

The other half of "the dashboard works" is that its tiles can actually move. The
"Open tasks" tile counts `tasks`, and non-email sequence steps are the only
thing that should ever write there; before this suite existed the worker skipped
those steps and wrote nothing, so the tile was structurally pinned to zero.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from app.db import connect, init_db, new_id, utcnow
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', 'test-token')}"}

_TRACK = {"ws": set(), "biz": set(), "seq": set(), "enr": set(), "contact": set()}


@pytest.fixture(autouse=True)
def _schema():
    init_db()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(app, headers=AUTH)


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    conn = connect()
    try:
        def purge(table, column, ids):
            ids = tuple(ids) or ("",)
            marks = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM {table} WHERE {column} IN ({marks})", ids)

        purge("tasks", "workspace_id", _TRACK["ws"])
        purge("tasks", "enrollment_id", _TRACK["enr"])
        purge("enrollments", "id", _TRACK["enr"])
        purge("sequence_steps", "sequence_id", _TRACK["seq"])
        purge("sequences", "id", _TRACK["seq"])
        purge("client_leads", "workspace_id", _TRACK["ws"])
        purge("workspace_clients", "workspace_id", _TRACK["ws"])
        purge("contacts", "business_id", _TRACK["biz"])
        purge("businesses", "id", _TRACK["biz"])
        purge("workspaces", "id", _TRACK["ws"])
        conn.commit()
    finally:
        conn.close()
    for bucket in _TRACK.values():
        bucket.clear()


def _workspace(client, slug):
    r = client.post(
        "/api/workspaces",
        json={"name": slug, "slug": slug, "physical_address": "9 Operator Way, Lowell, OH 45744"},
    )
    assert r.status_code < 300, r.text
    ws = r.json()["id"]
    _TRACK["ws"].add(ws)
    return ws


def _biz(conn, name, domain, source="manual"):
    bid = new_id()
    now = utcnow()
    conn.execute(
        """INSERT INTO businesses (id, name, city, state, website, domain, source,
                                   created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (bid, name, "Marietta", "OH", f"https://{domain}", domain, source, now, now),
    )
    _TRACK["biz"].add(bid)
    return bid


def _contact(conn, business_id, email):
    cid = new_id()
    conn.execute(
        """INSERT INTO contacts (id, business_id, email, email_status, is_primary, created_at)
           VALUES (?,?,?,?,?,?)""",
        (cid, business_id, email, "verified", 1, utcnow()),
    )
    _TRACK["contact"].add(cid)
    return cid


def _sequence(conn, ws, name, steps, status="active"):
    sid = new_id()
    now = utcnow()
    conn.execute(
        "INSERT INTO sequences (id, workspace_id, name, status, created_at) VALUES (?,?,?,?,?)",
        (sid, ws, name, status, now),
    )
    for order, channel in enumerate(steps, start=1):
        conn.execute(
            """INSERT INTO sequence_steps (id, sequence_id, step_order, channel, delay_days,
                                           subject_template, body_template, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (new_id(), sid, order, channel, 0, "Hi {{business}}", "Body for {{business}}", now),
        )
    _TRACK["seq"].add(sid)
    return sid


def _enroll(conn, seq, business_id, contact_id):
    eid = new_id()
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn.execute(
        """INSERT INTO enrollments (id, sequence_id, business_id, contact_id, status,
                                    current_step, next_send_at, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (eid, seq, business_id, contact_id, "active", 0, due, utcnow()),
    )
    _TRACK["enr"].add(eid)
    return eid


def test_overview_contacts_belong_to_the_asked_workspace(client):
    """A global COUNT(*) describes other workspaces' data under this one's label."""
    mine = _workspace(client, "dash-ws-mine")
    theirs = _workspace(client, "dash-ws-theirs")
    conn = connect()
    try:
        my_lead = _biz(conn, "Mine One", "mine-one.test")
        their_lead = _biz(conn, "Theirs One", "theirs-one.test")
        _contact(conn, my_lead, "a@mine-one.test")
        _contact(conn, their_lead, "b@theirs-one.test")
        _contact(conn, their_lead, "c@theirs-one.test")
        now = utcnow()
        conn.execute(
            "INSERT INTO client_leads (id, workspace_id, client_business_id, business_id, fit, status, origin, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id(), mine, _biz(conn, "Mine Client", "mine-client.test"), my_lead, 80, "new", "manual", now, now),
        )
        conn.execute(
            "INSERT INTO client_leads (id, workspace_id, client_business_id, business_id, fit, status, origin, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id(), theirs, _biz(conn, "Their Client", "their-client.test"), their_lead, 80, "new", "manual", now, now),
        )
        conn.commit()
    finally:
        conn.close()

    c = client.get(f"/api/overview?workspace_id={mine}").json()["counts"]
    assert c["leads"] == 1
    assert c["contacts"] == 1, "only the leads of this workspace have contacts here"
    assert c["contacts_with_email"] == 1


def test_research_status_denominates_by_leads_not_registry(client):
    """Two clients and no leads must not read '0 of 2 researched'."""
    base = client.get("/api/research/status").json()
    ws = _workspace(client, "dash-ws-research")
    conn = connect()
    try:
        client_bid = _biz(conn, "Dash Client", "dash-client.test", source="manual")
        conn.execute(
            "INSERT INTO workspace_clients (id, workspace_id, business_id, status, source, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (new_id(), ws, client_bid, "active", "manual", utcnow()),
        )
        conn.commit()
        s = client.get("/api/research/status").json()
        assert s["prospects"] == base["prospects"], "a client is not a research target"
        assert s["researched"] == base["researched"]
        assert s["clients"] == base["clients"] + 1
        assert s["businesses"] == base["businesses"] + 1

        lead = _biz(conn, "Dash Lead", "dash-lead.test")
        now = utcnow()
        conn.execute(
            "INSERT INTO client_leads (id, workspace_id, client_business_id, business_id, fit, status, origin, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id(), ws, client_bid, lead, 72, "new", "manual", now, now),
        )
        conn.execute("UPDATE businesses SET site_research = ? WHERE id = ?", ('{"summary":"x"}', lead))
        conn.commit()
        s = client.get("/api/research/status").json()
        assert s["prospects"] == base["prospects"] + 1, "the denominator is lead relationships"
        assert s["researched"] == base["researched"] + 1, "researched counts leads with cached site research"
    finally:
        conn.close()


def test_non_email_step_creates_a_task_and_moves_the_tile(client):
    """A call step has to land somewhere; nothing wrote to `tasks` before this."""
    ws = _workspace(client, "dash-ws-tasks")
    conn = connect()
    try:
        lead = _biz(conn, "Task Lead", "task-lead.test")
        cid = _contact(conn, lead, "owner@task-lead.test")
        seq = _sequence(conn, ws, "Call first", ["call", "email"])
        _enroll(conn, seq, lead, cid)
        conn.commit()
    finally:
        conn.close()

    assert client.get(f"/api/overview?workspace_id={ws}").json()["counts"]["open_tasks"] == 0
    r = client.post(f"/api/worker/tick?workspace_id={ws}")
    assert r.status_code < 300, r.text
    assert [x["status"] for x in r.json()["results"]] == ["task_only"], r.text
    assert client.get(f"/api/overview?workspace_id={ws}").json()["counts"]["open_tasks"] == 1

    listed = client.get(f"/api/tasks?workspace_id={ws}").json()
    assert listed["count"] == 1
    task = listed["results"][0]
    assert task["kind"] == "call"
    assert task["business_name"] == "Task Lead"
    assert task["due_at"]

    done = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})
    assert done.status_code == 200, done.text
    assert client.get(f"/api/tasks?workspace_id={ws}").json()["count"] == 0
    assert client.get(f"/api/overview?workspace_id={ws}").json()["counts"]["open_tasks"] == 0
    conn = connect()
    try:
        row = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (task["id"],)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "done" and row["completed_at"]


def test_ticking_twice_does_not_duplicate_the_task(client):
    """The email step must not be blocked behind a duplicate call task."""
    ws = _workspace(client, "dash-ws-dup")
    conn = connect()
    try:
        lead = _biz(conn, "Dup Lead", "dup-lead.test")
        cid = _contact(conn, lead, "dup@dup-lead.test")
        seq = _sequence(conn, ws, "Call twice", ["call", "email"])
        _enroll(conn, seq, lead, cid)
        conn.commit()
    finally:
        conn.close()
    client.post(f"/api/worker/tick?workspace_id={ws}")
    client.post(f"/api/worker/tick?workspace_id={ws}")
    assert client.get(f"/api/tasks?workspace_id={ws}").json()["count"] == 1


def test_task_patch_rejects_unknown_status_and_unknown_id(client):
    ws = _workspace(client, "dash-ws-bad")
    assert client.patch("/api/tasks/nope", json={"status": "done"}).status_code == 404
    conn = connect()
    try:
        lead = _biz(conn, "Bad Lead", "bad-lead.test")
        cid = _contact(conn, lead, "bad@bad-lead.test")
        seq = _sequence(conn, ws, "Bad", ["linkedin"])
        _enroll(conn, seq, lead, cid)
        conn.commit()
    finally:
        conn.close()
    client.post(f"/api/worker/tick?workspace_id={ws}")
    tid = client.get(f"/api/tasks?workspace_id={ws}").json()["results"][0]["id"]
    assert client.patch(f"/api/tasks/{tid}", json={"status": "banana"}).status_code == 422
