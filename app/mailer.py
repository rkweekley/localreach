"""Mailgun transport.

DRY_RUN=1 (default) writes the rendered message to the outbox instead of
sending, so the whole pipeline is testable without touching a real domain.

Three deliberate choices:
  * No `o:tracking-opens`. Open tracking injects a pixel, hurts placement, and
    the data is unreliable. Clicks are still tracked because a click is real
    intent.
  * Every message gets `List-Unsubscribe` + `List-Unsubscribe-Post`, which is
    what makes Gmail/Yahoo one-click work. Since Feb 2024 bulk senders that
    omit these get filtered heavily.
  * Transient failures (timeout, 429, 5xx) are retried; a 4xx that is the
    sender's fault (bad domain, auth) is not — retrying auth failures just
    burns reputation.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import get_settings

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _outbox() -> Path:
    return Path(get_settings().outbox_dir)


def dry_run() -> bool:
    return get_settings().dry_run


def build_unsub_headers(url: str) -> dict[str, str]:
    return {
        "List-Unsubscribe": f"<{url}>",
        # RFC 8058: tells Gmail to POST instead of sending the user to a page.
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def send(from_email: str, from_name: str, to_email: str, subject: str, body: str,
         reply_to: str | None = None, headers: dict | None = None,
         attempt: int = 1) -> dict:
    """Returns {ok, provider_id, status, detail}. Never raises."""
    settings = get_settings()

    if settings.dry_run:
        outbox = _outbox()
        outbox.mkdir(parents=True, exist_ok=True)
        mid = f"dry-{uuid.uuid4().hex[:12]}"
        (outbox / f"{mid}.json").write_text(json.dumps({
            "from": f"{from_name} <{from_email}>",
            "to": to_email,
            "subject": subject,
            "body": body,
            "headers": headers or {},
        }, indent=2))
        return {"ok": True, "provider_id": mid, "status": "dry_run", "detail": "written to outbox"}

    domain = settings.mailgun_domain
    key = settings.mailgun_api_key
    if not domain or not key:
        return {"ok": False, "provider_id": None, "status": "unconfigured",
                "detail": "MAILGUN_DOMAIN / MAILGUN_API_KEY missing"}

    fields = {
        "from": f"{from_name} <{from_email}>",
        "to": to_email,
        "subject": subject,
        "text": body,
        "o:tag": "localreach",
    }
    reply = reply_to or settings.mailgun_reply_to
    if reply:
        fields["h:Reply-To"] = reply
    for k, v in (headers or {}).items():
        fields[f"h:{k}"] = v
    data = urllib.parse.urlencode(fields).encode()
    auth = urllib.parse.quote(f"api:{key}")
    req = urllib.request.Request(
        f"{settings.mailgun_api_base}/{domain}/messages",
        data=data,
        headers={"Authorization": f"Basic {auth}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read())
        return {"ok": True, "provider_id": payload.get("id"), "status": "accepted",
                "detail": payload.get("message", "")}
    except urllib.error.HTTPError as e:
        detail = f"HTTP {e.code}: {e.read()[:400]!r}"
        if e.code in RETRY_STATUS and attempt < 3:
            time.sleep(1.5 * attempt)
            return send(from_email, from_name, to_email, subject, body,
                        reply_to, headers, attempt + 1)
        return {"ok": False, "provider_id": None, "status": "failed", "detail": detail}
    except Exception as e:  # noqa: BLE001 - network errors must not crash the worker
        if attempt < 3:
            time.sleep(1.5 * attempt)
            return send(from_email, from_name, to_email, subject, body,
                        reply_to, headers, attempt + 1)
        return {"ok": False, "provider_id": None, "status": "failed", "detail": str(e)}
