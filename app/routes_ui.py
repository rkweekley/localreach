"""Operator dashboard + the read-only views it needs.

The dashboard is a single static HTML file served at `/` — no build step, no
node_modules, nothing to keep in sync with the API. It authenticates with the
same bearer token as the API, so there is exactly one credential and one trust
boundary. `/identity` keeps the machine-readable service card that uptime
monitoring and deploy checks assert against.

The views here are read-only on purpose: every mutation still goes through the
existing `/api/*` routes, so the dashboard cannot become a second, divergent
write path.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from .config import get_settings
from .db import connect, rows

_INDEX = Path(__file__).resolve().parent / "static" / "index.html"

# Public: the shell is inert until a token is supplied, and it contains no data.
public = APIRouter(tags=["ui"])
# Authenticated: these are mounted under the same bearer-token dependency as /api.
router = APIRouter(prefix="/api", tags=["views"])


@public.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """The operator UI. Read per request so a deploy never serves a stale shell.

    `no-store` matters as much as the per-request read: without a Cache-Control the
    browser is free to reuse the previous shell after a deploy, which reads as "the
    fix did not work" while the server is in fact serving the new code.
    """
    return HTMLResponse(
        _INDEX.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@router.get("/overview")
def overview(workspace_id: str) -> dict:
    """One query set for the numbers a person actually looks at first.

    Deliberately excludes anything that costs a provider call: this endpoint is
    polled by the dashboard and must stay cheap.
    """
    settings = get_settings()
    conn = connect()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if not ws:
            raise HTTPException(404, "workspace not found")

        biz = conn.execute("SELECT COUNT(*) AS n FROM businesses").fetchone()["n"]
        # "Leads" must count prospect relationships, never the company registry: a client
        # is a businesses row too, so counting businesses reported 2 "leads" on an empty
        # database and made a completed wipe look like it had failed.
        leads = conn.execute(
            "SELECT COUNT(*) AS n FROM client_leads WHERE workspace_id = ?", (workspace_id,),
        ).fetchone()["n"]
        clients = conn.execute(
            "SELECT COUNT(*) AS n FROM workspace_clients WHERE workspace_id = ? AND status = 'active'",
            (workspace_id,),
        ).fetchone()["n"]
        # Contacts belong to leads, so scope them to this workspace's lead set. Counting
        # every contact row made the tile describe other workspaces' data.
        contacts = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(email IS NOT NULL), 0) AS with_email,
                      COALESCE(SUM(email_status = 'verified'), 0) AS verified
               FROM contacts
               WHERE business_id IN (
                   SELECT business_id FROM client_leads WHERE workspace_id = ?
               )""",
            (workspace_id,),
        ).fetchone()
        seqs = conn.execute(
            """SELECT COUNT(*) AS total, COALESCE(SUM(status = 'active'), 0) AS active
               FROM sequences WHERE workspace_id = ?""", (workspace_id,),
        ).fetchone()
        enr = rows(conn.execute(
            """SELECT e.status AS status, COUNT(*) AS n FROM enrollments e
               JOIN sequences s ON s.id = e.sequence_id
               WHERE s.workspace_id = ? GROUP BY e.status""", (workspace_id,),
        ))
        msg = rows(conn.execute(
            """SELECT direction, COALESCE(provider_status, 'unknown') AS provider_status, COUNT(*) AS n
               FROM messages WHERE workspace_id = ? GROUP BY direction, provider_status""",
            (workspace_id,),
        ))
        suppressions = conn.execute(
            "SELECT COUNT(*) AS n FROM suppressions WHERE workspace_id = ?", (workspace_id,),
        ).fetchone()["n"]
        lists = conn.execute(
            "SELECT COUNT(*) AS n FROM lists WHERE workspace_id = ?", (workspace_id,),
        ).fetchone()["n"]
        tasks = conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE workspace_id = ? AND status = 'open'", (workspace_id,),
        ).fetchone()["n"]
        recent = rows(conn.execute(
            """SELECT m.id, m.to_email, m.subject, m.provider_status, m.direction, m.created_at,
                      b.name AS business_name
               FROM messages m LEFT JOIN businesses b ON b.id = m.business_id
               WHERE m.workspace_id = ? ORDER BY m.created_at DESC LIMIT 8""", (workspace_id,),
        ))
        activity = recent_activity(conn, workspace_id)

        sent = sum(r["n"] for r in msg if r["direction"] == "outbound")
        replies = sum(r["n"] for r in msg if r["direction"] == "inbound")
        return {
            "workspace": dict(ws),
            "dry_run": settings.dry_run,
            "sender": settings.mailgun_sender,
            "counts": {
                "businesses": biz,
                "leads": leads,
                "clients": clients,
                "contacts": contacts["total"],
                "contacts_with_email": contacts["with_email"],
                "contacts_verified": contacts["verified"],
                "sequences": seqs["total"],
                "sequences_active": seqs["active"],
                "suppressions": suppressions,
                "lists": lists,
                "open_tasks": tasks,
                "sent": sent,
                "replies": replies,
            },
            "enrollment_status": {r["status"]: r["n"] for r in enr},
            "message_status": {f"{r['direction']}:{r['provider_status']}": r["n"] for r in msg},
            "recent": recent,
            "activity": activity,
        }
    finally:
        conn.close()


