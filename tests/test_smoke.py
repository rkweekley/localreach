"""Smoke tests: the guarantees that matter, not coverage theatre."""
from __future__ import annotations

import hashlib
import hmac
import os
import time

import pytest
from fastapi.testclient import TestClient

from app import ai, compliance
from app.db import connect, init_db, new_id, utcnow
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)


def _signed(payload: dict) -> dict:
    """Mailgun posts its own HMAC; the test has to forge it the same way."""
    ts = str(int(time.time()))
    tok = "test-webhook-token"
    sig = hmac.new(
        os.environ["MAILGUN_SIGNING_KEY"].encode(),
        f"{ts}{tok}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {**payload, "timestamp": ts, "token": tok, "signature": sig}


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()
    ws = c.post("/api/workspaces", json={
        "name": "Test Co", "slug": "test-co", "physical_address": "1 Main St, Lisbon OH",
    }).json()
    yield ws


def test_health():
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["dry_run"] is True


def test_unverified_and_role_addresses_are_blocked():
    assert not compliance.check_send("ws", "a@b.com", "unverified", "s@me.com", 100)
    assert compliance.check_send("ws", "a@b.com", "unverified", "s@me.com", 100).reason == "unverified"
    assert not compliance.check_send("ws", "info@b.com", "verified", "s@me.com", 100)
    assert compliance.check_send("ws", "info@b.com", "verified", "s@me.com", 100).reason == "role_address"
    assert compliance.check_send("ws", "owner@b.com", "verified", "s@me.com", 100)


def test_daily_cap_is_enforced():
    ws = c.post("/api/workspaces", json={
        "name": "Cap Co", "slug": "cap-co", "physical_address": "2 Main St", "daily_send_cap": 1,
    }).json()
    conn = connect()
    conn.execute(
        """INSERT INTO messages (id,workspace_id,direction,from_email,to_email,sent_at,created_at)
           VALUES (?,?,'outbound',?,?,?,?)""",
        (new_id(), ws["id"], "s@me.com", "x@y.com", utcnow(), utcnow()),
    )
    conn.commit()
    conn.close()
    v = compliance.check_send(ws["id"], "owner@b.com", "verified", "s@me.com", 1)
    assert not v and v.reason == "daily_cap"


def test_import_search_and_score():
    ws = c.post("/api/workspaces", json={
        "name": "Search Co", "slug": "search-co", "physical_address": "3 Main St",
    }).json()
    r = c.post("/api/businesses/import", json=[{
        "name": "Zed Garage", "category": "garage_door_repair", "city": "Columbus", "state": "OH",
        "rating": 4.8, "review_count": 150, "years_in_business": 9, "source": "osm",
        "source_ref": "t:1", "contact_email": "zed@zedgarage.com", "contact_name": "Zed",
        "contact_title": "Owner", "email_status": "verified",
    }]).json()
    assert r["created"] == 1
    found = c.get("/api/search", params={"workspace_id": ws["id"], "city": "Columbus"}).json()
    assert found["count"] == 1
    bid = found["results"][0]["id"]
    scored = c.post(f"/api/businesses/{bid}/score", json={"icp_categories": ["garage_door_repair"]}).json()
    assert scored["fit_score"] > 60
    assert scored["reach_score"] == 100  # verified + owner title


def test_reply_classifier():
    assert ai.classify_reply("Re: hi", "Please unsubscribe me")[0] == "unsubscribe"
    assert ai.classify_reply("Re: hi", "Not interested, thanks")[0] == "not_interested"
    assert ai.classify_reply("Re: hi", "Wrong person, contact our ops manager")[0] == "wrong_person"
    assert ai.classify_reply("Re: hi", "What do you charge?")[0] == "interested"


def test_snippet_falls_back_without_llm(monkeypatch):
    monkeypatch.delenv("AI_BASE_URL", raising=False)
    out = ai.generate_snippet(
        {"offer": "Flat-rate IT", "banned_phrases": '["hope this finds you well"]'},
        {"name": "Buckeye Garage Door", "city": "Columbus", "rating": 4.7, "review_count": 212},
        {"full_name": "Dana Whitfield"}, "", 100)
    assert out["used_llm"] is False
    assert "Columbus" in out["text"]
    assert not out["text"].lower().startswith("hi ")   # greeting belongs to the template
    assert "hope this finds you well" not in out["text"].lower()


def test_inbound_reply_stops_sequence_and_suppresses():
    ws = c.post("/api/workspaces", json={
        "name": "Reply Co", "slug": "reply-co", "physical_address": "4 Main St",
    }).json()
    c.post("/api/businesses/import", json=[{
        "name": "Reply Garage", "category": "garage_door_repair", "city": "Columbus", "state": "OH",
        "source": "osm", "source_ref": "t:2", "contact_email": "owner@replygarage.com",
        "contact_name": "Ann", "email_status": "verified",
    }])
    conn = connect()
    bid = conn.execute("SELECT id FROM businesses WHERE name='Reply Garage'").fetchone()["id"]
    conn.close()
    seq = c.post("/api/sequences", json={
        "workspace_id": ws["id"], "name": "R1",
        "steps": [{"step_order": 1, "channel": "email", "delay_days": 0,
                   "subject_template": "Hi", "body_template": "Body"}],
    }).json()
    c.post(f"/api/sequences/{seq['id']}/activate")
    c.post(f"/api/sequences/{seq['id']}/enroll", json={"business_ids": [bid]})

    r = c.post("/webhooks/mailgun/inbound", json=_signed({
        "sender": "owner@replygarage.com", "subject": "Re: Hi",
        "stripped-text": "Take me off your list.",
    })).json()
    assert r["intent"] == "unsubscribe"
    assert r["sequences_stopped"] == 1
    assert compliance.suppressed(ws["id"], "owner@replygarage.com") == "unsubscribe"
    # and the gate now refuses that address
    assert compliance.check_send(ws["id"], "owner@replygarage.com", "verified", "s@me.com", 100).reason == "suppressed"


def test_one_click_unsubscribe_token():
    tok = compliance.unsubscribe_token("ws1", "A@B.com")
    assert compliance.verify_token("ws1", "a@b.com", tok)
    assert not compliance.verify_token("ws2", "a@b.com", tok)
    assert not compliance.verify_token("ws1", "a@b.com", tok[:-1] + "0")
    # the address travels inside the token, which is what makes one-click work
    assert compliance.token_email(tok) == "a@b.com"


def test_unsubscribe_link_needs_only_one_click_and_no_body():
    ws = c.post("/api/workspaces", json={
        "name": "Unsub Co", "slug": "unsub-co", "physical_address": "6 Main St",
    }).json()
    c.post("/api/businesses/import", json=[{
        "name": "Unsub Garage", "category": "garage_door_repair", "city": "Columbus",
        "state": "OH", "source": "osm", "source_ref": "t:9",
        "contact_email": "quit@unsubco.com", "contact_name": "Q", "email_status": "verified",
    }])
    tok = compliance.unsubscribe_token(ws["id"], "quit@unsubco.com")

    page = c.get(f"/u/{tok}")
    assert page.status_code == 200 and "quit@unsubco.com" in page.text
    # GET must never opt anyone out: mail scanners and providers prefetch links.
    assert not compliance.suppressed(ws["id"], "quit@unsubco.com")

    r = c.post(f"/u/{tok}")  # RFC 8058 one-click: empty body, no form fields
    assert r.status_code == 200 and r.json()["applied"] >= 1
    assert compliance.suppressed(ws["id"], "quit@unsubco.com") == "unsubscribed"
    assert compliance.check_send(
        ws["id"], "quit@unsubco.com", "verified", "info@cyberalsolutions.com", 100
    ).reason == "suppressed"


def test_webhooks_reject_unsigned_and_forged_requests():
    forged = {"sender": "victim@target.com", "subject": "x", "stripped-text": "remove me"}
    # no signature at all
    assert c.post("/webhooks/mailgun/inbound", json=forged).status_code == 401
    # correct shape, wrong digest
    bad = _signed(forged)
    bad["signature"] = "0" * 64
    assert c.post("/webhooks/mailgun/inbound", json=bad).status_code == 401
    # a captured request replayed much later
    stale = _signed(forged)
    stale["timestamp"] = str(int(time.time()) - 7200)
    assert c.post("/webhooks/mailgun/inbound", json=stale).status_code == 401
    assert not compliance.suppressed("", "victim@target.com")


def test_api_requires_the_bearer_token():
    from fastapi.testclient import TestClient
    from app.main import app

    anon = TestClient(app)
    assert anon.get("/api/search", params={"city": "Columbus"}).status_code == 401
    assert anon.post("/api/workspaces", json={"name": "X", "slug": "x"}).status_code == 401
    assert anon.get("/health").status_code == 200


def test_send_windows_are_enforced():
    from datetime import datetime, timezone
    from app import schedule

    assert schedule.is_open("always", "America/New_York")
    # 04:00 UTC = 23:00 the previous evening in New York: outside 9-17
    late = datetime(2026, 11, 13, 4, 0, tzinfo=timezone.utc)
    assert not schedule.is_open("daily_9_17", "America/New_York", now=late)
    noon_et = datetime(2026, 11, 13, 17, 0, tzinfo=timezone.utc)
    assert schedule.is_open("daily_9_17", "America/New_York", now=noon_et)
    # a broken spec must not silence the worker
    assert schedule.is_open("garbage", "America/New_York")
    assert schedule.next_open("daily_9_17", "America/New_York", now=late) is not None


def test_agent_builds_a_draft_campaign():
    ws = c.post("/api/workspaces", json={
        "name": "Agent Co", "slug": "agent-co", "physical_address": "5 Main St",
    }).json()
    c.post("/api/businesses/import", json=[{
        "name": "Agent Garage", "category": "garage_door_repair", "city": "Columbus", "state": "OH",
        "source": "osm", "source_ref": "t:3", "contact_email": "o@agentgarage.com",
        "contact_name": "Al", "email_status": "verified",
    }])
    out = c.post("/api/agent/email-in", json={
        "workspace_id": ws["id"], "from_email": "ryan@cyberalsolutions.com",
        "subject": "", "body": "find garage door repair in Columbus",
    }).json()
    assert out["action"] == "campaign_drafted"
    assert out["status"] == "draft"          # never auto-sends
    assert out["mailable"] >= 1


def test_root_is_a_service_identity_not_a_404():
    r = TestClient(app).get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "LocalReach"
    assert body["status"] == "ok"


def test_creating_the_same_workspace_twice_is_idempotent():
    """A re-run deploy probe must not 500 on a UNIQUE constraint."""
    payload = {"name": "Idem Co", "slug": "idem-co", "physical_address": "9 Main St"}
    first = c.post("/api/workspaces", json=payload)
    second = c.post("/api/workspaces", json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["existed"] is False
    assert second.json()["existed"] is True
    assert first.json()["id"] == second.json()["id"]
