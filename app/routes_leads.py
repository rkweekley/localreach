"""The Leads surface: one row per prospect-per-client relationship.

Every other route in this app grew up around a single seller ("which local
businesses would buy from me?"). This one answers the agency question instead:
**which client is each prospect a lead for.**

Ownership is read from `client_leads` and nowhere else. It used to be encoded
three ways - `research_leads.client_business_id`, a list named "Leads for X", and
`businesses.source_ref` - and the Leads tab read `/api/search`, which has no
client concept at all. That is why the screen showed who the prospect was but
never who they were being sold to.

`businesses` stays a pure company registry: the same company can be a lead for
two clients while remaining one row, so contacts and suppression do not fork.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from . import export
from .db import connect, new_id, utcnow

router = APIRouter(prefix="/api/leads", tags=["leads"])

STATUSES = {"new", "contacted", "replied", "meeting", "won", "lost", "unfit"}

# A lead row is joined to its client, the newest evidence that put it there, and
# the primary contact. Nothing here decides whether email may be sent - that is
# still the send gate's job; `email_status` is shown as-is so the operator sees
# what the gate will see.
_SELECT = """
SELECT cl.id, cl.client_business_id, cb.name AS client_name, cb.domain AS client_domain,
       cl.business_id, b.name, b.category, b.city, b.state, b.domain, b.website, b.phone,
       COALESCE(cl.fit, rl.fit) AS fit, cl.status, cl.origin,
       cl.research_lead_id, cl.assigned_by, cl.created_at,
       rl.why_now, rl.signal_kind, rl.signal_detail, rl.signal_source, rl.contact_hint,
       rl.audit_score, rl.email_subject AS first_touch_subject, rl.email_body AS first_touch_body,
       ct.email, ct.email_status, ct.full_name AS contact_name,
       CASE WHEN COALESCE(rl.email_subject, '') <> '' OR COALESCE(rl.email_body, '') <> ''
            THEN 1 ELSE 0 END AS has_first_touch
  FROM client_leads cl
  JOIN businesses cb ON cb.id = cl.client_business_id
  JOIN businesses b  ON b.id  = cl.business_id
  LEFT JOIN research_leads rl ON rl.id = cl.research_lead_id
  LEFT JOIN contacts ct ON ct.business_id = b.id AND ct.is_primary = 1
 WHERE cl.workspace_id = ?
"""

# Businesses nobody owns. Excluded: businesses that are somebody's client (a
# prospect list must never contain the clients themselves) and rows already
# attributed anywhere.
_UNASSIGNED = """
SELECT NULL AS id, NULL AS client_business_id, NULL AS client_name, NULL AS client_domain,
       b.id AS business_id, b.name, b.category, b.city, b.state, b.domain, b.website, b.phone,
       NULL AS fit, 'unassigned' AS status, b.source AS origin,
       NULL AS research_lead_id, NULL AS assigned_by, b.created_at,
       NULL AS why_now, NULL AS signal_kind, NULL AS signal_detail, NULL AS signal_source,
       NULL AS contact_hint, NULL AS audit_score,
       NULL AS first_touch_subject, NULL AS first_touch_body,
       ct.email, ct.email_status, ct.full_name AS contact_name, 0 AS has_first_touch
  FROM businesses b
  LEFT JOIN contacts ct ON ct.business_id = b.id AND ct.is_primary = 1
 WHERE b.id NOT IN (SELECT business_id FROM client_leads)
   AND b.id NOT IN (SELECT client_business_id FROM client_leads)
   AND b.id NOT IN (SELECT business_id FROM workspace_clients)
   AND b.id NOT IN (SELECT client_business_id FROM research_leads
                     WHERE client_business_id IS NOT NULL)
