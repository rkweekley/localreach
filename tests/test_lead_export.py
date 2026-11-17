"""Copy-and-paste export: the text a human sends must be the text the app would send.

Ryan sends outreach by hand from his own mailbox for now, so "export" is not a
reporting feature - it is the send path. That makes three things load-bearing:

1. The CAN-SPAM footer is appended by the *mailer*, at send time. A hand-copied
   body therefore ships with no postal address and no unsubscribe link unless
   the export adds them itself. This suite pins that.
2. The unsubscribe link carries a token bound to the recipient. It is the only
   way a hand-sent email can still feed `suppressions`; drop it and the
   suppression table silently stops learning from real replies.
3. A lead with no drafted email must come back empty. "Export" must never
   degenerate into the app composing a first touch it never researched - that
   is exactly the invented-contact failure this product is built to avoid.
"""

import csv
import io
import os

import pytest

from app.db import connect, init_db, new_id, utcnow
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', 'test-token')}"}

_TRACK = {"ws": set(), "biz": set(), "contact": set(), "rl": set(),
          "cl": set(), "wc": set(), "sup": set()}


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

        purge("suppressions", "id", _TRACK["sup"])
        purge("client_leads", "id", _TRACK["cl"])
        purge("research_leads", "id", _TRACK["rl"])
        purge("workspace_clients", "id", _TRACK["wc"])
        purge("contacts", "id", _TRACK["contact"])
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


