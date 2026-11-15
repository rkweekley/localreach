"""Direction: the researched business is the client, the lead is the prospect.

The failure these tests exist for is a subtle one. Every piece can work — the
research, the lead list, the drafting, the send — and the whole thing can still
be pointed backwards, so that the app emails the company it just researched and
pitches *our* services to them.

The direction this app actually runs in is:

    researched business (client)  ->  its prospects (leads)

so an email to a lead is written *as the client*, signed by the client, with a
CAN-SPAM footer naming the client as the sender, and replies routed to the
client. A lead with no client behind it is the older mode: the workspace is
selling its own services and may speak for itself.

These tests assert who is on the envelope: `from`, `to`, `reply_to`, signature.
"""

import json
import os

import pytest

from app import ai, routes_campaigns, routes_research
from app.config import get_settings
from app.db import connect, init_db, new_id, utcnow
from app.main import app
from app.routes_research import _store_research_leads

AUTH = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', 'test-token')}"}

CLIENT = "Ridgeline Excavation LLC"
CLIENT_SITE = {
    "business_name": CLIENT,
    "what_they_do": "Site prep and excavation for builders in southeastern Ohio.",
    "services": ["managed IT", "grading", "utility trenching"],
    "tone": "plain, no hype",
}


@pytest.fixture(autouse=True)
def _schema():
    init_db()
    yield


_CLEAN = {"ws": set(), "biz": set()}


@pytest.fixture(scope="module", autouse=True)
def _teardown():
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


def _workspace(client, slug="direction-test"):
    existing = client.get("/api/workspaces").json()
    for w in existing if isinstance(existing, list) else existing.get("items", []):
        if w.get("slug") == slug:
            return w["id"]
    r = client.post(
        "/api/workspaces",
        json={
            "name": "Direction Test Co",
            "slug": slug,
            "physical_address": "9 Operator Way, Lowell, OH 45744",
        },
    )
    assert r.status_code < 300, r.text
    ws_id = r.json()["id"]
    _CLEAN["ws"].add(ws_id)
    return ws_id