"""


def _register_client(conn, workspace_id: str, business_id: str, source: str) -> None:
    """Record a standing client relationship. Idempotent."""
    conn.execute(
        """INSERT OR IGNORE INTO workspace_clients
               (id, workspace_id, business_id, status, source, created_at)
           VALUES (?,?,?,'active',?,?)""",
        (new_id(), workspace_id, business_id, source, utcnow()),
    )


def _clients(conn, workspace_id: str) -> list[dict[str, Any]]:
    """Clients the operator can file a lead under, with their live lead counts.

    A client exists because `workspace_clients` says so - not because some lead
    row happens to point at it. Legacy evidence (`client_leads`,
    `research_leads`) is unioned in so installs that predate the registry still
    list their clients, but a client with zero leads now stays listed at 0.
    """
    counts = {r["client_business_id"]: r["n"] for r in conn.execute(
        "SELECT client_business_id, COUNT(*) AS n FROM client_leads WHERE workspace_id = ? GROUP BY 1",
        (workspace_id,),
    )}
    rows = conn.execute(
        """SELECT b.id, b.name, b.domain FROM businesses b
            WHERE b.id IN (SELECT business_id FROM workspace_clients
                            WHERE workspace_id = ? AND status = 'active')
               OR b.id IN (SELECT client_business_id FROM client_leads WHERE workspace_id = ?)
               OR b.id IN (SELECT client_business_id FROM research_leads
                            WHERE client_business_id IS NOT NULL AND workspace_id = ?)
            ORDER BY b.name""",
        (workspace_id, workspace_id, workspace_id),
    )
    return [{"id": r["id"], "name": r["name"], "domain": r["domain"],
             "leads": counts.get(r["id"], 0)} for r in rows]


@router.get("")
def list_leads(
    workspace_id: str,
    client_business_id: str | None = None,
    bucket: str = "all",
    q: str | None = None,
    limit: int = 300,
) -> dict:
    conn = connect()
    try:
        items: list[dict[str, Any]]
        if bucket == "unassigned":
            items = [dict(r) for r in conn.execute(_UNASSIGNED + " ORDER BY b.name LIMIT ?", (limit,))]
        else:
            sql, args = _SELECT, [workspace_id]
            if client_business_id:
                sql += " AND cl.client_business_id = ?"
                args.append(client_business_id)
            if q and q.strip():
                like = f"%{q.strip()}%"
                sql += " AND (b.name LIKE ? OR COALESCE(b.domain, '') LIKE ?)"
                args += [like, like]
            if bucket == "sendable":
                sql += " AND ct.email_status = 'verified'"
            elif bucket == "no_email":
                sql += " AND (ct.email IS NULL OR ct.email = '')"
            sql += (" ORDER BY cl.client_business_id,"
                    " COALESCE(cl.fit, rl.fit, 0) DESC, b.name LIMIT ?")
            args.append(limit)
            items = [dict(r) for r in conn.execute(sql, args)]

        unassigned = conn.execute("SELECT COUNT(*) AS n FROM (" + _UNASSIGNED + ")").fetchone()["n"]
        return {
            "items": items,
            "count": len(items),
            "client_business_id": client_business_id,
            "bucket": bucket,
            "clients": _clients(conn, workspace_id),
            "unassigned_count": unassigned,
        }
    finally:
        conn.close()


@router.get("/export.csv")
def export_csv(workspace_id: str) -> Response:
    """Every lead with its drafted email, as a CSV built for pasting by hand.

    Declared before the `/{lead_id}` routes so a path segment can never be read
    as an id. The body column carries the finished email *including* the CAN-SPAM
    footer and the tracked unsubscribe link, because the mailer is what normally
    adds those and it is not in the loop when a human copies the text.
    """
    conn = connect()
    try:
        text = export.leads_csv(conn, workspace_id)
    except KeyError:
        raise HTTPException(404, "unknown workspace")
    finally:
        conn.close()
    return Response(
        content="\ufeff" + text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="localreach-leads.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/{lead_id}/email")
def lead_email(lead_id: str, workspace_id: str) -> dict:
    """One lead's email, ready to copy: recipient, subject, body, footer, warnings."""
    conn = connect()
    try:
        item = export.lead_email(conn, workspace_id, lead_id)
    except KeyError:
        raise HTTPException(404, "unknown workspace")
    finally:
        conn.close()
    if not item:
        raise HTTPException(404, "no such lead in this workspace")
    return item


class ClientIn(BaseModel):
    workspace_id: str
    business_id: str | None = None
    name: str | None = None
    website: str | None = None


@router.post("/clients")
def add_client(body: ClientIn) -> dict:
    """Register a client so the Leads tab has somewhere to file prospects.

    Takes either an existing `business_id` or a name + website. Registering is a
    relationship, not research: no leads are created, so this is safe to run on a
    freshly wiped database. Research on the client's site registers it too.
    """
    from .research import ResearchError, normalize_url

    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone():
            raise HTTPException(404, "workspace not found")

        if body.business_id:
            biz = conn.execute(
                "SELECT id, name, domain FROM businesses WHERE id = ?", (body.business_id,)
            ).fetchone()
            if not biz:
                raise HTTPException(404, "business not found")
            bid = biz["id"]
        else:
            name = (body.name or "").strip()
            if not name:
                raise HTTPException(422, "a client needs a name")
            try:
                url, domain = normalize_url(body.website or "")
            except ResearchError as exc:
                raise HTTPException(422, str(exc)) from exc
            # 'www.' is a spelling of the same site, not a different company.
            if domain.startswith("www."):
                domain = domain[4:]
            biz = conn.execute(
                "SELECT id, name, domain FROM businesses WHERE lower(trim(domain)) = ? ORDER BY created_at LIMIT 1",
                (domain,),
            ).fetchone()
            if biz:
                bid = biz["id"]
            else:
                bid = new_id()
                now = utcnow()
                conn.execute(
                    """INSERT INTO businesses (id, name, domain, website, source, created_at, updated_at)
                       VALUES (?,?,?,?,'manual',?,?)""",
                    (bid, name, domain, url, now, now),
                )
        _register_client(conn, body.workspace_id, bid, "manual")
        conn.commit()
        row = conn.execute("SELECT id, name, domain FROM businesses WHERE id = ?", (bid,)).fetchone()
        leads = conn.execute(
            "SELECT COUNT(*) AS n FROM client_leads WHERE workspace_id = ? AND client_business_id = ?",
            (body.workspace_id, bid),
        ).fetchone()["n"]
        return {"id": row["id"], "name": row["name"], "domain": row["domain"], "leads": leads}
    finally:
        conn.close()


