"""AI layer: snippet generation + reply classification.

Two deliberate choices:
  1. LLM is optional. With no LLM configured the app still works using a
     deterministic template — so the product is never dead because a key
     expired or a model got slow.
  2. Prompts are versioned constants in code, and the snippet actually used
     is stored on the message row. When someone replies "who is this and why
     did you email me", we can replay exactly what we sent and why.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request

LENGTHS = {60: "under 60 words", 100: "60-100 words", 150: "100-150 words"}

SNIPPET_PROMPT = """You write the first two sentences of a cold email to a local business owner.

SELLER PROFILE
offer: {offer}
ideal customer: {icp}
tone: {tone}
proof points: {proof}
never use these words/phrases: {banned}

PROSPECT
business: {name}
category: {category}
location: {city}, {state}
rating: {rating} ({reviews} reviews)
website: {website}
observed detail: {detail}

TASK
Write {length} of body copy that (a) references the observed detail specifically,
(b) states the offer in plain words, (c) asks one easy yes/no question.
No greeting, no sign-off, no subject line, no links, no exclamation marks,
no "I hope this finds you well", no flattery about their reviews.
Output only the body copy."""

INTENT_RULES = [
    ("unsubscribe", r"\b(unsubscribe|remove me|take me off|stop emailing|opt out)\b"),
    ("not_interested", r"\b(not interested|no thanks|no thank you|we're good|already have)\b"),
    ("wrong_person", r"\b(wrong person|not my (department|area)|reach out to|contact .{0,20} instead)\b"),
    ("ooo", r"\b(out of (the )?office|on vacation|maternity leave|will return)\b"),
]


def _llm(prompt: str, max_tokens: int = 400, temperature: float = 0.4) -> str | None:
    base = os.getenv("AI_BASE_URL")            # e.g. http://localhost:11434/v1
    model = os.getenv("AI_MODEL")              # e.g. qwen3.5:9b-64k
    key = os.getenv("AI_API_KEY", "ollama")
    effort = os.getenv("AI_REASONING_EFFORT", "").strip()
    if not base or not model:
        return None
    # Thinking models spend most of a small budget in the reasoning channel and hand back
    # an empty string. Retry upward instead of silently degrading every call to templates.
    budget = max_tokens
    while True:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": budget,
            "temperature": temperature,
        }
        if effort:
            # Thinking models (qwen3 etc.) otherwise spend the whole budget in the
            # reasoning channel and return empty content. "none" disables it where supported.
            payload["reasoning_effort"] = effort
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if content:
                return content
        except Exception:
            return None
        if budget >= 3000:
            return None
        budget = min(3000, budget * 4)


def _fallback_snippet(profile: dict, business: dict, contact: dict | None, detail: str) -> str:
    name = business.get("name") or "your business"
    city = business.get("city") or "town"
    reviews = business.get("review_count") or 0
    rating = business.get("rating")
    who = (contact or {}).get("full_name", "").split(" ")[0] or "there"
    proof = ""
    try:
        pts = json.loads(profile.get("proof_points") or "[]")
        proof = f" {pts[0]}" if pts else ""
    except Exception:
        pass
    observed = detail or (f"you're sitting at {rating} stars across {reviews} reviews in {city}")
    # no greeting here: every body template already opens with "Hi {{contact_first_name}},"
    return (
        f"{observed[0].upper() + observed[1:]}.{proof} "
        f"{profile.get('offer') or 'We help local businesses get more of the jobs they actually want'}. "
        f"Worth a two-minute call this week, or should I leave you alone?"
    )


def generate_snippet(profile: dict, business: dict, contact: dict | None, detail: str = "", length: int = 100) -> dict:
    prompt = SNIPPET_PROMPT.format(
        offer=profile.get("offer") or "",
        icp=profile.get("icp") or "",
        tone=profile.get("tone") or "direct, peer-to-peer",
        proof=profile.get("proof_points") or "[]",
        banned=profile.get("banned_phrases") or "[]",
        name=business.get("name"),
        category=business.get("category"),
        city=business.get("city"),
        state=business.get("state"),
        rating=business.get("rating"),
        reviews=business.get("review_count"),
        website=business.get("website"),
        detail=detail,
        length=LENGTHS.get(length, LENGTHS[100]),
    )
    out = _llm(prompt)
    used_llm = out is not None
    if not out:
        out = _fallback_snippet(profile, business, contact, detail)
    banned = []
    try:
        banned = json.loads(profile.get("banned_phrases") or "[]")
    except Exception:
        pass
    for phrase in banned:
        out = re.sub(re.escape(phrase), "", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip()
    return {"text": out, "used_llm": used_llm, "word_count": len(out.split()), "length_target": length}


def classify_reply(subject: str, body: str) -> tuple[str, float]:
    blob = f"{subject}\n{body}".lower()
    blob = re.sub(r"^>.*$", "", blob, flags=re.M)  # drop quoted history
    for intent, pattern in INTENT_RULES:
        if re.search(pattern, blob):
            return intent, 0.9
    if re.search(r"\?|(how much|what do you charge|send (me )?more|tell me more|interested)", blob):
        return "interested", 0.7
    if len(blob.strip()) < 12:
        return "unknown", 0.3
    # an LLM pass can arbitrate the ambiguous middle; the rules win when they fire
    verdict = _llm(
        "Classify this reply to a cold sales email as exactly one of: interested, "
        "not_interested, wrong_person, ooo, unsubscribe, question, unknown.\n"
        f"Reply:\n{subject}\n{body}\n\nAnswer with the single label only.",
        max_tokens=12,
    )
    if verdict:
        label = verdict.strip().lower().split()[0].strip(".,:;\"'")
        if label in {"interested", "not_interested", "wrong_person", "ooo", "unsubscribe", "question", "unknown"}:
            return label, 0.75
    return "unknown", 0.4


def readiness(profile: dict, chars: int) -> int:
    """Mirror their 30-minute 'training bar': content quality, not time spent."""
    score = 0
    score += min(40, chars // 500)                       # corpus volume
    if profile.get("offer"):
        score += 15
    if profile.get("icp"):
        score += 15
    if profile.get("proof_points") and profile["proof_points"] != "[]":
        score += 15
    if profile.get("tone"):
        score += 10
    if profile.get("banned_phrases") and profile["banned_phrases"] != "[]":
        score += 5
    return min(100, score)


# ---------------- full email drafting (domain research -> sendable copy) ------

EMAIL_PROMPT = """You write one cold email to a local business owner. It must read like a
person who actually looked at their website wrote it.