@router.get("/suppressions")
def list_suppressions(workspace_id: str) -> list[dict]:
    """Who has opted out. The Overview tile counts them; this is the list behind it."""
    conn = connect()
    try:
        return rows(conn.execute(
            """SELECT id, email, domain, reason, scope, note, created_at
               FROM suppressions WHERE workspace_id = ?
               ORDER BY created_at DESC LIMIT 200""", (workspace_id,),
        ))
    finally:
        conn.close()


@router.get("/events")
def list_events(workspace_id: str, limit: int = 50) -> list[dict]:
    """Delivery events (delivered / bounced / complained / unsubscribed)."""
    conn = connect()
    try:
        return rows(conn.execute(
            """SELECT e.*, m.subject, m.to_email
               FROM events e LEFT JOIN messages m ON m.id = e.message_id
               JOIN messages w ON w.id = e.message_id AND w.workspace_id = ?
               ORDER BY e.created_at DESC LIMIT ?""", (workspace_id, limit),
        ))
    finally:
        conn.close()


# --- recent activity -------------------------------------------------------
# "Recent activity" used to read the outbox, so a workspace that had never sent
# an email looked dead even while research was running and leads were landing.
# Every table that records a human-visible action reports into one feed here.
# One query per source with an identical shape; the merge happens in Python so
# ordering stays exact and a slow source can be tuned without touching the rest.
_ACTIVITY_SQL = (
    # email sent
    """SELECT m.created_at AS at, 'email' AS kind, 'outbox' AS view, m.id AS ref,
              'Email to ' || COALESCE(b.name, m.to_email, 'a prospect') AS title,
              COALESCE(NULLIF(m.subject, ''), '(no subject)') || ' · ' ||
              COALESCE(m.provider_status, 'queued') AS detail
         FROM messages m LEFT JOIN businesses b ON b.id = m.business_id
        WHERE m.workspace_id = ? AND m.direction = 'outbound'
        ORDER BY m.created_at DESC LIMIT 6""",
    # reply received
    """SELECT m.created_at AS at, 'reply' AS kind, 'outbox' AS view, m.id AS ref,
              'Reply from ' || COALESCE(b.name, m.to_email, 'a prospect') AS title,
              COALESCE(NULLIF(m.subject, ''), '(no subject)') ||
              COALESCE(' · intent: ' || m.intent, '') AS detail
         FROM messages m LEFT JOIN businesses b ON b.id = m.business_id
        WHERE m.workspace_id = ? AND m.direction = 'inbound'
        ORDER BY m.created_at DESC LIMIT 6""",
    # research finished (or failed)
    """SELECT COALESCE(q.finished_at, q.requested_at) AS at, 'research' AS kind,
              'research' AS view, q.id AS ref,
              'Researched ' || q.domain AS title,
              CASE WHEN q.status = 'error'
                   THEN 'failed: ' || COALESCE(q.error, 'unknown error')
                   ELSE (SELECT COUNT(*) FROM research_leads rl WHERE rl.queue_id = q.id)
                        || ' lead(s) found' END AS detail
         FROM research_queue q
        WHERE q.workspace_id = ? AND q.status IN ('done', 'error')
        ORDER BY at DESC LIMIT 6""",
    # lead assigned to a client
    """SELECT cl.created_at AS at, 'lead' AS kind, 'leads' AS view, cl.business_id AS ref,
              COALESCE(pb.name, 'A prospect') || ' → ' || COALESCE(cb.name, 'a client') AS title,
              'origin ' || cl.origin || COALESCE(' · fit ' || cl.fit, '') AS detail
         FROM client_leads cl
         JOIN businesses pb ON pb.id = cl.business_id
         JOIN businesses cb ON cb.id = cl.client_business_id
        WHERE cl.workspace_id = ?
        ORDER BY cl.created_at DESC LIMIT 6""",
    # client registered
    """SELECT wc.created_at AS at, 'client' AS kind, 'leads' AS view, wc.business_id AS ref,
              'Client added: ' || COALESCE(b.name, 'unnamed') AS title,
              COALESCE(NULLIF(b.domain, ''), 'no domain') || COALESCE(' · ' || wc.source, '') AS detail
         FROM workspace_clients wc JOIN businesses b ON b.id = wc.business_id
        WHERE wc.workspace_id = ?
        ORDER BY wc.created_at DESC LIMIT 6""",
    # contact discovered
    """SELECT c.created_at AS at, 'contact' AS kind, 'leads' AS view, c.business_id AS ref,
              'Contact at ' || COALESCE(b.name, 'a prospect') AS title,
              COALESCE(NULLIF(c.email, ''), 'no email') ||
              COALESCE(' · ' || c.title, '') || COALESCE(' · ' || c.email_status, '') AS detail
         FROM contacts c JOIN businesses b ON b.id = c.business_id
        WHERE c.business_id IN (SELECT business_id FROM client_leads WHERE workspace_id = ?)
        ORDER BY c.created_at DESC LIMIT 6""",
    # sequence created
    """SELECT s.created_at AS at, 'sequence' AS kind, 'sequences' AS view, s.id AS ref,
              'Sequence: ' || s.name AS title, 'status ' || s.status AS detail
         FROM sequences s WHERE s.workspace_id = ?
        ORDER BY s.created_at DESC LIMIT 4""",
    # enrollment created
    """SELECT e.created_at AS at, 'enrolled' AS kind, 'sequences' AS view, e.sequence_id AS ref,
              COALESCE(b.name, 'A lead') || ' enrolled in ' || s.name AS title,
              'step ' || e.current_step || ' · ' || e.status AS detail
         FROM enrollments e JOIN sequences s ON s.id = e.sequence_id
         LEFT JOIN businesses b ON b.id = e.business_id
        WHERE s.workspace_id = ?
        ORDER BY e.created_at DESC LIMIT 6""",
    # manual task completed
    """SELECT COALESCE(t.completed_at, t.created_at) AS at, 'task' AS kind, 'overview' AS view,
              t.id AS ref,
              UPPER(t.kind) || ' task done' || COALESCE(' · ' || b.name, '') AS title,
              COALESCE(NULLIF(t.notes, ''), 'no notes') AS detail
         FROM tasks t LEFT JOIN businesses b ON b.id = t.business_id
        WHERE t.workspace_id = ? AND t.status = 'done'
        ORDER BY at DESC LIMIT 4""",
    # opt-out
    """SELECT s.created_at AS at, 'optout' AS kind, 'leads' AS view, s.id AS ref,
              'Opted out: ' || s.email AS title,
              s.reason || ' · scope ' || s.scope AS detail
         FROM suppressions s WHERE s.workspace_id = ?
        ORDER BY s.created_at DESC LIMIT 4""",
    # saved list
    """SELECT l.created_at AS at, 'list' AS kind, 'leads' AS view, l.id AS ref,
              'List saved: ' || l.name AS title,
              (SELECT COUNT(*) FROM list_members lm WHERE lm.list_id = l.id) || ' members' AS detail
         FROM lists l WHERE l.workspace_id = ?
        ORDER BY l.created_at DESC LIMIT 4""",
)


