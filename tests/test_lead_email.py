"""The per-lead email is the whole point of the research.

A lead with no email is a spreadsheet row. The value of researching a client is
that the first touch to each of their prospects can name something real: the role
they are hiring for, the permit they pulled, the gap on their own site. These
tests pin that the email survives the trip: agent payload -> research_leads row ->
enrollment override -> the message that actually goes out.

They also pin the guards. An override must only ever replace step 1 (step 2 and 3
are the follow-ups the sequence owns), the unsubscribe footer must still be
appended to an overridden body, and a lead whose email is missing or blank must
fall back to the template rather than sending an empty message.
"""

import os

import pytest

from app import routes_campaigns
from app.db import connect, init_db, new_id, utcnow
from app.main import app
from app.routes_research import _store_research_leads

AUTH = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', 'test-token')}"}


@pytest.fixture(autouse=True)
def _schema():
    init_db()
    yield


_CLEAN = {"ws": set(), "biz": set()}
# Enrollments/sequences live only in this file, so a marker in the name is enough
# to find them again; the workspace ids above find everything else.
_SEQ_NAME = "LeadMailTestOutreach"


@pytest.fixture(scope="module", autouse=True)
def _teardown():
    """Leave the shared test database as we found it.

    Every test file in this suite runs against the same sqlite file, so rows we
    leave behind break other files' cleanup (an FK failure in one module's
    teardown cascades into a count mismatch in another's assertions).
    """
    yield
    conn = connect()
    try:
        ws_ids = tuple(_CLEAN["ws"]) or ("",)
        biz_ids = tuple(_CLEAN["biz"]) or ("",)
        marks = ",".join("?" * len(ws_ids))
        bmarks = ",".join("?" * len(biz_ids))
        seq_ids = tuple(
            r["id"] for r in conn.execute(
                f"SELECT id FROM sequences WHERE workspace_id IN ({marks})", ws_ids)
        ) or ("",)
        smarks = ",".join("?" * len(seq_ids))
        en_ids = tuple(
            r["id"] for r in conn.execute(
                f"SELECT id FROM enrollments WHERE sequence_id IN ({smarks})", seq_ids)
        ) or ("",)
        emarks = ",".join("?" * len(en_ids))
        conn.execute(f"DELETE FROM messages WHERE enrollment_id IN ({emarks})", en_ids)
        conn.execute(f"DELETE FROM enrollments WHERE sequence_id IN ({smarks})", seq_ids)
        conn.execute(f"DELETE FROM sequence_steps WHERE sequence_id IN ({smarks})", seq_ids)
        conn.execute(f"DELETE FROM sequences WHERE id IN ({smarks})", seq_ids)
        conn.execute(f"DELETE FROM research_leads WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM contacts WHERE business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM list_members WHERE business_id IN ({bmarks})", biz_ids)
        conn.execute(
            f"DELETE FROM list_members WHERE list_id IN "
            f"(SELECT id FROM lists WHERE workspace_id IN ({marks}))", ws_ids
        )
        conn.execute(f"DELETE FROM lists WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM businesses WHERE id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM workspaces WHERE id IN ({marks})", ws_ids)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(app, headers=AUTH)


def _workspace(client, slug="lead-mail"):
    existing = client.get("/api/workspaces").json()
    for w in existing if isinstance(existing, list) else existing.get("items", []):
        if w.get("slug") == slug:
            return w["id"]
    r = client.post(
        "/api/workspaces",
        json={"name": "Lead Mail Co", "slug": slug, "physical_address": "100 Main St, Lowell, OH 45744"},
    )
    assert r.status_code < 300, r.text
    ws_id = r.json()["id"]
    _CLEAN["ws"].add(ws_id)
    return ws_id


