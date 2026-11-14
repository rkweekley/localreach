"""Sequences, enrollment, and the send worker.

The worker is deliberately a pull-based tick (`POST /api/worker/tick`) rather
than a background thread: it is trivially testable, it is safe under multiple
replicas, and it makes "why did that email go out" answerable.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import ai, compliance, mailer, schedule
from .config import get_settings
from .db import connect, new_id, rows, utcnow

router = APIRouter(prefix="/api", tags=["campaigns"])


class StepIn(BaseModel):
    step_order: int
    channel: str = "email"
    delay_days: int = 0
    subject_template: str | None = None
    body_template: str | None = None
    variant_of: str | None = None


class SequenceIn(BaseModel):
    workspace_id: str
    name: str
    steps: list[StepIn]
    send_window: str = "weekdays_9_16"
    stop_on_reply: bool = True


@router.post("/sequences")
def create_sequence(body: SequenceIn) -> dict:
    conn = connect()
    try:
        sid = new_id()
        conn.execute(
            """INSERT INTO sequences (id,workspace_id,name,status,send_window,stop_on_reply,created_at)
               VALUES (?,?,?,'draft',?,?,?)""",
            (sid, body.workspace_id, body.name, body.send_window, int(body.stop_on_reply), utcnow()),
        )
        for s in body.steps:
            conn.execute(
                """INSERT INTO sequence_steps (id,sequence_id,step_order,channel,delay_days,
                   subject_template,body_template,variant_of,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (new_id(), sid, s.step_order, s.channel, s.delay_days,
                 s.subject_template, s.body_template, s.variant_of, utcnow()),
            )
        conn.commit()
        return {"id": sid, "name": body.name, "steps": len(body.steps), "status": "draft"}
    finally:
        conn.close()


@router.post("/sequences/{sequence_id}/activate")
def activate(sequence_id: str) -> dict:
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM sequences WHERE id = ?", (sequence_id,)).fetchone():
            raise HTTPException(404, "sequence not found")
        conn.execute("UPDATE sequences SET status = 'active' WHERE id = ?", (sequence_id,))
        conn.commit()
        return {"id": sequence_id, "status": "active"}
    finally:
        conn.close()


class EnrollIn(BaseModel):
    business_ids: list[str]
    skip_blocked: bool = True


@router.post("/sequences/{sequence_id}/enroll")
def enroll(sequence_id: str, body: EnrollIn) -> dict:
    conn = connect()
    try:
        seq = conn.execute("SELECT * FROM sequences WHERE id = ?", (sequence_id,)).fetchone()
        if not seq:
            raise HTTPException(404, "sequence not found")
        enrolled, skipped = [], []
        for bid in body.business_ids:
            contact = conn.execute(
                "SELECT * FROM contacts WHERE business_id = ? ORDER BY is_primary DESC LIMIT 1", (bid,)
            ).fetchone()
            if not contact:
                skipped.append({"business_id": bid, "reason": "no_contact"})
                continue
            verdict = compliance.check_send(
                seq["workspace_id"], contact["email"], contact["email_status"],
                get_settings().mailgun_sender, 10**6,
            )
            if not verdict and body.skip_blocked:
                skipped.append({"business_id": bid, "reason": verdict.reason})
                continue
            # A researched lead can carry the agent's own first touch, written for
            # this company about the signal we actually found. Enrolling it uses
            # that instead of the sequence's generic step 1.
            rl = conn.execute(
                "SELECT email_subject, email_body FROM research_leads"
                " WHERE business_id = ? AND COALESCE(email_subject, '') != ''"
                " ORDER BY created_at DESC LIMIT 1",
                (bid,),
            ).fetchone()
            conn.execute(
                """INSERT INTO enrollments (id,sequence_id,business_id,contact_id,status,current_step,
                   next_send_at,created_at,override_subject,override_body)
                   VALUES (?,?,?,?,'active',0,?,?,?,?)
                   ON CONFLICT (sequence_id, business_id) DO NOTHING""",
                (new_id(), sequence_id, bid, contact["id"], utcnow(), utcnow(),
                 rl["email_subject"] if rl else None, rl["email_body"] if rl else None),
            )
            enrolled.append(bid)
        conn.commit()
        return {"enrolled": len(enrolled), "skipped": skipped}
    finally:
        conn.close()


def render(template: str, ctx: dict) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1).strip()
        return str(ctx.get(key, ""))
    return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", sub, template or "")


