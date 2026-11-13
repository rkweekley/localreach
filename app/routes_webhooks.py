"""Inbound email + delivery events + one-click unsubscribe.

This is the half of an outreach system people forget to build, and it is where
all the value is: a reply that nobody reads is worse than no send at all.
"""
from __future__ import annotations

import json
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import ai, compliance
from .db import connect, new_id, rows, utcnow

router = APIRouter(tags=["webhooks"])

STOP_INTENTS = {"unsubscribe", "not_interested", "wrong_person"}


def _workspace_for(conn, email: str) -> str | None:
    email = email.lower()
    row = conn.execute("SELECT workspace_id FROM suppressions WHERE email = ? LIMIT 1", (email,)).fetchone()
    if row:
        return row["workspace_id"]
    # Prefer the tenant that actually owns this contact: guessing from "first
    # workspace" files suppressions under the wrong company.
    row = conn.execute(
        """SELECT q.workspace_id FROM contacts c
           JOIN enrollments e ON e.contact_id = c.id
           JOIN sequences q ON q.id = e.sequence_id
           WHERE c.email = ? ORDER BY e.created_at DESC LIMIT 1""",
        (email,),
    ).fetchone()
    if row:
        return row["workspace_id"]
    row = conn.execute(
        """SELECT s.workspace_id FROM messages m
           JOIN enrollments e ON e.id = m.enrollment_id
           JOIN sequences s ON s.id = e.sequence_id
           WHERE m.to_email = ? OR m.from_email = ? ORDER BY m.created_at DESC LIMIT 1""",
        (email, email),
    ).fetchone()
    if row:
        return row["workspace_id"]
    row = conn.execute("SELECT id FROM workspaces ORDER BY created_at LIMIT 1").fetchone()
    return row["id"] if row else None


async def _payload(request: Request) -> dict:
    raw = await request.body()
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        return json.loads(raw or b"{}")
    parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


@router.post("/webhooks/mailgun/inbound")
async def inbound(request: Request) -> dict:
    p = await _payload(request)
    from_email = (p.get("sender") or p.get("From") or "").lower()
    if "<" in from_email:
        from_email = from_email.split("<")[-1].strip("> ")
    subject = p.get("subject") or p.get("Subject") or ""
    body = p.get("stripped-text") or p.get("body-plain") or ""
    msg_id = p.get("Message-Id") or ""
    in_reply_to = p.get("In-Reply-To") or ""

    intent, confidence = ai.classify_reply(subject, body)

    conn = connect()
    try:
        wid = _workspace_for(conn, from_email)
        if not wid:
            raise HTTPException(500, "no workspace configured")

        contact = conn.execute("SELECT * FROM contacts WHERE email = ?", (from_email,)).fetchone()
        contact_id = contact["id"] if contact else None
        business_id = contact["business_id"] if contact else None

        thread_key = f"c:{contact_id}" if contact_id else f"m:{in_reply_to or msg_id}"

        conn.execute(
            """INSERT INTO messages (id,workspace_id,contact_id,business_id,direction,from_email,to_email,
               subject,body,in_reply_to,thread_key,intent,received_at,created_at)
               VALUES (?,?,?,?,'inbound',?,?,?,?,?,?,?,?,?)""",
            (new_id(), wid, contact_id, business_id, from_email,
             p.get("recipient") or "", subject, body, in_reply_to, thread_key,
             intent, utcnow(), utcnow()),
        )

        stopped = 0
        if intent in STOP_INTENTS:
            cur = conn.execute(
                """UPDATE enrollments SET status = CASE WHEN ?='unsubscribe' THEN 'unsubscribed' ELSE 'replied' END,
                   stopped_reason = ?, next_send_at = NULL WHERE contact_id = ? AND status = 'active'""",
                (intent, intent, contact_id),
            )
            stopped = cur.rowcount
        elif intent in {"interested", "question", "ooo", "unknown"}:
            # any human reply stops the sequence; the reply itself is the deliverable
            cur = conn.execute(
                """UPDATE enrollments SET status='replied', stopped_reason=?, next_send_at=NULL
                   WHERE contact_id = ? AND status = 'active'""",
                (intent, contact_id),
            )
            stopped = cur.rowcount

        if intent == "unsubscribe" or intent in STOP_INTENTS:
            compliance.add_suppression(wid, from_email, intent, conn=conn)

        conn.commit()
        return {"ok": True, "intent": intent, "confidence": confidence,
                "sequences_stopped": stopped, "suppressed": intent in STOP_INTENTS}
    finally:
        conn.close()


@router.post("/webhooks/mailgun/events")
async def events(request: Request) -> dict:
    p = await _payload(request)
    items = p if isinstance(p, list) else [p]
    conn = connect()
    handled = 0
    try:
        for it in items:
            if not isinstance(it, dict):
                continue
            etype = it.get("event")
            mid = it.get("message", {}).get("headers", {}).get("message-id")
            recipient = (it.get("recipient") or "").lower()
            severity = it.get("severity")
            conn.execute(
                "INSERT INTO events (id,message_id,type,payload,created_at) VALUES (?,?,?,?,?)",
                (new_id(), mid, etype or "unknown", json.dumps(it)[:4000], utcnow()),
            )
            if etype == "failed" and severity == "permanent" and recipient:
                wid = _workspace_for(conn, recipient)
                if wid:
                    compliance.add_suppression(wid, recipient, "hard_bounce")
            if etype == "complained" and recipient:
                wid = _workspace_for(conn, recipient)
                if wid:
                    compliance.add_suppression(wid, recipient, "complained", scope="global")
            if mid:
                conn.execute("UPDATE messages SET provider_status = ? WHERE provider_id = ?",
                             (etype, mid))
            handled += 1
        conn.commit()
        return {"handled": handled}
    finally:
        conn.close()


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_landing(token: str) -> str:
    return f"""<html><body style="font-family:system-ui;max-width:520px;margin:80px auto">
<h2>Unsubscribe</h2>
<p>Click the button and you will not receive another email from this sender.</p>
<form method="post" action="/u/{token}">
  <button style="padding:12px 20px;font-size:16px">Unsubscribe me</button>
</form></body></html>"""


@router.post("/u/{token}")
async def unsubscribe_confirm(token: str, request: Request):
    p = await _payload(request)
    email = (p.get("email") or "").lower()
    conn = connect()
    try:
        # token encodes workspace:email; verify against every workspace that has this contact
        candidates = rows(conn.execute("SELECT DISTINCT workspace_id FROM messages WHERE to_email = ?", (email,))) if email else []
        applied = 0
        for c in candidates:
            if compliance.verify_token(c["workspace_id"], email, token):
                compliance.add_suppression(c["workspace_id"], email, "unsubscribed")
                conn.execute("UPDATE enrollments SET status='unsubscribed', next_send_at=NULL WHERE contact_id IN (SELECT id FROM contacts WHERE email=?)", (email,))
                applied += 1
        conn.commit()
        return {"ok": bool(applied), "email": email, "applied": applied}
    finally:
        conn.close()
