"""Inbound email + delivery events + one-click unsubscribe.

This is the half of an outreach system people forget to build, and it is where
all the value is: a reply that nobody reads is worse than no send at all.

Trust boundaries:
  * `/webhooks/mailgun/*`  — HMAC-verified. Without signature checking, the URL
    alone lets anyone forge a hard bounce (kills the lead) or an unsubscribe
    (kills the list).
  * `/u/{token}`           — public by necessity, protected by a signed token
    that carries the address. GET renders a confirmation page; POST actually
    opts out. That split matters: mailbox providers and security scanners
    prefetch GET links, so a GET that unsubscribed would delete real contacts.
"""
from __future__ import annotations

import html
import json
import logging
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import ai, compliance, mailer
from .db import connect, new_id, rows, utcnow
from .security import client_ip, verify_mailgun

router = APIRouter(tags=["webhooks"])
log = logging.getLogger("localreach.webhooks")

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


def _candidate_workspaces(conn, email: str) -> list[str]:
    """Every tenant with a claim on this address — used to validate the token."""
    email = email.lower()
    found: list[str] = []
    for sql, params in (
        ("SELECT DISTINCT workspace_id FROM messages WHERE to_email = ?", (email,)),
        ("SELECT DISTINCT workspace_id FROM suppressions WHERE email = ?", (email,)),
        ("""SELECT DISTINCT q.workspace_id FROM contacts c
            JOIN enrollments e ON e.contact_id = c.id
            JOIN sequences q ON q.id = e.sequence_id WHERE c.email = ?""", (email,)),
    ):
        for r in conn.execute(sql, params):
            if r["workspace_id"] and r["workspace_id"] not in found:
                found.append(r["workspace_id"])
    return found


async def _payload(request: Request) -> dict:
    raw = await request.body()
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise HTTPException(400, "malformed JSON")
        return data if isinstance(data, dict) else {"_list": data}
    parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _authenticate(request: Request, p: dict) -> None:
    verify_mailgun(
        request,
        str(p.get("timestamp") or ""),
        str(p.get("token") or ""),
        str(p.get("signature") or ""),
    )


def _forward_reply(from_email: str, subject: str, body: str, request: Request) -> None:
    """Copy the reply into a human mailbox so nobody has to babysit the app."""
    settings = request.app.state.settings
    target = settings.reply_forward_to
    if not target:
        return
    sender = (settings.mailgun_sender or "").lower()
    if from_email.lower() in {target.lower(), sender}:
        return  # never forward our own mail back into the loop
    result = mailer.send(
        from_email=settings.mailgun_sender or f"noreply@{settings.mailgun_domain or 'localhost'}",
        from_name=settings.mailgun_sender_name or "LocalReach",
        to_email=target,
        subject=f"[LocalReach reply] {subject or '(no subject)'}"[:200],
        body=f"From: {from_email}\n\n{body}",
        reply_to=from_email or None,
    )
    if not result.get("ok"):
        log.warning("reply forward failed: %s", result.get("detail"))


@router.post("/webhooks/mailgun/inbound")
async def inbound(request: Request) -> dict:
    p = await _payload(request)
    _authenticate(request, p)

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

        if intent in STOP_INTENTS:
            compliance.add_suppression(wid, from_email, intent, conn=conn)

        conn.commit()
    finally:
        conn.close()

    _forward_reply(from_email, subject, body, request)
    log.info("inbound reply", extra={"extra_fields": {
        "from": from_email, "intent": intent, "stopped": stopped}})
    return {"ok": True, "intent": intent, "confidence": confidence,
            "sequences_stopped": stopped, "suppressed": intent in STOP_INTENTS}