def _biz(conn, ws, name, domain, research=None, address=None, category=None,
         city=None, state=None):
    bid = new_id()
    addr = address or {}
    conn.execute(
        """INSERT INTO businesses (id, name, website, domain, source, source_ref,
                                   site_research, address1, city, state, postal_code,
                                   category, fetched_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (bid, name, f"https://{domain}", domain, "agent_lead", ws,
         json.dumps(research) if research else None,
         addr.get("address1"), city or addr.get("city"), state or addr.get("state"),
         addr.get("postal_code"), category,
         utcnow(), utcnow(), utcnow()),
    )
    conn.commit()
    _CLEAN["biz"].add(bid)
    return bid


def _contact(conn, bid):
    cid = new_id()
    email = f"owner@{new_id()[:8]}.example"
    conn.execute(
        """INSERT INTO contacts (id, business_id, email, email_status, email_source, verified_at,
                                 is_primary, created_at)
           VALUES (?,?,?,'verified','website',?,1,?)""",
        (cid, bid, email, utcnow(), utcnow()),
    )
    conn.commit()
    return cid, email


def _sequence(conn, ws, client_id=None):
    seq = new_id()
    conn.execute(
        """INSERT INTO sequences (id, workspace_id, name, status, send_window,
                                  client_business_id, created_at)
           VALUES (?,?,?,'active',NULL,?,?)""",
        (seq, ws, "Direction Outreach", client_id, utcnow()),
    )
    conn.execute(
        """INSERT INTO sequence_steps (id, sequence_id, step_order, channel, subject_template,
                                       body_template, delay_days, created_at)
           VALUES (?,?,1,'email','Template subject','Template body {{first_name}}',0,?)""",
        (new_id(), seq, utcnow()),
    )
    conn.commit()
    return seq


def _enroll_row(conn, seq, bid, cid, client_id, subject="Lead first touch", body="Lead body"):
    en = new_id()
    conn.execute(
        """INSERT INTO enrollments (id, sequence_id, business_id, contact_id, status, current_step,
                                    next_send_at, created_at, override_subject, override_body,
                                    client_business_id)
           VALUES (?,?,?,?,'active',0,?,?,?,?,?)""",
        (en, seq, bid, cid, utcnow(), utcnow(), subject, body, client_id),
    )
    conn.commit()
    return en


def _capture(monkeypatch):
    sent = []

    def fake_send(from_email, from_name, to_email, subject, body, **kw):
        sent.append({
            "from_email": from_email,
            "from_name": from_name,
            "to": to_email,
            "subject": subject,
            "body": body,
            "reply_to": kw.get("reply_to"),
        })
        return {"provider_id": "fake-1", "status": "sent", "ok": True}

    monkeypatch.setattr(routes_campaigns.mailer, "send", fake_send)
    return sent


# ------------------------------------------------------------ who signs the mail


def test_the_client_signs_the_email_and_the_lead_receives_it(client, monkeypatch):
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _biz(conn, ws, CLIENT, "ridgeline.example", research=CLIENT_SITE,
                          address={"address1": "100 Main St", "city": "Lowell",
                                   "state": "OH", "postal_code": "45744"})
        lead_bid = _biz(conn, ws, "Promanco", f"promanco-{new_id()[:6]}.example")
        cid, lead_email = _contact(conn, lead_bid)
        seq = _sequence(conn, ws, client_bid)
        _enroll_row(conn, seq, lead_bid, cid, client_bid)
    finally:
        conn.close()

    sent = _capture(monkeypatch)
    results = routes_campaigns.tick(workspace_id=ws)

    assert sent, "tick sent nothing"
    assert sent[0]["to"] == lead_email, "the lead is the recipient"
    assert sent[0]["from_name"] == CLIENT, "the client is the sender, not the lead"
    assert "Promanco" not in (sent[0]["from_name"] or "")
    assert results["results"][0]["as_client"] is True


def test_the_footer_names_the_client_as_the_sender(client, monkeypatch):
    """CAN-SPAM identifies the sender. The sender here is the client."""
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _biz(conn, ws, CLIENT, "ridgeline.example", research=CLIENT_SITE,
                          address={"address1": "100 Main St", "city": "Lowell",
                                   "state": "OH", "postal_code": "45744"})
        lead_bid = _biz(conn, ws, "Promanco", f"promanco-{new_id()[:6]}.example")
        cid, _ = _contact(conn, lead_bid)
        seq = _sequence(conn, ws, client_bid)
        _enroll_row(conn, seq, lead_bid, cid, client_bid, body="Ten minutes this week?")
    finally:
        conn.close()

    sent = _capture(monkeypatch)
    routes_campaigns.tick(workspace_id=ws)

    body = sent[0]["body"]
    assert CLIENT in body, "the client must appear in its own footer"
    assert "100 Main St" in body, "the client's postal address, not ours"
    assert "/u/" in body or "unsubscribe" in body.lower()


def test_the_first_touch_template_is_filled_from_the_lead_at_send_time(client, monkeypatch):
    """One client template, many leads: the placeholders must resolve per lead."""
    ws = _workspace(client, slug="direction-render")
    conn = connect()
    try:
        client_bid = _biz(conn, ws, CLIENT, "ridgeline.example", research=CLIENT_SITE,
                          address={"address1": "100 Main St", "city": "Lowell",
                                   "state": "OH", "postal_code": "45744"})
        lead_bid = _biz(conn, ws, "Promanco", f"promanco-{new_id()[:6]}.example",
                        city="Athens", state="OH")
        cid, _ = _contact(conn, lead_bid)
        seq = _sequence(conn, ws, client_bid)
        _enroll_row(
            conn, seq, lead_bid, cid, client_bid,
            subject="Quick question about {{business_name}}",
            body="Hi {{business_name}}, saw you build around {{city}}.",
        )
    finally:
        conn.close()

    sent = _capture(monkeypatch)
    routes_campaigns.tick(workspace_id=ws)

    assert sent, "tick sent nothing"
    assert sent[0]["subject"] == "Quick question about Promanco"
    assert "Hi Promanco, saw you build around Athens." in sent[0]["body"]
    assert "{{" not in sent[0]["body"], "an unfilled placeholder reached the prospect"


def test_a_lead_with_no_client_is_still_sent_as_the_workspace(client, monkeypatch):
    """The older mode stays: direct prospecting speaks for the workspace."""
    ws = _workspace(client)
    conn = connect()
    try:
        lead_bid = _biz(conn, ws, "Unclaimed Co", f"unclaimed-{new_id()[:6]}.example")
        cid, _ = _contact(conn, lead_bid)
        seq = _sequence(conn, ws, None)
        _enroll_row(conn, seq, lead_bid, cid, None)
    finally:
        conn.close()

    sent = _capture(monkeypatch)
    results = routes_campaigns.tick(workspace_id=ws)

    assert sent, "tick sent nothing"
    assert sent[0]["from_email"] == get_settings().mailgun_sender
    assert sent[0]["from_name"] == (get_settings().mailgun_sender_name or "Direction Test Co")
    assert results["results"][0]["as_client"] is False


def test_a_client_reply_goes_to_the_client(client, monkeypatch):
    """A lead answering the email should reach the client, not us."""
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _biz(conn, ws, CLIENT, "ridgeline.example", research=CLIENT_SITE)
        lead_bid = _biz(conn, ws, "Promanco", f"promanco-{new_id()[:6]}.example")
        cid, _ = _contact(conn, lead_bid)
        seq = _sequence(conn, ws, client_bid)
        conn.execute("UPDATE sequences SET reply_to = ? WHERE id = ?",
                     (f"tj@{new_id()[:5]}.example", seq))
        conn.commit()
        _enroll_row(conn, seq, lead_bid, cid, client_bid)
    finally:
        conn.close()

    sent = _capture(monkeypatch)
    routes_campaigns.tick(workspace_id=ws)

    assert sent[0]["reply_to"], "a client-signed email needs a reply target"
    assert sent[0]["reply_to"] != get_settings().mailgun_sender


# --------------------------------------------------------- the enrollment record


def test_enrolling_a_researched_lead_records_which_client_it_was_for(client):
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _biz(conn, ws, CLIENT, "ridgeline.example", research=CLIENT_SITE)
        lead_bid = _biz(conn, ws, "Promanco", f"promanco-{new_id()[:6]}.example")
        _contact(conn, lead_bid)
        seq = _sequence(conn, ws, None)
        # the research row is what records who this lead was found for
        conn.execute(
            """INSERT INTO research_leads (id, workspace_id, client_business_id, business_id,
                                           name, domain, why_now, signal_kind, fit,
                                           email_subject, email_body, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), ws, client_bid, lead_bid, "Promanco",
             f"promanco-{new_id()[:6]}.example", "Hiring a project engineer", "hiring", 85,
             "Your project engineer posting", "Ten minutes this week?", utcnow()),
        )
        conn.commit()
    finally:
        conn.close()

    r = client.post(f"/api/sequences/{seq}/enroll", json={"business_ids": [lead_bid]})
    assert r.status_code < 300, r.text

    conn = connect()
    try:
        row = conn.execute(
            "SELECT client_business_id FROM enrollments WHERE business_id = ?", (lead_bid,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["client_business_id"] == client_bid


# ------------------------------------------------------------ the draft endpoint


def test_the_draft_writes_as_the_client_and_says_so(client, monkeypatch):
    ws = _workspace(client)
    conn = connect()
    try:
        client_bid = _biz(conn, ws, CLIENT, "ridgeline.example", research=CLIENT_SITE)
        lead_bid = _biz(conn, ws, "Promanco", f"promanco-{new_id()[:6]}.example",
                        research={"business_name": "Promanco", "what_they_do": "General contractor"})
        _contact(conn, lead_bid)
        conn.execute(
            """INSERT INTO research_leads (id, workspace_id, client_business_id, business_id,
                                           name, domain, why_now, signal_kind, fit, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), ws, client_bid, lead_bid, "Promanco", f"promanco-{new_id()[:6]}.example",
             "Hiring a project engineer", "hiring", 85, utcnow()),
        )
        conn.commit()
    finally:
        conn.close()

    captured = {}

    def fake_draft(profile, business, contact=None, brief="", length=100, signature=""):
        captured.update({"seller": profile, "signature": signature, "business": business})
        return {"subject": "Your project engineer posting", "body": "Body text"}

    monkeypatch.setattr(ai, "draft_email", fake_draft)

    r = client.post(
        "/api/research/email",
        json={"workspace_id": ws, "business_id": lead_bid},
    )
    assert r.status_code < 300, r.text
    d = r.json()

    assert d["direction"] == "client_to_lead"
    assert d["as_client"] == CLIENT
    assert d["business_name"] == "Promanco"
    assert captured["signature"] == CLIENT, "signed by the client, not by us"
    offer = (captured["seller"] or {}).get("offer") or ""
    assert "managed IT" in offer, "the model must pitch the client's services, not ours"


def test_a_broken_research_cache_does_not_steal_the_voice():
    """Bad JSON must fall back, never crash a send."""
    voice = ai.voice_from_research("not a dict", {"offer": "our own services"})
    assert voice["offer"] == "our own services"


def test_voice_from_research_reads_the_client_profile():
    voice = ai.voice_from_research(CLIENT_SITE, {})
    assert "managed IT" in voice["offer"]
    assert voice["tone"] == "plain, no hype"
    assert voice["name"] == CLIENT


# ------------------------------------------------- the lead pull finds the buyers

SELLER_PROFILE = {
    "business_name": CLIENT,
    "name": CLIENT,
    "domain": "ridgeline-buyers.example",
    "industry": "Excavation Contractor",
    "what_they_do": "Site prep and excavation for builders in southeastern Ohio.",
    "services": ["grading", "utility trenching", "site prep"],
    "icp": "general contractors and land developers in southeastern Ohio",
    "customers": "general contractors, land developers",
}


def test_the_lead_pull_finds_the_buyers_not_the_rivals(client, monkeypatch):
    """Matching the client's own trade returns its competitors — backwards."""
    ws = _workspace(client, slug="direction-buyers")
    conn = connect()
    try:
        _biz(conn, ws, CLIENT, SELLER_PROFILE["domain"], research=SELLER_PROFILE,
             category="Excavation Contractor")
        buyer = _biz(conn, ws, "Buckeye Builders", f"buckeye-{new_id()[:6]}.example",
                     category="General Contractor", city="Lowell", state="OH")
        buyer2 = _biz(conn, ws, "Hocking Land Co", f"hocking-{new_id()[:6]}.example",
                      category="Land Developer", city="Lowell", state="OH")
        rival = _biz(conn, ws, "Tri-State Digging", f"tristate-{new_id()[:6]}.example",
                     category="Excavation Contractor", city="Lowell", state="OH")
        monkeypatch.setattr(routes_research.research, "research",
                            lambda url, deep=False: SELLER_PROFILE)
        r = client.post("/api/research/leads", json={
            "workspace_id": ws, "url": f"https://{SELLER_PROFILE['domain']}", "limit": 25})
    finally:
        conn.close()

    assert r.status_code < 300, r.text
    d = r.json()
    got = {row["name"] for row in d["results"]}
    assert "Excavation Contractor" not in d["filters"]["categories"], \
        "the client's own trade is a rival list, not a lead list"
    assert "Tri-State Digging" not in got, "a competitor is not a prospect"
    assert got == {"Buckeye Builders", "Hocking Land Co"}, got
    assert "seller" in d["direction_note"]


def test_a_lead_with_no_note_inherits_the_clients_first_touch_template(client):
    ws = _workspace(client, slug="direction-template")
    conn = connect()
    try:
        client_bid = _biz(conn, ws, CLIENT, "ridgeline-tpl.example", research=CLIENT_SITE)
        template = {
            "subject": "Quick question about {{business_name}}",
            "body": "Hi {{business_name}}, saw you build around {{city}}. Worth a call?",
        }
        profile = {"business_name": CLIENT, "leads": [{
            "name": "Buckeye Builders", "domain": "buckeye-tpl.example",
            "signal_kind": "hiring", "signal_detail": "Hiring a site foreman",
            "signal_source": "https://buckeye-tpl.example/careers",
        }]}
        _store_research_leads(conn, ws, None, client_bid, profile, template)
        row = conn.execute(
            "SELECT email_subject, email_body FROM research_leads "
            "WHERE workspace_id = ? AND domain = ?",
            (ws, "buckeye-tpl.example"),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["email_subject"] == template["subject"]
    assert "{{business_name}}" in row["email_body"], \
        "placeholders must survive to send time, where they fill from the lead"
