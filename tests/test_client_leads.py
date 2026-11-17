"""Ownership: which client is this prospect a lead for.

The bug these tests exist for is not a crash. It is a screen that shows a
prospect, a phone number and a fit score, and never answers the one question the
operator has: *whose outreach is this?* Outreach to a lead is written and signed
as the client it is a lead for, so a lead with no owner is unusable, and a lead
with the wrong owner is worse than unusable - it sends one client's pitch under
another client's name.

Ownership therefore lives in exactly one place, `client_leads`, and these tests
pin the properties that make it trustworthy:

  * one company can be a lead for two clients without becoming two companies
  * a client is never a lead for itself
  * an unattributed business is visible in the Unassigned bucket, never dropped
  * assigning twice does not create a second row
  * assignment never guesses: no fuzzy matching, no inferred owners
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
        conn.execute(f"DELETE FROM client_leads WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM workspace_clients WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM client_leads WHERE business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM client_leads WHERE client_business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM research_leads WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM contacts WHERE business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM businesses WHERE id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM workspaces WHERE id IN ({marks})", ws_ids)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(app, headers=AUTH)


def _workspace(client, slug="client-leads-test"):
    existing = client.get("/api/workspaces").json()
    rows = existing if isinstance(existing, list) else existing.get("items", [])
    for w in rows:
        if w.get("slug") == slug:
            return w["id"]
    r = client.post(
        "/api/workspaces",
        json={
            "name": "Client Leads Test Co",
            "slug": slug,
            "physical_address": "9 Operator Way, Lowell, OH 45744",
        },
    )
    assert r.status_code < 300, r.text
    ws_id = r.json()["id"]
    _CLEAN["ws"].add(ws_id)
    return ws_id


def _biz(conn, name, domain=None, city="Marietta", state="OH", source="manual"):
    bid = new_id()
    now = utcnow()
    conn.execute(
        """INSERT INTO businesses (id, name, city, state, website, domain, source,
                                   created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (bid, name, city, state, f"https://{domain}" if domain else None, domain, source, now, now),
    )
    _CLEAN["biz"].add(bid)
    return bid


def _contact(conn, business_id, email, status="verified", primary=1):
    cid = new_id()
    conn.execute(
        """INSERT INTO contacts (id, business_id, email, email_status, is_primary, created_at)
           VALUES (?,?,?,?,?,?)""",
        (cid, business_id, email, status, primary, utcnow()),
    )
    return cid


def _research(conn, ws, client_bid, name, domain=None, fit=80, subject=None, body=None,
              business_id=None):
    rid = new_id()
    conn.execute(
        """INSERT INTO research_leads (id, workspace_id, client_business_id, business_id,
               name, domain, why_now, signal_kind, fit, email_subject, email_body, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rid, ws, client_bid, business_id, name, domain, "posted a project engineer role",
         "hiring", fit, subject, body, utcnow()),
    )
    return rid


def _owner(conn, ws, client_bid, business_id, fit=75, status="new", origin="research",
           research_lead_id=None):
    lid = new_id()
    now = utcnow()
    conn.execute(
        """INSERT INTO client_leads (id, workspace_id, client_business_id, business_id, fit,
               status, origin, research_lead_id, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (lid, ws, client_bid, business_id, fit, status, origin, research_lead_id, now, now),
    )
    return lid


def _setup(conn, client, slug="client-leads-test"):
    """A workspace with one client, two prospects, and one orphan."""
    ws = _workspace(client, slug)
    # contacts.email is globally unique, so every fixture address is namespaced by
    # slug - otherwise the second test to run collides with the first.
    cl = _biz(conn, "Ridgeline Excavation LLC", f"ridgeline-{slug}.test", source="manual")
    p1 = _biz(conn, "Promanco Builders", f"promanco-{slug}.test")
    p2 = _biz(conn, "Ohio Valley Grading", f"ovg-{slug}.test")
    orphan = _biz(conn, f"Nobody Owns Me {slug}", None)
    _contact(conn, p1, f"pm@{slug}.test", "verified")
    _contact(conn, p2, f"info@{slug}.test", "risky")
    conn.commit()
    return ws, cl, p1, p2, orphan


# --------------------------------------------------------------------------- #
# grouping and attribution
# --------------------------------------------------------------------------- #

def test_a_lead_states_which_client_it_is_for(client):
    """The whole point. A row with no client name is a row the operator cannot use."""
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client)
        _owner(conn, ws, cl, p1, fit=88)
        conn.commit()
    finally:
        conn.close()

    d = client.get("/api/leads", params={"workspace_id": ws}).json()
    assert d["count"] == 1, d
    row = d["items"][0]
    assert row["client_name"] == "Ridgeline Excavation LLC"
    assert row["client_business_id"] == cl
    assert row["name"] == "Promanco Builders"
    assert row["fit"] == 88
    assert [c["name"] for c in d["clients"]] == ["Ridgeline Excavation LLC"]
    assert d["clients"][0]["leads"] == 1


