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


def _llm(prompt: str, max_tokens: int = 400) -> str | None:
    base = os.getenv("AI_BASE_URL")            # e.g. http://localhost:11434/v1
    model = os.getenv("AI_MODEL")              # e.g. qwen3.5:9b-64k
    key = os.getenv("AI_API_KEY", "ollama")
    if not base or not model:
        return None
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


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