SELLER
offer: {offer}
ideal customer: {icp}
tone: {tone}
proof points: {proof}
never use these words/phrases: {banned}

PROSPECT
business: {name}
category: {category}
location: {city}, {state}
website: {website}
their own site, in their words: {brief}

TASK
Return ONE json object, no prose, no markdown fence:
{{"subject": "6 words max, specific, no clickbait, no colon; lowercase is fine but keep company and place names capitalized",
  "body": "under {target} words"}}

Rules for the body:
- Open with the specific detail from THEIR site ({brief}) — prove you read it.
- One sentence on what we do, in plain words. No jargon, no awards, no flattery.
- One easy yes/no question. Then sign off as {signature}.
- No links, no exclamation marks, no "I hope this finds you well", no em dashes,
  no bullet points, no subject line inside the body, no promises about rankings
  or revenue, no fake familiarity ("loved your recent post").
Output only the json object."""


def _fallback_email(profile: dict, business: dict, contact: dict | None, brief: str,
                    signature: str) -> dict:
    name = (contact or {}).get("full_name", "") or ""
    first = name.split(" ")[0] if name else "there"
    city = business.get("city") or "town"
    observed = brief or f"you're listed in {city}"
    offer = profile.get("offer") or "We help local businesses get more of the jobs they want"
    offer = offer.rstrip(".")
    proof = ""
    try:
        pts = json.loads(profile.get("proof_points") or "[]")
        proof = f" {pts[0].rstrip('.')}." if pts else ""
    except Exception:
        pass
    subject = f"{(business.get('name') or 'your business')[:40]} - quick question"
    body = (
        f"Hi {first},\n\n"
        f"{observed.rstrip('.')}.\n\n"
        f"{offer}.{proof}\n\n"
        f"Worth a two-minute call this week, or should I leave you alone?\n\n"
        f"{signature}"
    )
    return {"subject": subject, "body": body.strip()}


def draft_email(profile: dict | None, business: dict | None, contact: dict | None = None,
                brief: str = "", length: int = 100, signature: str = "") -> dict:
    """Full subject+body for one prospect, grounded in their own website.

    Same contract as generate_snippet: the model is an upgrade, never a
    dependency. A wrong model or a dead endpoint still yields usable copy.
    """
    profile = profile or {}
    business = business or {}
    signature = signature or os.getenv("MAILGUN_SENDER_NAME", "") or "Thanks"
    banned: list[str] = []
    try:
        banned = json.loads(profile.get("banned_phrases") or "[]")
    except Exception:
        banned = []

    prompt = EMAIL_PROMPT.format(
        offer=profile.get("offer") or "",
        icp=profile.get("icp") or "",
        tone=profile.get("tone") or "direct, peer-to-peer",
        proof=profile.get("proof_points") or "[]",
        banned=json.dumps(banned),
        name=business.get("name") or "",
        category=business.get("category") or "",
        city=business.get("city") or "",
        state=business.get("state") or "",
        website=business.get("website") or "",
        brief=brief or "(nothing fetched)",
        target=length,
        signature=signature,
    )
    raw = _llm(prompt, max_tokens=700, temperature=0.5)
    used_llm = False
    parsed = None
    if raw:
        from .research import _parse_json_object  # local import: avoids a cycle
        parsed = _parse_json_object(raw)

    if parsed and isinstance(parsed.get("body"), str) and len(parsed["body"].strip()) > 40:
        subject = str(parsed.get("subject") or "").strip() or _fallback_email(
            profile, business, contact, brief, signature)["subject"]
        body = parsed["body"].strip()
        used_llm = True
    else:
        fb = _fallback_email(profile, business, contact, brief, signature)
        subject, body = fb["subject"], fb["body"]

    for phrase in banned:
        if not phrase:
            continue
        subject = re.sub(re.escape(phrase), "", subject, flags=re.I)
        body = re.sub(re.escape(phrase), "", body, flags=re.I)
    nouns = _proper_nouns(business, profile)
    if nouns:
        subject = _restore_entity_case(subject, nouns)
        body = _restore_entity_case(body, nouns)
    subject = re.sub(r"\s+", " ", subject).strip() or "quick question"
    body = re.sub(r"[ \t]+([.,!?])", r"\1", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return {
        "subject": subject[:160],
        "body": body,
        "used_llm": used_llm,
        "word_count": len(body.split()),
        "grounded": bool(brief),
    }


_STOP = {"the", "and", "for", "llc", "inc", "co", "company", "services", "service"}


def _proper_nouns(business: dict, profile: dict | None) -> list[str]:
    """Words that must keep their capitalisation inside a written email."""
    words: list[str] = []
    for field in ("name", "city", "state"):
        words += str(business.get(field) or "").split()
    service_area = str(business.get("service_area") or (profile or {}).get("service_area") or "")
    words += service_area.replace(",", " ").split()
    out = []
    for w in words:
        w = w.strip(".,;:()'\"")
        if len(w) >= 3 and w[:1].isupper() and w.lower() not in _STOP and w not in out:
            out.append(w)
    return out


def _restore_entity_case(text: str, nouns: list[str]) -> str:
    """The model drifts to lowercase ("local web team for ridgeline"); fix the names only."""
    for noun in nouns:
        text = re.sub(r"\b" + re.escape(noun) + r"\b", noun, text, flags=re.I)
    return text


def llm_health() -> dict:
    """Can this box actually reach a model right now? The UI shows this."""
    base = os.getenv("AI_BASE_URL") or ""
    model = os.getenv("AI_MODEL") or ""
    out = {"configured": bool(base and model), "model": model,
           "endpoint": "", "reachable": False, "latency_ms": None, "error": None}
    if not out["configured"]:
        out["error"] = "AI_BASE_URL / AI_MODEL not set - using templates"
        return out
    try:
        host = urllib.parse.urlsplit(base).hostname or base
    except Exception:
        host = base
    out["endpoint"] = host
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            base.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {os.getenv('AI_API_KEY', 'ollama')}"},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read(2048)
        out["reachable"] = True
    except Exception as exc:  # noqa: BLE001 - health probes report, never raise
        out["error"] = str(getattr(exc, "reason", exc))[:200]
    out["latency_ms"] = int((time.monotonic() - t0) * 1000)
    return out