def test_one_prospect_can_be_a_lead_for_two_clients(client):
    """A bank, a GC, a landlord: genuine prospects for more than one client.

    Both clients keep their own fit, status and first-touch draft, while the
    company itself stays one row so contacts and suppression do not fork.
    """
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="cl-two")
        other = _biz(conn, "Cyberal Solutions", "cyberal-cl-two.test")
        _owner(conn, ws, cl, p1, fit=88, status="contacted")
        _owner(conn, ws, other, p1, fit=41, status="new")
        conn.commit()
    finally:
        conn.close()

    # Two rows for one company, each under its own client.
    d = client.get("/api/leads", params={"workspace_id": ws}).json()
    assert d["count"] == 2
    pairs = {(r["client_name"], r["fit"], r["status"]) for r in d["items"]}
    assert pairs == {("Ridgeline Excavation LLC", 88, "contacted"), ("Cyberal Solutions", 41, "new")}
    assert len({r["business_id"] for r in d["items"]}) == 1

    # Filtering by client narrows to that client's relationship only.
    only = client.get("/api/leads", params={"workspace_id": ws, "client_business_id": other}).json()
    assert only["count"] == 1 and only["items"][0]["fit"] == 41


def test_a_client_is_never_listed_as_its_own_lead(client):
    """Research once produced the client itself as a prospect.

    Writing 'we should hire Ridgeline' to Ridgeline is not a lead, so the client
    must not appear in the lead list nor in the unassigned bucket, which is where
    it would otherwise land the moment its business row is created.
    """
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="cl-self-lead")
        _owner(conn, ws, cl, p1)
        conn.commit()
    finally:
        conn.close()

    d = client.get("/api/leads", params={"workspace_id": ws}).json()
    assert cl not in {r["business_id"] for r in d["items"]}

    un = client.get("/api/leads", params={"workspace_id": ws, "bucket": "unassigned"}).json()
    assert cl not in {r["business_id"] for r in un["items"]}


def test_client_tabs_do_not_leak_across_workspaces(client):
    """One workspace's clients must not appear as tabs in another.

    Grouping is only trustworthy if the tab row is that workspace's own client
    book; a stray client from someone else's workspace invites filing a lead
    under the wrong business.
    """
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="cl-scope-a")
        other_ws = _workspace(client, "cl-scope-b")
        far = _biz(conn, "Elsewhere Client", "elsewhere-cl-scope.test", source="manual")
        _owner(conn, other_ws, far, _biz(conn, "Elsewhere Prospect", "ep-cl-scope.test"))
        _owner(conn, ws, cl, p1)
        conn.commit()
    finally:
        conn.close()

    d = client.get("/api/leads", params={"workspace_id": ws}).json()
    names = {c["name"] for c in d["clients"]}
    assert "Ridgeline Excavation LLC" in names
    assert "Elsewhere Client" not in names


