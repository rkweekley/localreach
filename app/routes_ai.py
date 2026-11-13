"""AI profile (the 'training' surface), snippet generation, and the email agent.

The email agent is the feature most people underestimate: you email a plain-English
instruction to one address and the platform executes it. Here that address is
`agent@<your domain>`, wired through the Mailgun inbound route, and the handler
below is what turns the email into actions.
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import ai
from .db import connect, new_id, rows, utcnow

router = APIRouter(prefix="/api", tags=["ai"])


class ProfileIn(BaseModel):
    workspace_id: str
    name: str
    offer: str | None = None
    icp: str | None = None
    tone: str | None = "direct, peer-to-peer, no hype"
    proof_points: list[str] = []
    banned_phrases: list[str] = ["hope this finds you well", "quick question", "circle back", "synergy"]


def _recalc(conn, pid: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(char_count),0) AS c FROM ai_profile_docs WHERE profile_id = ?", (pid,)).fetchone()
    prof = conn.execute("SELECT * FROM ai_profiles WHERE id = ?", (pid,)).fetchone()
    r = ai.readiness(dict(prof), int(row["c"]))
    conn.execute("UPDATE ai_profiles SET readiness = ?, corpus_chars = ?, updated_at = ? WHERE id = ?",
                 (r, int(row["c"]), utcnow(), pid))
    conn.commit()
    return r


@router.post("/ai-profiles")
def create_profile(body: ProfileIn) -> dict:
    conn = connect()
    try:
        pid = new_id()
        conn.execute(
            """INSERT INTO ai_profiles (id,workspace_id,name,offer,icp,tone,proof_points,banned_phrases,
               corpus_chars,readiness,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,0,0,?,?)""",
            (pid, body.workspace_id, body.name, body.offer, body.icp, body.tone,
             json.dumps(body.proof_points), json.dumps(body.banned_phrases), utcnow(), utcnow()),
        )
        conn.commit()
        return {"id": pid, "readiness": _recalc(conn, pid)}
    finally:
        conn.close()


class DocIn(BaseModel):
    kind: str = "paste"      # url | file | paste
    label: str | None = None
    content: str


@router.post("/ai-profiles/{profile_id}/docs")
def add_doc(profile_id: str, body: DocIn) -> dict:
    conn = connect()
    try:
        if not conn.execute("SELECT 1 FROM ai_profiles WHERE id = ?", (profile_id,)).fetchone():
            raise HTTPException(404, "profile not found")
        conn.execute(
            """INSERT INTO ai_profile_docs (id,profile_id,kind,label,content,char_count,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (new_id(), profile_id, body.kind, body.label, body.content, len(body.content), utcnow()),
        )
        readiness = _recalc(conn, profile_id)
        conn.commit()
        return {"profile_id": profile_id, "chars_added": len(body.content), "readiness": readiness}
    finally:
        conn.close()


@router.get("/ai-profiles/{profile_id}")
def get_profile(profile_id: str) -> dict:
    conn = connect()
    try:
        p = conn.execute("SELECT * FROM ai_profiles WHERE id = ?", (profile_id,)).fetchone()
        if not p:
            raise HTTPException(404, "profile not found")
        docs = rows(conn.execute(
            "SELECT id,kind,label,char_count,created_at FROM ai_profile_docs WHERE profile_id = ?", (profile_id,)))
        return {**dict(p), "docs": docs}
    finally:
        conn.close()


class SnippetIn(BaseModel):
    profile_id: str
    business_id: str
    length: int = 100
    observed_detail: str = ""


@router.post("/ai/snippet")
def snippet(body: SnippetIn) -> dict:
    conn = connect()
    try:
        p = conn.execute("SELECT * FROM ai_profiles WHERE id = ?", (body.profile_id,)).fetchone()
        b = conn.execute("SELECT * FROM businesses WHERE id = ?", (body.business_id,)).fetchone()
        if not p or not b:
            raise HTTPException(404, "profile or business not found")
        c = conn.execute("SELECT * FROM contacts WHERE business_id = ? AND is_primary = 1", (body.business_id,)).fetchone()
        return ai.generate_snippet(dict(p), dict(b), dict(c) if c else None, body.observed_detail, body.length)
    finally:
        conn.close()


# ---------------- email agent ----------------

FIND = re.compile(
    r"\b(?:find|get|pull|build|search(?: for)?)\s+(?:me\s+)?(?:a\s+list\s+of\s+)?(?P<cat>[\w\s&/-]{3,40}?)\s+"
    r"(?:in|around|near|for)\s+(?P<city>[\w\s,.-]{2,40})",
    re.I,
)


class AgentEmail(BaseModel):
    workspace_id: str
    from_email: str
    subject: str = ""
    body: str
    profile_id: str | None = None


