"""Verification must be honest, boring, and impossible to fake.

The failure mode that matters is not "a bad address got through" — it is "a good
address was marked invalid during a DNS blip", or "verified stopped meaning
anything because we marked everything verified". These tests pin both.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import compliance, verify
from app.db import connect, init_db, new_id, utcnow
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)


@pytest.fixture(scope="module", autouse=True)
def _schema() -> None:
    init_db()


# --- a fake resolver: (record_type) -> (status, answers) -------------------

def _fake_dns(zone: dict[str, dict[str, tuple[int | None, list[str]]]]):
    def _doh(name: str, rtype: str):
        entry = zone.get(name.lower(), {})
        if rtype not in entry:
            return 0, []
        return entry[rtype]

    return _doh


LIVE = _fake_dns({
    "live-co.com": {"MX": (0, ["10 mx1.emailsrvr.com."]), "TXT": (0, ['"v=spf1 include:mailgun.org ~all"'])},
    "_dmarc.live-co.com": {"TXT": (0, ['"v=DMARC1; p=none"'])},
    "implicit-co.com": {"A": (0, ["93.184.216.34"])},
    "dead-co.com": {"MX": (3, []), "A": (3, [])},
})

OFFLINE = _fake_dns({})  # every lookup fails -> the resolver returns (None, [])
OFFLINE = lambda name, rtype: (None, [])


def test_bad_syntax_is_invalid(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    assert verify.verify_address("not-an-address")["status"] == "invalid"
    assert verify.verify_address("ryan@")["status"] == "invalid"


def test_disposable_is_invalid(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    res = verify.verify_address("throwaway@mailinator.com")
    assert res["status"] == "invalid" and res["reason"] == "disposable"


def test_live_domain_verified_when_read_off_a_page(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    res = verify.verify_address("tj@live-co.com", source="website")
    assert res["status"] == "verified"
    assert res["detail"]["mx"] == ["mx1.emailsrvr.com"]
    assert res["detail"]["spf"] is True and res["detail"]["dmarc"] is True


def test_role_mailbox_is_deliverable_but_flagged(monkeypatch):
    """`info@` is a real mailbox. Calling it invalid would be a lie; the send gate
    is where role addresses get refused."""
    monkeypatch.setattr(verify, "_doh", LIVE)
    res = verify.verify_address("info@live-co.com", source="website")
    assert res["status"] == "verified"
    assert res["detail"]["role"] is True
    verdict = compliance.check_send("ws-test", "info@live-co.com", "verified",
                                     "info@cyberalsolutions.com", cap=40)
    assert not verdict.allowed and verdict.reason == "role_address"


def test_guessed_pattern_stays_risky(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    res = verify.verify_address("first.last@live-co.com", source="guessed")
    assert res["status"] == "risky" and res["reason"] == "guessed_pattern"


def test_no_mx_but_a_record_is_risky_not_verified(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    assert verify.verify_address("a@implicit-co.com")["status"] == "risky"


def test_dead_domain_is_invalid(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    assert verify.verify_address("a@dead-co.com")["status"] == "invalid"


def test_dns_outage_never_marks_an_address_bad(monkeypatch):
    monkeypatch.setattr(verify, "_doh", OFFLINE)
    res = verify.verify_address("tj@live-co.com")
    assert res["status"] == "unverified" and res["reason"] == "dns_unavailable"


def test_risky_is_not_sendable():
    assert verify.SENDABLE == {"verified"}
    verdict = compliance.check_send("ws-test", "a@live-co.com", "risky",
                                    "info@cyberalsolutions.com", cap=40)
    assert not verdict.allowed


# --- database behaviour ----------------------------------------------------

def _workspace_id() -> str:
    """Reuse whatever workspace exists, else create one through the real route."""
    conn = connect()
    try:
        ws = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()
    finally:
        conn.close()
    if ws:
        return ws["id"]
    r = c.post("/api/workspaces", json={
        "name": "Verify Co", "slug": "verify-co",
        "physical_address": "100 Main St, Lowell, OH 45744",
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _business(name: str, domain: str) -> str:
    conn = connect()
    try:
        bid = new_id()
        conn.execute(
            "INSERT INTO businesses (id, name, domain, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (bid, name, domain, "manual", utcnow(), utcnow()),
        )
        conn.commit()
        return bid
    finally:
        conn.close()


def _seed_contact(email: str, source: str = "website") -> tuple[str, str]:
    """Returns (prospect_business_id, contact_id).

    The contact hangs off the *prospect* business — the company we would mail —
    so a join from research_leads.business_id can find it. Attaching it to the
    researched business would be the classic off-by-one-direction bug.
    """
    _workspace_id()
    conn = connect()
    try:
        bid = new_id()
        conn.execute(
            "INSERT INTO businesses (id, name, domain, source, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (bid, "Live Co", "live-co.com", "manual", utcnow(), utcnow()),
        )
        cid = new_id()
        conn.execute(
            """INSERT INTO contacts (id, business_id, email, email_status, email_source, created_at)
               VALUES (?,?,?, 'unverified', ?, ?)""",
            (cid, bid, email, source, utcnow()),
        )
        conn.commit()
        return bid, cid
    finally:
        conn.close()


def test_apply_verdict_writes_status_and_evidence(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    _, cid = _seed_contact("owner@live-co.com")
    conn = connect()
    try:
        res = verify.verify_contact(conn, cid)
        conn.commit()
        assert res["email_status"] == "verified"
        row = conn.execute("SELECT email_status, verified_at, verify_detail FROM contacts WHERE id = ?",
                           (cid,)).fetchone()
    finally:
        conn.close()
    assert row["email_status"] == "verified" and row["verified_at"]
    assert "mx1.emailsrvr.com" in (row["verify_detail"] or "")


def test_a_dns_failure_cannot_downgrade_a_verified_contact(monkeypatch):
    """Re-running verification during an outage must not cost you your list."""
    monkeypatch.setattr(verify, "_doh", LIVE)
    _, cid = _seed_contact("owner2@live-co.com")
    conn = connect()
    try:
        verify.verify_contact(conn, cid)
        conn.commit()
        monkeypatch.setattr(verify, "_doh", OFFLINE)
        res = verify.verify_contact(conn, cid)
        conn.commit()
        assert res["updated"] is False and res["reason"] == "kept_verified"
    finally:
        conn.close()


def test_route_verifies_one_contact(monkeypatch):
    monkeypatch.setattr(verify, "_doh", LIVE)
    _, cid = _seed_contact("hello@live-co.com")
    r = c.post(f"/api/contacts/{cid}/verify")
    assert r.status_code == 200, r.text
    assert r.json()["email_status"] == "verified"


def test_route_verifies_a_lead_batch(monkeypatch):
    """A research run's contacts are all reachable from one call, and the batch
    skips anything already proven so a second click is cheap."""
    monkeypatch.setattr(verify, "_doh", LIVE)
    ws_id = _workspace_id()
    prospect_bid, contact_id = _seed_contact("lead-owner@live-co.com")
    client_bid = _business("Cyberal Solutions", "cyberalsolutions.com")
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO research_leads (id, workspace_id, client_business_id, business_id, name,
                                           domain, signal_kind, signal_source, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (new_id(), ws_id, client_bid, prospect_bid, "Live Co", "live-co.com",
             "hiring", "https://example.com/jobs", utcnow()),
        )
        conn.commit()
    finally:
        conn.close()

    r = c.post("/api/research/leads/verify", json={"client_business_id": client_bid, "limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"].get("verified") == 1
    assert any(x.get("contact_id") == contact_id for x in body["results"])

    again = c.post("/api/research/leads/verify", json={"client_business_id": client_bid, "limit": 10})
    assert again.json()["checked"] == 0  # already verified, so nothing is re-hit