def test_an_unowned_business_is_visible_not_dropped(client):
    """Unattributed research is unfinished work, not data to hide.

    Silently excluding it would look clean and lose real prospects; the operator
    sees it in Unassigned with an explicit Assign action instead.
    """
    conn = connect()
    try:
        ws, cl, p1, _, orphan = _setup(conn, client, slug="cl-unowned")
        _owner(conn, ws, cl, p1)
        conn.commit()
    finally:
        conn.close()

    un = client.get("/api/leads", params={"workspace_id": ws, "bucket": "unassigned"}).json()
    ids = {r["business_id"] for r in un["items"]}
    assert orphan in ids
    assert p1 not in ids  # already attributed
    assert un["unassigned_count"] >= 1
    row = next(r for r in un["items"] if r["business_id"] == orphan)
    assert row["client_name"] is None and row["status"] == "unassigned"


# --------------------------------------------------------------------------- #
# assigning
# --------------------------------------------------------------------------- #

def test_assigning_twice_does_not_duplicate_the_lead(client):
    """Operators double-click. The UNIQUE pair, not the UI, is the guarantee."""
    conn = connect()
    try:
        ws, cl, p1, _, orphan = _setup(conn, client, slug="cl-twice")
        conn.commit()
    finally:
        conn.close()

    body = {"workspace_id": ws, "business_id": orphan, "client_business_id": cl}
    first = client.post("/api/leads/assign", json=body)
    assert first.status_code < 300, first.text
    second = client.post("/api/leads/assign", json=body)
    assert second.status_code < 300, second.text
    assert first.json()["id"] == second.json()["id"]

    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM client_leads WHERE business_id = ? AND client_business_id = ?",
            (orphan, cl),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_assigning_keeps_the_evidence_and_the_draft(client):
    """A hand-assigned lead still shows why it is worth contacting.

    The research row is the only record of the signal and of the per-lead email
    the agent wrote, so the assignment must link it rather than start blank.
    """
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="client-leads-evidence")
        rid = _research(conn, ws, cl, "Promanco Builders", "promanco-client-leads-evidence.test", fit=91,
                        subject="Your project engineer posting", body="Hi {{first_name}},")
        conn.commit()
    finally:
        conn.close()

    r = client.post("/api/leads/assign", json={
        "workspace_id": ws, "business_id": p1, "client_business_id": cl,
    })
    assert r.status_code < 300, r.text
    out = r.json()
    assert out["research_lead_id"] == rid
    assert out["fit"] == 91
    assert out["has_first_touch"] == 1
    assert out["first_touch_subject"] == "Your project engineer posting"


def test_a_business_cannot_be_its_own_client(client):
    conn = connect()
    try:
        ws, cl, _, _, _ = _setup(conn, client, slug="client-leads-self")
        conn.commit()
    finally:
        conn.close()

    r = client.post("/api/leads/assign", json={
        "workspace_id": ws, "business_id": cl, "client_business_id": cl,
    })
    assert r.status_code == 422


def test_assignment_refuses_unknown_ids(client):
    """No silent creation of placeholder businesses from a typo'd id."""
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="client-leads-unknown")
        conn.commit()
    finally:
        conn.close()

    assert client.post("/api/leads/assign", json={
        "workspace_id": ws, "business_id": "nope", "client_business_id": cl,
    }).status_code == 404
    assert client.post("/api/leads/assign", json={
        "workspace_id": ws, "business_id": p1, "client_business_id": "nope",
    }).status_code == 404


# --------------------------------------------------------------------------- #
# working the list
# --------------------------------------------------------------------------- #

def test_status_is_restricted_to_the_known_values(client):
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="client-leads-status")
        lid = _owner(conn, ws, cl, p1)
        conn.commit()
    finally:
        conn.close()

    bad = client.patch(f"/api/leads/{lid}", json={"status": "emailed"})
    assert bad.status_code == 422
    good = client.patch(f"/api/leads/{lid}", json={"status": "replied"})
    assert good.status_code < 300, good.text
    assert good.json()["status"] == "replied"