@router.post("/agent/email-in")
def agent_email_in(body: AgentEmail) -> dict:
    """Executes a plain-English instruction, exactly like emailing agent@re2.ai."""
    text = f"{body.subject}\n{body.body}".strip()
    lowered = text.lower()
    conn = connect()
    try:
        # --- status question ---
        if any(k in lowered for k in ("how are", "status", "performing", "results")):
            seqs = rows(conn.execute(
                "SELECT id,name,status FROM sequences WHERE workspace_id = ?", (body.workspace_id,)))
            out = []
            for s in seqs:
                sent = conn.execute(
                    """SELECT COUNT(*) n FROM messages m JOIN enrollments e ON e.id=m.enrollment_id
                       WHERE e.sequence_id=? AND m.direction='outbound'""", (s["id"],)).fetchone()["n"]
                rep = conn.execute(
                    """SELECT COUNT(*) n FROM messages m JOIN enrollments e ON e.id=m.enrollment_id
                       WHERE e.sequence_id=? AND m.direction='inbound'""", (s["id"],)).fetchone()["n"]
                out.append({**s, "sent": sent, "replies": rep,
                            "reply_rate": round(rep / sent * 100, 1) if sent else 0.0})
            return {"action": "status_report", "sequences": out,
                    "reply": _status_sentence(out)}

        # --- pause ---
        if "pause" in lowered:
            target = conn.execute(
                "SELECT id FROM sequences WHERE workspace_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
                (body.workspace_id,)).fetchone()
            if target:
                conn.execute("UPDATE sequences SET status='paused' WHERE id=?", (target["id"],))
                conn.commit()
                return {"action": "paused", "sequence_id": target["id"],
                        "reply": "Paused. Nothing else goes out from that campaign until you say so."}
            return {"action": "noop", "reply": "Nothing is active right now, so there's nothing to pause."}

        # --- find + build ---
        m = FIND.search(text)
        if m:
            cat = m.group("cat").strip()
            city = m.group("city").strip().strip(".,")
            cat_norm = re.sub(r"\s+", "_", cat.lower())
            found = rows(conn.execute(
                """SELECT b.*, c.email, c.email_status FROM businesses b
                   LEFT JOIN contacts c ON c.business_id=b.id AND c.is_primary=1
                   WHERE lower(b.category) LIKE ? AND lower(b.city) LIKE ?
                   ORDER BY b.review_count DESC LIMIT 200""",
                (f"%{cat_norm.replace('_','%')}%", f"%{city.lower()}%")))
            mailable = [f for f in found if (f.get("email_status") == "verified")]
            if not found:
                return {"action": "no_match", "reply":
                        f"I found nothing in the database for '{cat}' in {city}. "
                        "Run the ingest job for that metro first — I won't invent businesses."}

            sid = new_id()
            conn.execute(
                """INSERT INTO sequences (id,workspace_id,name,status,send_window,stop_on_reply,created_at)
                   VALUES (?,?,?,'draft','weekdays_9_16',1,?)""",
                (sid, body.workspace_id, f"{cat.title()} — {city.title()}", utcnow()))
            steps = [
                (1, 0, "{{business_name}} — quick question",
                 "Hi {{contact_first_name}},\n\n{{ai_snippet}}\n"),
                (2, 3, "Re: {{business_name}} — quick question",
                 "Hi {{contact_first_name}},\n\nBumping this once in case it got buried. "
                 "If it's a no, say no and I'll stop.\n"),
            ]
            for order, delay, subj, tpl in steps:
                conn.execute(
                    """INSERT INTO sequence_steps (id,sequence_id,step_order,channel,delay_days,
                       subject_template,body_template,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (new_id(), sid, order, "email", delay, subj, tpl, utcnow()))
            conn.commit()
            return {"action": "campaign_drafted", "sequence_id": sid,
                    "matched": len(found), "mailable": len(mailable),
                    "steps": 2, "status": "draft",
                    "reply": (f"Built '{cat.title()} — {city.title()}': {len(found)} businesses matched, "
                              f"{len(mailable)} with a verified address. Two-step sequence is drafted, "
                              "not sending. Say 'send it' when you've looked at the snippets.")}

        return {"action": "unrecognized",
                "reply": ("I can do: 'find roofers in Columbus', 'how are my campaigns doing', "
                          "'pause'. I didn't catch an instruction in that one.")}
    finally:
        conn.close()


def _status_sentence(seqs: list[dict]) -> str:
    if not seqs:
        return "No campaigns yet."
    parts = [f"{s['name']}: {s['sent']} sent, {s['replies']} replies ({s['reply_rate']}%)" for s in seqs]
    return " | ".join(parts)
