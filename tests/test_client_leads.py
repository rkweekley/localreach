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
        conn.execute(f"DELETE FROM workspace_businesses WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM workspace_businesses WHERE business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM client_leads WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM workspace_clients WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM client_leads WHERE business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM client_leads WHERE client_business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM research_leads WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(
            f"DELETE FROM list_members WHERE list_id IN "
            f"(SELECT id FROM lists WHERE workspace_id IN ({marks}))", ws_ids)
        conn.execute(f"DELETE FROM lists WHERE workspace_id IN ({marks})", ws_ids)
        conn.execute(f"DELETE FROM contacts WHERE business_id IN ({bmarks})", biz_ids)
        conn.execute(f"DELETE FROM scores WHERE business_id IN ({bmarks})", biz_ids)
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


def _pool(conn, ws, business_id, origin="import"):
    """Record that this workspace brought the prospect in."""
    conn.execute(
        """INSERT OR IGNORE INTO workspace_businesses (workspace_id, business_id, origin, created_at)
           VALUES (?,?,?,?)""",
        (ws, business_id, origin, utcnow()),
    )
    return business_id


def _setup(conn, client, slug="client-leads-test"):
    """A workspace with one client, two prospects, and one orphan."""
    ws = _workspace(client, slug)
    # contacts.email is globally unique, so every fixture address is namespaced by
    # slug - otherwise the second test to run collides with the first.
    cl = _biz(conn, "Ridgeline Excavation LLC", f"ridgeline-{slug}.test", source="manual")
    p1 = _biz(conn, "Promanco Builders", f"promanco-{slug}.test")
    p2 = _biz(conn, "Ohio Valley Grading", f"ovg-{slug}.test")
    orphan = _biz(conn, f"Nobody Owns Me {slug}", None)
    # Prospects belong to the workspace that imported or researched them.
    for bid in (p1, p2, orphan):
        _pool(conn, ws, bid, origin="research")
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


def test_a_second_workspace_cannot_see_another_workspaces_prospects(client):
    """Pool isolation, not just client isolation.

    A new workspace used to open on the first workspace's imported prospects,
    because "unassigned" meant "nobody claimed it anywhere" rather than "nobody
    claimed it here". That is a data leak with an email button attached: the
    second workspace could research, write to and send on behalf of prospects it
    never imported.
    """
    conn = connect()
    try:
        owner_ws, cl, p1, _, orphan = _setup(conn, client, slug="cl-pool-owner")
        other_ws = _workspace(client, "cl-pool-other")
        conn.commit()
    finally:
        conn.close()

    theirs = client.get("/api/leads", params={"workspace_id": other_ws,
                                              "bucket": "unassigned"}).json()
    ids = {r["business_id"] for r in theirs["items"]}
    assert orphan not in ids
    assert p1 not in ids
    assert theirs["unassigned_count"] == 0

    ours = client.get("/api/leads", params={"workspace_id": owner_ws,
                                            "bucket": "unassigned"}).json()
    assert orphan in {r["business_id"] for r in ours["items"]}


def test_import_records_which_workspace_brought_the_prospect_in(client):
    """The import seam is where ownership is established - otherwise the row is
    invisible to everyone except a single-workspace install."""
    ws = _workspace(client, "cl-pool-import")
    r = client.post(
        "/api/businesses/import",
        params={"workspace_id": ws},
        json=[{"name": "Hometown Roofing", "category": "roofing", "city": "Marietta",
               "state": "OH", "website": "hometownroofing.test"}],
    )
    assert r.status_code < 300, r.text

    listed = client.get("/api/leads", params={"workspace_id": ws,
                                             "bucket": "unassigned"}).json()
    names = {row["name"] for row in listed["items"]}
    assert "Hometown Roofing" in names


def test_scoring_writes_the_fit_the_operator_is_looking_at(client):
    """Score selected used to toast and change nothing.

    The number the Leads table renders is client_leads.fit (research_leads.fit for
    attributed leads), while scoring wrote only to `scores`. Both had to be true
    at once, so the score now lands on the row the screen reads.
    """
    conn = connect()
    try:
        ws, cl, p1, _, orphan = _setup(conn, client, slug="cl-score")
        lid = _owner(conn, ws, cl, p1, fit=-1)  # sentinel: impossible real score
        _contact(conn, orphan, "owner@cl-score.test", "verified")
        conn.commit()
    finally:
        conn.close()

    scored = client.post(
        f"/api/businesses/{p1}/score",
        json={"workspace_id": ws, "client_business_id": cl, "icp_categories": ["excavation"]},
    )
    assert scored.status_code < 300, scored.text
    body = scored.json()
    assert body["assigned_rows_updated"] == 1

    conn = connect()
    try:
        row = conn.execute("SELECT fit FROM client_leads WHERE id = ?", (lid,)).fetchone()
        persisted = conn.execute(
            "SELECT fit_score FROM scores WHERE business_id = ?", (p1,)).fetchone()
    finally:
        conn.close()
    assert row["fit"] == body["fit_score"] != -1
    assert persisted["fit_score"] == body["fit_score"]

    # And the screen reads it back on both buckets.
    listed = client.get("/api/leads", params={"workspace_id": ws}).json()
    mine = next(i for i in listed["items"] if i["business_id"] == p1)
    assert mine["fit"] == body["fit_score"]

    client.post(f"/api/businesses/{orphan}/score", json={"workspace_id": ws})
    pool = client.get("/api/leads", params={"workspace_id": ws, "bucket": "unassigned"}).json()
    unowned = next(i for i in pool["items"] if i["business_id"] == orphan)
    assert unowned["fit"] is not None