def _business(conn, workspace_id, name, domain):
    """Businesses are global in this schema; the workspace link is the list."""
    bid = new_id()
    conn.execute(
        """INSERT INTO businesses (id, name, website, domain, source, source_ref,
                                   fetched_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (bid, name, f"https://{domain}", domain, "agent_lead", workspace_id,
         utcnow(), utcnow(), utcnow()),
    )
    conn.commit()
    _CLEAN["biz"].add(bid)
    return bid


# ------------------------------------------------------------------ the payload


def test_the_agent_email_is_stored_on_the_lead(client):
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _business(conn, ws, "Cyberal Solutions", "cyberalsolutions.com")
        summary = _store_research_leads(
            conn,
            ws,
            None,
            client_bid,
            {
                "leads": [
                    {
                        "name": "Promanco",
                        "domain": "promanco.com",
                        "why_now": "Hiring a project engineer",
                        "signal_kind": "hiring",
                        "signal_detail": "Project Engineer, Marietta OH",
                        "signal_source": "https://example.com/jobs/123",
                        "fit": 92,
                        "email": {
                            "subject": "Your project engineer posting",
                            "body": "You are hiring a project engineer in Marietta. We handle the IT side.",
                        },
                    }
                ]
            },
        )
        assert summary["created"] == 1
        row = conn.execute(
            "SELECT email_subject, email_body FROM research_leads WHERE domain = 'promanco.com'"
        ).fetchone()
        assert row["email_subject"] == "Your project engineer posting"
        assert "project engineer" in row["email_body"]
    finally:
        conn.close()


def test_a_lead_with_no_email_still_stores(client):
    """Half a payload must not lose the lead — the signal is still worth keeping."""
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _business(conn, ws, "Cyberal Solutions", "cyberalsolutions.com")
        _store_research_leads(
            conn,
            ws,
            None,
            client_bid,
            {
                "leads": [
                    {
                        "name": "No Mail Co",
                        "domain": "nomail-co.com",
                        "signal_kind": "news",
                        "signal_source": "https://example.com/news",
                        "fit": 40,
                    }
                ]
            },
        )
        row = conn.execute(
            "SELECT email_subject, email_body FROM research_leads WHERE domain = 'nomail-co.com'"
        ).fetchone()
        assert row is not None
        assert row["email_subject"] is None
        assert row["email_body"] is None
    finally:
        conn.close()


def test_a_blank_email_does_not_wipe_a_good_one(client):
    """Re-running research with a lazy payload must not erase the draft we have."""
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _business(conn, ws, "Cyberal Solutions", "cyberalsolutions.com")
        lead = {
            "name": "Reuse Co",
            "domain": "reuse-co.com",
            "signal_kind": "hiring",
            "signal_source": "https://example.com/jobs/1",
            "fit": 70,
            "email": {"subject": "Good subject", "body": "Good body"},
        }
        _store_research_leads(conn, ws, None, client_bid, {"leads": [lead]})

        lead2 = dict(lead)
        lead2["email"] = {"subject": "", "body": "   "}
        _store_research_leads(conn, ws, None, client_bid, {"leads": [lead2]})

        row = conn.execute(
            "SELECT email_subject, email_body FROM research_leads WHERE domain = 'reuse-co.com'"
        ).fetchone()
        assert row["email_subject"] == "Good subject"
        assert row["email_body"] == "Good body"
    finally:
        conn.close()


# --------------------------------------------------------------- the send path


def _seed_sendable(conn, ws, *, subject=None, body=None, step_order=1, override=True):
    """A workspace ready to send one email: business, verified contact, sequence."""
    bid = _business(conn, ws, "Promanco", f"sendable-{new_id()[:8]}.com")
    cid = new_id()
    conn.execute(
        """INSERT INTO contacts (id, business_id, email, email_status, email_source, verified_at,
                                 is_primary, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cid, bid, f"owner@{new_id()[:8]}.com", "verified", "website", utcnow(), 1, utcnow()),
    )
    seq = new_id()
    conn.execute(
        """INSERT INTO sequences (id, workspace_id, name, status, send_window, created_at)
           VALUES (?,?,?,'active',NULL,?)""",
        (seq, ws, "Outreach", utcnow()),
    )
    conn.execute(
        """INSERT INTO sequence_steps (id, sequence_id, step_order, channel, subject_template,
                                       body_template, delay_days, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (new_id(), seq, step_order, "email", "Template subject", "Template body {{first_name}}", 0, utcnow()),
    )
    en = new_id()
    conn.execute(
        """INSERT INTO enrollments (id, sequence_id, business_id, contact_id, status, current_step,
                                    next_send_at, created_at, override_subject, override_body)
           VALUES (?,?,?,?,'active',?,?,?,?,?)""",
        (en, seq, bid, cid, step_order - 1, utcnow(), utcnow(),
         subject if override else None, body if override else None),
    )
    conn.commit()
    return en


def _run_tick(monkeypatch):
    sent = []

    def fake_send(from_email, from_name, to_email, subject, body, **kw):
        sent.append({"subject": subject, "body": body, "to": to_email})
        return {"provider_id": "fake-1", "status": "sent", "ok": True}

    monkeypatch.setattr(routes_campaigns.mailer, "send", fake_send)
    return sent


def test_step_one_sends_the_email_the_agent_wrote(client, monkeypatch):
    ws = _workspace(client)
    conn = connect()
    try:
        _seed_sendable(
            conn, ws,
            subject="Your project engineer posting",
            body="You are hiring a project engineer in Marietta. Worth ten minutes?",
        )
    finally:
        conn.close()

    sent = _run_tick(monkeypatch)
    routes_campaigns.tick(workspace_id=ws)

    assert sent, "tick sent nothing"
    assert sent[0]["subject"] == "Your project engineer posting"
    assert "project engineer" in sent[0]["body"]


def test_the_unsubscribe_footer_survives_an_overridden_body(client, monkeypatch):
    """An override is the agent's words. It is not permission to skip CAN-SPAM."""
    ws = _workspace(client)
    conn = connect()
    try:
        _seed_sendable(conn, ws, subject="Hi", body="Short body with no footer.")
    finally:
        conn.close()

    sent = _run_tick(monkeypatch)
    routes_campaigns.tick(workspace_id=ws)

    assert sent, "tick sent nothing"
    assert "unsubscribe" in sent[0]["body"].lower() or "/u/" in sent[0]["body"]


def test_step_two_follows_the_sequence_not_the_override(client, monkeypatch):
    """The override is a first touch. Reusing it as the follow-up is how people
    get the same email twice."""
    ws = _workspace(client)
    conn = connect()
    try:
        _seed_sendable(conn, ws, subject="Agent first touch", body="Agent body", step_order=2)
    finally:
        conn.close()

    sent = _run_tick(monkeypatch)
    routes_campaigns.tick(workspace_id=ws)

    assert sent, "tick sent nothing"
    assert sent[0]["subject"] == "Template subject"
    assert "Agent body" not in sent[0]["body"]


def test_an_empty_override_falls_back_to_the_template(client, monkeypatch):
    ws = _workspace(client)
    conn = connect()
    try:
        _seed_sendable(conn, ws, subject="   ", body="", step_order=1)
    finally:
        conn.close()

    sent = _run_tick(monkeypatch)
    routes_campaigns.tick(workspace_id=ws)

    assert sent, "tick sent nothing"
    assert sent[0]["subject"] == "Template subject"