def _biz(conn, name, domain, city="Marietta", state="OH", address1=None, postal=None):
    bid = new_id()
    now = utcnow()
    conn.execute(
        """INSERT INTO businesses (id, name, city, state, address1, postal_code, website,
                                   domain, source, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (bid, name, city, state, address1, postal, f"https://{domain}", domain,
         "manual", now, now),
    )
    _TRACK["biz"].add(bid)
    return bid


def _contact(conn, business_id, email, status="verified"):
    cid = new_id()
    conn.execute(
        """INSERT INTO contacts (id, business_id, email, email_status, is_primary, created_at)
           VALUES (?,?,?,?,?,?)""",
        (cid, business_id, email, status, 1, utcnow()),
    )
    _TRACK["contact"].add(cid)
    return cid


def _lead(client, ws, client_name="Parmar Law", body="Short note about your roof.",
          subject="Quick question", prospect_email="tj@buckeye-roofing.test",
          with_draft=True, prospect_name="Buckeye Roofing"):
    """One prospect, sold for one client, optionally with a drafted first touch."""
    conn = connect()
    try:
        client_bid = _biz(conn, client_name, "parmar-law.test",
                          address1="120 Front St", postal="45750")
        wc_id = new_id()
        conn.execute(
            "INSERT INTO workspace_clients (id, workspace_id, business_id, status, source, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (wc_id, ws, client_bid, "active", "manual", utcnow()),
        )
        _TRACK["wc"].add(wc_id)

        prospect_bid = _biz(conn, prospect_name, "buckeye-roofing.test")
        if prospect_email:
            _contact(conn, prospect_bid, prospect_email)

        rl_id = None
        if with_draft:
            rl_id = new_id()
            conn.execute(
                """INSERT INTO research_leads (id, workspace_id, client_business_id, business_id,
                                               name, domain, email_subject, email_body, fit, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (rl_id, ws, prospect_bid, prospect_bid, prospect_name,
                 "buckeye-roofing.test", subject, body, 80, utcnow()),
            )
            _TRACK["rl"].add(rl_id)

        cl_id = new_id()
        now = utcnow()
        conn.execute(
            """INSERT INTO client_leads (id, workspace_id, client_business_id, business_id,
                                         research_lead_id, fit, status, origin, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cl_id, ws, client_bid, prospect_bid, rl_id, 80, "new", "research", now, now),
        )
        _TRACK["cl"].add(cl_id)
        conn.commit()
        return cl_id
    finally:
        conn.close()


def _suppress(workspace_id, email, reason="unsubscribed"):
    conn = connect()
    try:
        sid = new_id()
        conn.execute(
            """INSERT INTO suppressions (id, workspace_id, email, domain, reason, scope, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (sid, workspace_id, email.lower(), "buckeye-roofing.test", reason, "global", utcnow()),
        )
        conn.commit()
        _TRACK["sup"].add(sid)
    finally:
        conn.close()


def test_hand_sent_copy_carries_the_footer_and_a_tracked_link(client):
    """The mailer is not in the loop, so the footer has to come from the export."""
    ws = _workspace(client, "exp-ws-footer")
    lead_id = _lead(client, ws)

    r = client.get(f"/api/leads/{lead_id}/email?workspace_id={ws}")
    assert r.status_code == 200, r.text
    item = r.json()

    assert item["subject"] == "Quick question"
    assert item["to"] == "tj@buckeye-roofing.test"
    assert item["footer_included"] is True

    body = item["body"]
    assert "Short note about your roof." in body
    # The client is the sender, so the client's identity identifies the message.
    assert "Parmar Law" in body
    assert "120 Front St, Marietta, OH 45750" in body
    # And the opt-out is a live, token-bearing URL, not a dead "/u/".
    assert "/u/u2." in body
    assert item["unsubscribe_url"].endswith(f"/u/{body.rsplit('/u/', 1)[1].strip()}")

    text = item["copy_text"]
    assert text.startswith("To: tj@buckeye-roofing.test")
    assert "Subject: Quick question" in text
    assert text.rstrip().endswith(body.rstrip()), "copy text must not truncate the footer"


def test_a_suppressed_recipient_is_flagged_not_silently_exported(client):
    """Suppression has to survive the fact that a human, not the worker, sends."""
    ws = _workspace(client, "exp-ws-sup")
    lead_id = _lead(client, ws)
    _suppress(ws, "tj@buckeye-roofing.test")

    item = client.get(f"/api/leads/{lead_id}/email?workspace_id={ws}").json()
    assert item["blocked"] is True
    assert item["block_reason"] == "unsubscribed"
    assert any("Suppressed" in w for w in item["warnings"])

    csv_text = client.get(f"/api/leads/export.csv?workspace_id={ws}").text.lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    mine = [x for x in rows if x["lead_id"] == lead_id]
    assert len(mine) == 1
    assert mine[0]["blocked"] == "yes", "a blocked lead must be visible, not dropped"


def test_csv_round_trips_bodies_with_commas_quotes_and_newlines(client):
    ws = _workspace(client, "exp-ws-csv")
    body = 'He said "call me", then hung up.\nSecond line, with a comma.'
    lead_id = _lead(client, ws, body=body, subject="Roof, gutters & siding")

    csv_text = client.get(f"/api/leads/export.csv?workspace_id={ws}").text.lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    mine = [x for x in rows if x["lead_id"] == lead_id]
    assert len(mine) == 1, "quoting must not split one lead across rows"
    assert mine[0]["body"].startswith(body)
    assert mine[0]["subject"] == "Roof, gutters & siding"


def test_csv_guards_formulas_in_identity_columns(client):
    """A business named like a formula executes when the file is opened."""
    ws = _workspace(client, "exp-ws-formula")
    _lead(client, ws, prospect_name='=HYPERLINK("http://x","click")')

    csv_text = client.get(f"/api/leads/export.csv?workspace_id={ws}").text.lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    names = [x["business"] for x in rows]
    assert any(n.startswith("'=HYPERLINK") for n in names), names
    assert not any(n.startswith("=") for n in names), "no cell may begin a formula"


def test_export_is_scoped_to_the_workspace_asked_for(client):
    mine = _workspace(client, "exp-ws-mine")
    theirs = _workspace(client, "exp-ws-theirs")
    my_lead = _lead(client, mine, prospect_name="Mine Prospect", prospect_email="a@mine.test")
    their_lead = _lead(client, theirs, prospect_name="Their Prospect", prospect_email="b@theirs.test")

    text = client.get(f"/api/leads/export.csv?workspace_id={mine}").text.lstrip("\ufeff")
    rows = list(csv.DictReader(io.StringIO(text)))
    ids = {r["lead_id"] for r in rows}
    assert my_lead in ids
    assert their_lead not in ids
    assert "Theirs Prospect" not in text


def test_a_lead_without_a_draft_never_gets_invented_copy(client):
    """No researched first touch means an empty body and an honest warning."""
    ws = _workspace(client, "exp-ws-nodraft")
    lead_id = _lead(client, ws, with_draft=False)

    item = client.get(f"/api/leads/{lead_id}/email?workspace_id={ws}").json()
    assert item["has_draft"] is False
    assert item["body"] == ""
    assert item["footer_included"] is False
    assert any("No first touch" in w for w in item["warnings"])

    text = client.get(f"/api/leads/export.csv?workspace_id={ws}").text.lstrip("\ufeff")
    rows = [x for x in csv.DictReader(io.StringIO(text)) if x["lead_id"] == lead_id]
    assert len(rows) == 1
    assert rows[0]["body"] == ""


def test_export_refuses_the_wrong_workspace(client):
    ws = _workspace(client, "exp-ws-404")
    lead_id = _lead(client, ws)

    assert client.get(f"/api/leads/{lead_id}/email?workspace_id=nope").status_code == 404
    assert client.get(f"/api/leads/nope/email?workspace_id={ws}").status_code == 404


def test_export_requires_the_token():
    from fastapi.testclient import TestClient

    bare = TestClient(app)
    assert bare.get("/api/leads/export.csv?workspace_id=x").status_code in (401, 403)