def recent_activity(conn, workspace_id: str, limit: int = 14) -> list[dict]:
    """Merge every source that records a visible action into one time-ordered feed."""
    items: list[dict] = []
    for sql in _ACTIVITY_SQL:
        try:
            items.extend(dict(r) for r in conn.execute(sql, (workspace_id,)).fetchall())
        except Exception:  # a missing optional table must not blank the dashboard
            continue
    items.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    # One import or one bulk assignment can produce a dozen identical rows and
    # bury the send, reply and opt-out a marketer actually needs to see. Cap the
    # feed at two rows per kind so it reads like a timeline, not a log dump.
    seen: dict[str, int] = {}
    out: list[dict] = []
    for r in items:
        k = str(r.get("kind") or "?")
        if seen.get(k, 0) >= 2:
            continue
        seen[k] = seen.get(k, 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


@router.get("/sequences")
def list_sequences(workspace_id: str | None = None) -> list[dict]:
    """Sequences with the counters the list view needs, in one round trip."""
    conn = connect()
    try:
        where, args = ("WHERE s.workspace_id = ?", [workspace_id]) if workspace_id else ("", [])
        return rows(conn.execute(
            f"""SELECT s.*,
                  (SELECT COUNT(*) FROM sequence_steps st
                     WHERE st.sequence_id = s.id AND st.variant_of IS NULL) AS steps,
                  (SELECT COUNT(*) FROM enrollments e
                     WHERE e.sequence_id = s.id AND e.status = 'active') AS active,
                  (SELECT COUNT(*) FROM enrollments e
                     WHERE e.sequence_id = s.id) AS enrolled,
                  (SELECT COUNT(*) FROM messages m JOIN enrollments e ON e.id = m.enrollment_id
                     WHERE e.sequence_id = s.id AND m.direction = 'outbound') AS sent
                FROM sequences s {where} ORDER BY s.created_at DESC""", args,
        ))
    finally:
        conn.close()


@router.get("/sequences/{sequence_id}/detail")
def sequence_detail(sequence_id: str) -> dict:
    """Steps plus enrollment counts. `/stats` stays the send-metrics endpoint."""
    conn = connect()
    try:
        seq = conn.execute("SELECT * FROM sequences WHERE id = ?", (sequence_id,)).fetchone()
        if not seq:
            raise HTTPException(404, "sequence not found")
        steps = rows(conn.execute(
            """SELECT id, step_order, channel, delay_days, subject_template, body_template
               FROM sequence_steps WHERE sequence_id = ? AND variant_of IS NULL
               ORDER BY step_order""", (sequence_id,),
        ))
        statuses = rows(conn.execute(
            "SELECT status, COUNT(*) AS n FROM enrollments WHERE sequence_id = ? GROUP BY status",
            (sequence_id,),
        ))
        return {**dict(seq), "steps": steps,
                "enrollment_status": {r["status"]: r["n"] for r in statuses}}
    finally:
        conn.close()


@router.get("/messages")
def list_messages(workspace_id: str, limit: int = 50, direction: str | None = None) -> dict:
    """The outbox. This is how an operator sees what the engine would send."""
    limit = max(1, min(limit, 200))
    conn = connect()
    try:
        where = ["m.workspace_id = ?"]
        args: list = [workspace_id]
        if direction:
            where.append("m.direction = ?")
            args.append(direction)
        data = rows(conn.execute(
            f"""SELECT m.id, m.direction, m.to_email, m.from_email, m.subject, m.provider_status,
                       m.step_order, m.created_at, m.sent_at, m.snippet_text,
                       substr(COALESCE(m.body, ''), 1, 160) AS preview,
                       b.name AS business_name, c.full_name AS contact_name
                FROM messages m
                LEFT JOIN businesses b ON b.id = m.business_id
                LEFT JOIN contacts c ON c.id = m.contact_id
                WHERE {' AND '.join(where)}
                ORDER BY m.created_at DESC LIMIT ?""", (*args, limit),
        ))
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE workspace_id = ?", (workspace_id,),
        ).fetchone()["n"]
        return {"count": len(data), "total": total, "results": data}
    finally:
        conn.close()


@router.get("/messages/{message_id}")
def get_message(message_id: str) -> dict:
    """Full body for the preview pane."""
    conn = connect()
    try:
        m = conn.execute(
            """SELECT m.*, b.name AS business_name, c.full_name AS contact_name
               FROM messages m
               LEFT JOIN businesses b ON b.id = m.business_id
               LEFT JOIN contacts c ON c.id = m.contact_id
               WHERE m.id = ?""", (message_id,),
        ).fetchone()
        if not m:
            raise HTTPException(404, "message not found")
        return dict(m)
    finally:
        conn.close()