@router.post("/worker/tick")
def tick(workspace_id: str, limit: int = 25) -> dict:
    """Send every message that is due. Returns a per-recipient audit trail."""
    conn = connect()
    results: list[dict] = []
    settings = get_settings()
    run_id = uuid.uuid4().hex[:12]
    # A claim older than this is from a worker that died mid-send.
    claim_stale = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    try:
        ws = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if not ws:
            raise HTTPException(404, "workspace not found")
        profile = conn.execute(
            "SELECT * FROM ai_profiles WHERE workspace_id = ? ORDER BY created_at LIMIT 1", (workspace_id,)
        ).fetchone()

        due = rows(conn.execute(
            """SELECT e.*, s.stop_on_reply, s.send_window, s.name AS seq_name FROM enrollments e
               JOIN sequences s ON s.id = e.sequence_id
               WHERE s.workspace_id = ? AND e.status = 'active'
                 AND (e.next_send_at IS NULL OR e.next_send_at <= ?)
               LIMIT ?""",
            (workspace_id, utcnow(), limit),
        ))

        for en in due:
            # Claim before doing any work: a manual tick and a timer tick can
            # overlap, and double-sending one prospect burns the whole domain.
            claimed = conn.execute(
                """UPDATE enrollments SET claimed_at = ?, claimed_by = ?
                   WHERE id = ? AND (claimed_at IS NULL OR claimed_at < ?)""",
                (utcnow(), run_id, en["id"], claim_stale),
            ).rowcount
            conn.commit()
            if not claimed:
                continue
            if not schedule.is_open(en["send_window"], settings.send_tz):
                # Leave next_send_at untouched so it goes out when the window
                # opens instead of being silently dropped.
                results.append({"enrollment": en["id"], "status": "outside_send_window",
                                "window": en["send_window"]})
                continue

            step = conn.execute(
                """SELECT * FROM sequence_steps WHERE sequence_id = ? AND step_order = ?
                   AND variant_of IS NULL LIMIT 1""",
                (en["sequence_id"], en["current_step"] + 1),
            ).fetchone()
            if not step:
                conn.execute("UPDATE enrollments SET status='finished' WHERE id=?", (en["id"],))
                results.append({"enrollment": en["id"], "status": "finished"})
                continue
            if step["channel"] != "email":
                # call/linkedin tasks surface in the task queue; the worker skips them
                conn.execute(
                    """UPDATE enrollments SET current_step = current_step + 1, next_send_at = ?,
                       claimed_at = NULL, claimed_by = NULL WHERE id = ?""",
                    ((datetime.now(timezone.utc) + timedelta(days=step["delay_days"])).isoformat(), en["id"]),
                )
                results.append({"enrollment": en["id"], "status": "task_only", "channel": step["channel"]})
                continue

            contact = conn.execute("SELECT * FROM contacts WHERE id = ?", (en["contact_id"],)).fetchone()
            biz = conn.execute("SELECT * FROM businesses WHERE id = ?", (en["business_id"],)).fetchone()
            if not contact or not biz:
                conn.execute("UPDATE enrollments SET status='stopped', stopped_reason='missing_data' WHERE id=?", (en["id"],))
                results.append({"enrollment": en["id"], "status": "stopped", "reason": "missing_data"})
                continue

            from_email = settings.mailgun_sender
            from_name = settings.mailgun_sender_name or ws["name"]
            verdict = compliance.check_send(
                workspace_id, contact["email"], contact["email_status"], from_email, ws["daily_send_cap"]
            )
            if not verdict:
                conn.execute(
                    "UPDATE enrollments SET status='stopped', stopped_reason=? WHERE id=?",
                    (verdict.reason, en["id"]),
                )
                results.append({"enrollment": en["id"], "status": "blocked", "reason": verdict.reason,
                                "detail": verdict.detail})
                continue

            ctx = {
                "business_name": biz["name"],
                "city": biz["city"],
                "state": biz["state"],
                "category": biz["category"],
                "contact_first_name": (contact["full_name"] or "there").split(" ")[0],
            }
            body_tpl = step["body_template"] or ""
            snippet_text = None
            if "{{ai_snippet}}" in body_tpl:
                # Cite the prospect's own website when we have researched it.
                # This is the difference between "you're a roofer in Columbus"
                # and something a human can tell was written for them, and it
                # costs nothing at send time because the profile is cached.
                observed = ""
                try:
                    from . import research as _research

                    stored = biz["site_research"] if "site_research" in biz.keys() else None
                    observed = _research.brief(stored)
                except Exception:  # noqa: BLE001 - a bad cache must never stop a send
                    observed = ""
                gen = ai.generate_snippet(
                    dict(profile) if profile else {}, dict(biz), dict(contact), detail=observed
                )
                snippet_text = gen["text"]
                body_tpl = body_tpl.replace("{{ai_snippet}}", snippet_text)

            body = render(body_tpl, ctx)
            subject = render(step["subject_template"] or "", ctx)

            # Step 1 for a researched lead: the agent already wrote this email for
            # this exact company about the signal we found. Replaying the generic
            # template over it would throw away the only reason we researched them.
            override = conn.execute(
                "SELECT override_subject, override_body FROM enrollments WHERE id = ?", (en["id"],)
            ).fetchone()
            if override and step["step_order"] == 1:
                if (override["override_subject"] or "").strip():
                    subject = override["override_subject"].strip()
                if (override["override_body"] or "").strip():
                    body = override["override_body"].strip()
                    snippet_text = snippet_text or "lead-research"

            token = compliance.unsubscribe_token(workspace_id, contact["email"])
            body += compliance.footer(dict(ws), token)

            unsub_url = f"{settings.public_base_url}/u/{token}"
            sent = mailer.send(
                from_email, from_name, contact["email"], subject, body,
                reply_to=settings.mailgun_reply_to or from_email,
                headers=mailer.build_unsub_headers(unsub_url),
            )

            mid = new_id()
            conn.execute(
                """INSERT INTO messages (id,workspace_id,enrollment_id,contact_id,business_id,direction,
                   step_order,from_email,to_email,subject,body,provider_id,provider_status,snippet_text,
                   thread_key,sent_at,created_at)
                   VALUES (?,?,?,?,?,'outbound',?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, workspace_id, en["id"], contact["id"], biz["id"], step["step_order"], from_email,
                 contact["email"], subject, body, sent["provider_id"], sent["status"], snippet_text,
                 f"c:{contact['id']}", utcnow(), utcnow()),
            )
            if not sent["ok"]:
                conn.execute(
                    "UPDATE enrollments SET status='stopped', stopped_reason='send_failed' WHERE id=?",
                    (en["id"],),
                )
                results.append({"enrollment": en["id"], "status": "send_failed", "detail": sent["detail"]})
            else:
                nxt = conn.execute(
                    """SELECT delay_days FROM sequence_steps WHERE sequence_id = ? AND step_order > ?
                       AND variant_of IS NULL ORDER BY step_order LIMIT 1""",
                    (en["sequence_id"], step["step_order"]),
                ).fetchone()
                next_at = None
                if nxt:
                    next_at = (datetime.now(timezone.utc) + timedelta(days=nxt["delay_days"])).isoformat()
                conn.execute(
                    """UPDATE enrollments SET current_step = ?, next_send_at = ?,
                       claimed_at = NULL, claimed_by = NULL WHERE id = ?""",
                    (step["step_order"], next_at, en["id"]),
                )
                if next_at is None:
                    conn.execute("UPDATE enrollments SET status='finished' WHERE id=?", (en["id"],))
                results.append({"enrollment": en["id"], "status": "sent", "to": contact["email"],
                                "subject": subject, "provider_id": sent["provider_id"],
                                "next_send_at": next_at})
            conn.commit()
        return {"processed": len(results), "results": results}
    finally:
        conn.close()


@router.get("/sequences/{sequence_id}/stats")
def stats(sequence_id: str) -> dict:
    conn = connect()
    try:
        s = rows(conn.execute(
            """SELECT status, COUNT(*) AS n FROM enrollments WHERE sequence_id = ? GROUP BY status""",
            (sequence_id,),
        ))
        sent = conn.execute(
            """SELECT COUNT(*) AS n FROM messages m JOIN enrollments e ON e.id = m.enrollment_id
               WHERE e.sequence_id = ? AND m.direction = 'outbound'""", (sequence_id,),
        ).fetchone()
        replies = conn.execute(
            """SELECT COUNT(*) AS n FROM messages m JOIN enrollments e ON e.id = m.enrollment_id
               WHERE e.sequence_id = ? AND m.direction = 'inbound'""", (sequence_id,),
        ).fetchone()
        return {"enrollment_status": {r["status"]: r["n"] for r in s},
                "sent": sent["n"], "replies": replies["n"]}
    finally:
        conn.close()