# --------------------------------------------------------------------------- #
# a fresh, empty database
# --------------------------------------------------------------------------- #
# Clients used to be implied by lead rows, so the first legitimate wipe deleted
# every client along with the prospects. That made "start fresh" impossible: the
# clients reappeared as unassigned prospects and outreach had nobody to be signed
# as. A client is a standing relationship and now has its own table.

def _register(client_obj, ws, name, website):
    r = client_obj.post("/api/leads/clients", json={
        "workspace_id": ws, "name": name, "website": website})
    assert r.status_code < 300, r.text
    _CLEAN["biz"].add(r.json()["id"])
    return r.json()


def test_client_survives_with_zero_leads(client):
    conn = connect()
    try:
        ws = _workspace(client, slug="client-leads-empty")
    finally:
        conn.close()

    made = _register(client, ws, "Ridgeline Excavation LLC", "ridgeline-empty.test")

    body = client.get("/api/leads", params={"workspace_id": ws, "limit": 300}).json()
    assert [(c["name"], c["leads"]) for c in body["clients"]] == [("Ridgeline Excavation LLC", 0)]
    assert body["count"] == 0
    # The client must never come back as a prospect to be assigned to itself.
    assert made["id"] not in {r.get("business_id") for r in body["items"]}
    assert made["domain"] == "ridgeline-empty.test"


def test_registering_twice_reuses_the_same_company(client):
    conn = connect()
    try:
        ws = _workspace(client, slug="client-leads-twice")
    finally:
        conn.close()

    first = _register(client, ws, "Ridgeline Excavation LLC", "ridgeline-twice.test")
    # Same company, different spelling of the same site - not a second company.
    second = _register(client, ws, "Ridgeline", "https://www.ridgeline-twice.test/about")
    assert second["id"] == first["id"]

    body = client.get("/api/leads", params={"workspace_id": ws, "limit": 300}).json()
    assert len(body["clients"]) == 1

    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM workspace_clients WHERE workspace_id = ?",
                         (ws,)).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_assigning_a_lead_puts_the_client_on_file(client):
    """Clients created before the registry existed must not stay invisible."""
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="client-leads-register")
        conn.commit()
    finally:
        conn.close()

    r = client.post("/api/leads/assign", json={
        "workspace_id": ws, "business_id": p1, "client_business_id": cl})
    assert r.status_code < 300, r.text

    conn = connect()
    try:
        row = conn.execute(
            "SELECT source FROM workspace_clients WHERE workspace_id = ? AND business_id = ?",
            (ws, cl)).fetchone()
    finally:
        conn.close()
    assert row and row["source"] == "manual"


def test_client_registration_rejects_nonsense(client):
    conn = connect()
    try:
        ws = _workspace(client, slug="client-leads-bad")
    finally:
        conn.close()

    assert client.post("/api/leads/clients", json={
        "workspace_id": ws, "name": "   ", "website": "example.test"}).status_code == 422
    assert client.post("/api/leads/clients", json={
        "workspace_id": ws, "name": "No Site"}).status_code == 422
    assert client.post("/api/leads/clients", json={
        "workspace_id": "nope", "name": "Ghost", "website": "ghost.test"}).status_code == 404


def test_filters_separate_mailable_leads_from_call_only_ones(client):
    """'Verified email' is the mailable list; everything else is phone/LinkedIn work."""
    conn = connect()
    try:
        ws, cl, p1, p2, _ = _setup(conn, client, slug="client-leads-filters")
        _owner(conn, ws, cl, p1)
        _owner(conn, ws, cl, p2)
        conn.commit()
    finally:
        conn.close()

    ok = client.get("/api/leads", params={"workspace_id": ws, "bucket": "sendable"}).json()
    assert {r["name"] for r in ok["items"]} == {"Promanco Builders"}

    none = client.get("/api/leads", params={"workspace_id": ws, "bucket": "no_email"}).json()
    assert none["count"] == 0

    hit = client.get("/api/leads", params={"workspace_id": ws, "q": "ovg"}).json()
    assert {r["name"] for r in hit["items"]} == {"Ohio Valley Grading"}