def test_scoring_refuses_to_rank_a_client_with_no_icp(client):
    """A score against no target is a shuffle, not a ranking.

    With an empty ICP every prospect landed on the 40-point base and a curated
    fit was overwritten with a lower, meaningless number. The endpoint now says
    what is missing instead of quietly degrading the list.
    """
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="cl-no-icp")
        lid = _owner(conn, ws, cl, p1, fit=88)
        conn.execute("UPDATE businesses SET category = NULL WHERE id = ?", (p1,))
        conn.commit()
    finally:
        conn.close()

    refused = client.post(
        f"/api/businesses/{p1}/score",
        json={"workspace_id": ws, "client_business_id": cl},
    )
    assert refused.status_code == 422, refused.text
    assert "no categorised leads" in refused.json()["detail"].lower()

    conn = connect()
    try:
        fit = conn.execute("SELECT fit FROM client_leads WHERE id = ?", (lid,)).fetchone()["fit"]
    finally:
        conn.close()
    assert fit == 88, "a refused score must not touch the row the operator sees"


def test_unassigned_scoring_uses_the_workspaces_own_targeting(client):
    """Unassigned prospects are still measured against what this shop sells."""
    conn = connect()
    try:
        ws, cl, p1, _, orphan = _setup(conn, client, slug="cl-pool-icp")
        _owner(conn, ws, cl, p1)  # an existing categorised lead is the target
        conn.execute("UPDATE businesses SET category = 'roofing' WHERE id = ?", (p1,))
        conn.execute("UPDATE businesses SET category = 'roofing' WHERE id = ?", (orphan,))
        conn.commit()
    finally:
        conn.close()

    scored = client.post(f"/api/businesses/{orphan}/score", json={"workspace_id": ws})
    assert scored.status_code < 300, scored.text
    assert scored.json()["factors"]["category_match"] == 25


def test_every_score_carries_its_own_explanation(client):
    """A bare number reads as a bug. The base is always a stated factor."""
    conn = connect()
    try:
        ws, cl, p1, _, _ = _setup(conn, client, slug="cl-explain")
        conn.execute("UPDATE businesses SET category = 'excavation' WHERE id = ?", (p1,))
        conn.commit()
    finally:
        conn.close()

    scored = client.post(
        f"/api/businesses/{p1}/score",
        json={"workspace_id": ws, "client_business_id": cl, "icp_categories": ["excavation"]},
    )
    body = scored.json()
    assert body["factors"]["base"] == 40
    assert sum(body["factors"].values()) == body["fit_score"]


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


def test_dashboard_leads_counts_prospects_not_clients(client):
    """The Overview tile said "Leads 2" on a database with zero leads.

    `businesses` is the company registry and every client is in it, so counting
    registry rows makes a completed wipe look like it failed.
    """
    conn = connect()
    try:
        ws = _workspace(client, slug="client-leads-dash")
    finally:
        conn.close()

    _register(client, ws, "Ridgeline Excavation LLC", "ridgeline-dash.test")
    _register(client, ws, "Cyberal Solutions", "cyberal-dash.test")

    counts = client.get("/api/overview", params={"workspace_id": ws}).json()["counts"]
    assert counts["leads"] == 0
    assert counts["clients"] == 2
    assert counts["businesses"] >= 2


def test_dashboard_leads_moves_when_a_prospect_is_filed(client):
    conn = connect()
    try:
        ws = _workspace(client, slug="client-leads-dash2")
        owner = _biz(conn, "Ridgeline Excavation LLC", domain="ridgeline-dash2.test")
        conn.execute(
            "INSERT INTO workspace_clients (id, workspace_id, business_id, status, source, created_at)"
            " VALUES (?, ?, ?, 'active', 'manual', ?)",
            (new_id(), ws, owner, utcnow()),
        )
        prospect = _biz(conn, "Piedmont Residential", domain="piedmont-dash2.test")
        _owner(conn, ws, owner, prospect, origin="research")
        conn.commit()
    finally:
        conn.close()

    counts = client.get("/api/overview", params={"workspace_id": ws}).json()["counts"]
    assert counts["leads"] == 1
    assert counts["clients"] == 1


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


def test_a_saved_list_can_be_read_back_and_filter_the_leads_screen(client):
    """'Save as list' must not be a one-way write.

    It used to be: the POST wrote `lists` and `list_members`, toasted, and the
    list was unreadable forever after - no collection route, no filter, no way to
    get back to the selection a marketer had just built. A saved list that
    cannot be read is not a saved list.
    """
    conn = connect()
    try:
        ws, cl, p1, p2, _ = _setup(conn, client, slug="client-leads-lists")
        _owner(conn, ws, cl, p1)
        _owner(conn, ws, cl, p2)
        conn.commit()
    finally:
        conn.close()
    other = _workspace(client, slug="client-leads-lists-other")

    made = client.post("/api/lists", json={
        "workspace_id": ws, "name": "Q4 tier A", "business_ids": [p1]})
    assert made.status_code < 300, made.text
    lid = made.json()["id"]

    rows = client.get("/api/lists", params={"workspace_id": ws}).json()
    assert [r["id"] for r in rows] == [lid]
    assert rows[0]["name"] == "Q4 tier A"
    assert rows[0]["members"] == 1

    # A list belongs to the workspace that saved it. Another tenant sees nothing.
    assert client.get("/api/lists", params={"workspace_id": other}).json() == []

    only = client.get("/api/leads", params={"workspace_id": ws, "list_id": lid}).json()
    assert [r["business_id"] for r in only["items"]] == [p1]

    # Filtering by someone else's list is a 404, never a peek at their prospects.
    assert client.get("/api/leads", params={
        "workspace_id": other, "list_id": lid}).status_code == 404