class AssignIn(BaseModel):
    workspace_id: str
    business_id: str
    client_business_id: str
    assigned_by: str | None = None


@router.post("/assign")
def assign(body: AssignIn) -> dict:
    """File a prospect under a client. Idempotent: re-assigning is a no-op.

    Never guesses. An ambiguous prospect stays in the unassigned bucket until a
    human says whose it is - outreach is written and signed AS the client, so the
    wrong attribution sends the wrong company's pitch.
    """
    if body.business_id == body.client_business_id:
        raise HTTPException(422, "a business cannot be a lead for itself")
    conn = connect()
    try:
        for label, bid in (("business", body.business_id), ("client", body.client_business_id)):
            if not conn.execute("SELECT 1 FROM businesses WHERE id = ?", (bid,)).fetchone():
                raise HTTPException(404, f"{label} not found")
        if not conn.execute("SELECT 1 FROM workspaces WHERE id = ?", (body.workspace_id,)).fetchone():
            raise HTTPException(404, "workspace not found")

        # Filing a lead under a client is also evidence the client exists - keep
        # the registry honest even for clients added before it existed.
        _register_client(conn, body.workspace_id, body.client_business_id, "manual")

        # If research already produced evidence for this exact pair, keep the link
        # so the UI can show the signal and the first-touch draft behind the lead.
        ev = conn.execute(
            """SELECT id, fit FROM research_leads
                WHERE business_id = ? AND client_business_id = ?
                ORDER BY created_at DESC LIMIT 1""",
            (body.business_id, body.client_business_id),
        ).fetchone()

        # Rows researched before the write path recorded business_id carry NULL.
        # Fall back to the domain - still exact, still one client - so assigning a
        # legacy lead does not silently drop the reason it was researched.
        if not ev:
            dom = conn.execute(
                "SELECT domain FROM businesses WHERE id = ?", (body.business_id,),
            ).fetchone()
            dom = (dom["domain"] or "").strip().lower() if dom else ""
            if dom:
                ev = conn.execute(
                    """SELECT id, fit FROM research_leads
                        WHERE client_business_id = ? AND business_id IS NULL
                          AND lower(trim(domain)) = ?
                        ORDER BY created_at DESC LIMIT 1""",
                    (body.client_business_id, dom),
                ).fetchone()

        existing = conn.execute(
            "SELECT id FROM client_leads WHERE client_business_id = ? AND business_id = ?",
            (body.client_business_id, body.business_id),
        ).fetchone()
        now = utcnow()
        if existing:
            conn.execute(
                "UPDATE client_leads SET updated_at = ? WHERE id = ?", (now, existing["id"]),
            )
            lid = existing["id"]
        else:
            lid = new_id()
            conn.execute(
                """INSERT INTO client_leads (id, workspace_id, client_business_id, business_id,
                       fit, status, origin, research_lead_id, assigned_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,'new',?,?,?,?,?)""",
                (lid, body.workspace_id, body.client_business_id, body.business_id,
                 ev["fit"] if ev else None, "manual", ev["id"] if ev else None,
                 body.assigned_by, now, now),
            )
        conn.commit()
        row = conn.execute(_SELECT + " AND cl.id = ?", (body.workspace_id, lid)).fetchone()
        return dict(row) if row else {"id": lid}
    finally:
        conn.close()


class PatchIn(BaseModel):
    status: str
    updated_by: str | None = None


@router.patch("/{lead_id}")
def set_status(lead_id: str, body: PatchIn) -> dict:
    if body.status not in STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(STATUSES)}")
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM client_leads WHERE id = ?", (lead_id,)).fetchone()
        if not row:
            raise HTTPException(404, "lead not found")
        conn.execute(
            "UPDATE client_leads SET status = ?, updated_at = ? WHERE id = ?",
            (body.status, utcnow(), lead_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM client_leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(updated)
    finally:
        conn.close()
