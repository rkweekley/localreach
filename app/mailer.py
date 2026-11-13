"""Mailgun transport.

DRY_RUN=1 (default) writes the rendered message to ./outbox/ instead of sending,
so the whole pipeline is testable without touching a real domain.

Note the deliberate omission: no `o:tracking-opens`. Open tracking injects a
pixel, hurts placement, and the data is unreliable anyway. Clicks are still
tracked because a click is a real intent signal.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAILGUN_API = os.getenv("MAILGUN_API_BASE", "https://api.mailgun.net/v3")
OUTBOX = Path(os.getenv("OUTBOX_DIR", "./outbox"))


def dry_run() -> bool:
    return os.getenv("DRY_RUN", "1") == "1"


def send(from_email: str, from_name: str, to_email: str, subject: str, body: str,
         reply_to: str | None = None, headers: dict | None = None) -> dict:
    """Returns {ok, provider_id, status, detail}."""
    if dry_run():
        OUTBOX.mkdir(parents=True, exist_ok=True)
        mid = f"dry-{uuid.uuid4().hex[:12]}"
        (OUTBOX / f"{mid}.json").write_text(json.dumps({
            "from": f"{from_name} <{from_email}>",
            "to": to_email,
            "subject": subject,
            "body": body,
            "headers": headers or {},
        }, indent=2))
        return {"ok": True, "provider_id": mid, "status": "dry_run", "detail": "written to outbox"}

    domain = os.getenv("MAILGUN_DOMAIN", "")
    key = os.getenv("MAILGUN_API_KEY", "")
    if not domain or not key:
        return {"ok": False, "provider_id": None, "status": "unconfigured",
                "detail": "MAILGUN_DOMAIN / MAILGUN_API_KEY missing"}

    fields = {
        "from": f"{from_name} <{from_email}>",
        "to": to_email,
        "subject": subject,
        "text": body,
    }
    if reply_to:
        fields["h:Reply-To"] = reply_to
    for k, v in (headers or {}).items():
        fields[f"h:{k}"] = v
    data = urllib.parse.urlencode(fields).encode()
    auth = urllib.parse.quote(f"api:{key}")
    req = urllib.request.Request(
        f"{MAILGUN_API}/{domain}/messages",
        data=data,
        headers={"Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read())
        return {"ok": True, "provider_id": payload.get("id"), "status": "accepted", "detail": payload.get("message", "")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "provider_id": None, "status": "failed", "detail": f"HTTP {e.code}: {e.read()[:400]!r}"}
    except Exception as e:
        return {"ok": False, "provider_id": None, "status": "failed", "detail": str(e)}