@router.post("/webhooks/mailgun/events")
async def events(request: Request) -> dict:
    p = await _payload(request)
    _authenticate(request, p)

    items = p.get("_list") or [p]
    conn = connect()
    handled = 0
    try:
        for it in items:
            if not isinstance(it, dict):
                continue
            etype = it.get("event")
            mid = (it.get("message") or {}).get("headers", {}).get("message-id") if isinstance(it.get("message"), dict) else None
            recipient = (it.get("recipient") or "").lower()
            severity = it.get("severity")
            conn.execute(
                "INSERT INTO events (id,message_id,type,payload,created_at) VALUES (?,?,?,?,?)",
                (new_id(), mid, etype or "unknown", json.dumps(it)[:4000], utcnow()),
            )
            if recipient:
                wid = _workspace_for(conn, recipient)
                if etype == "failed" and severity == "permanent":
                    if wid:
                        compliance.add_suppression(wid, recipient, "hard_bounce", conn=conn)
                    conn.execute(
                        """UPDATE enrollments SET status='bounced', stopped_reason='hard_bounce',
                           next_send_at=NULL WHERE contact_id IN
                           (SELECT id FROM contacts WHERE email = ?) AND status = 'active'""",
                        (recipient,),
                    )
                elif etype in {"complained", "unsubscribed"}:
                    # complained -> global (they marked us spam: never mail again, any tenant)
                    if wid:
                        compliance.add_suppression(
                            wid, recipient, etype,
                            scope="global" if etype == "complained" else "workspace", conn=conn,
                        )
                    conn.execute(
                        """UPDATE enrollments SET status='unsubscribed', next_send_at=NULL
                           WHERE contact_id IN (SELECT id FROM contacts WHERE email = ?)
                             AND status IN ('active','paused')""",
                        (recipient,),
                    )
            if mid:
                conn.execute("UPDATE messages SET provider_status = ? WHERE provider_id = ?", (etype, mid))
            handled += 1
        conn.commit()
    finally:
        conn.close()
    return {"handled": handled}


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Unsubscribe</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:520px;
      margin:80px auto;padding:0 20px;color:#111;line-height:1.5}}
 button{{padding:12px 22px;font-size:16px;border:0;border-radius:8px;
        background:#111;color:#fff;cursor:pointer}}
 .muted{{color:#666;font-size:14px}}
 form{{margin:24px 0}}
</style></head><body>
<h2>{title}</h2>
{content}
</body></html>"""


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_landing(token: str) -> HTMLResponse:
    email = compliance.token_email(token)
    if not email:
        return HTMLResponse(_PAGE.format(
            title="Link not valid",
            content="<p>This unsubscribe link is malformed or expired. "
                    "Reply to the email you received and ask to be removed.</p>",
        ), status_code=400)
    content = (
        f"<p>Stop all email to <strong>{html.escape(email)}</strong>?</p>"
        f'<form method="post" action="/u/{html.escape(token)}">'
        '<button type="submit">Yes, unsubscribe me</button></form>'
        '<p class="muted">One click. No account, no questions, nothing else required.</p>'
    )
    return HTMLResponse(_PAGE.format(title="Unsubscribe", content=content))


@router.post("/u/{token}")
async def unsubscribe_confirm(token: str, request: Request) -> dict:
    """Works with an empty body so RFC 8058 one-click POST works."""
    limiter = request.app.state.limiter
    limiter.check(f"unsub:{client_ip(request)}", limit=30, window_s=60)

    email = compliance.token_email(token)
    if not email:
        raise HTTPException(400, "invalid unsubscribe token")

    conn = connect()
    try:
        # The HMAC in the token already proves which tenant it was issued for,
        # so every workspace gets a shot: an address that was never enrolled
        # still unsubscribes correctly, and a forged token matches nothing.
        candidates = _candidate_workspaces(conn, email)
        for r in conn.execute("SELECT id FROM workspaces"):
            if r["id"] not in candidates:
                candidates.append(r["id"])
        applied = 0
        for wid in candidates:
            if not compliance.verify_token(wid, email, token):
                continue
            compliance.add_suppression(wid, email, "unsubscribed", conn=conn)
            conn.execute(
                """UPDATE enrollments SET status='unsubscribed', stopped_reason='unsubscribed',
                   next_send_at=NULL WHERE contact_id IN (SELECT id FROM contacts WHERE email = ?)
                   AND status IN ('active','paused')""",
                (email,),
            )
            applied += 1
        conn.commit()
    finally:
        conn.close()

    if not applied:
        # Token decoded but the HMAC did not match any tenant: forged or stale.
        raise HTTPException(400, "invalid unsubscribe token")

    log.info("unsubscribed", extra={"extra_fields": {"email": email, "tenants": applied}})
    return {"ok": True, "email": email, "applied": applied}
