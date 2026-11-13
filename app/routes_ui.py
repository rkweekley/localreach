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
    """The operator UI. Read per request so a deploy never serves a stale shell."""
    return HTMLResponse(_INDEX.read_text(encoding="utf-8"))


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
        contacts = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(email IS NOT NULL), 0) AS with_email,
                      COALESCE(SUM(email_status = 'verified'), 0) AS verified
               FROM contacts"""
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

        sent = sum(r["n"] for r in msg if r["direction"] == "outbound")
        replies = sum(r["n"] for r in msg if r["direction"] == "inbound")
        return {
            "workspace": dict(ws),
            "dry_run": settings.dry_run,
            "sender": settings.mailgun_sender,
            "counts": {
                "businesses": biz,
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
        }
    finally:
        conn.close()


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
