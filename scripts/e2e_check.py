"""End-to-end check against a running instance, using only the public API.

Proves the whole pipeline in the deployed image: workspace -> import -> score ->
sequence -> enroll -> tick -> rendered message in the outbox. Run it after every
deploy; it is deliberately destructive-looking but writes only to the instance
you point it at.

    LOCALREACH_URL=http://127.0.0.1:8000 API_TOKEN=... python scripts/e2e_check.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("LOCALREACH_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ["API_TOKEN"]
OUTBOX = pathlib.Path(os.environ.get("OUTBOX_DIR", "/app/outbox"))


def call(path: str, payload=None, method: str | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method or ("POST" if data is not None else "GET"),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode()
        return json.loads(body) if body.strip() else {}


def main() -> int:
    slug = "e2e-check"
    ws = call("/api/workspaces", {
        "name": "E2E Check Co", "slug": slug, "physical_address": "1 Test Way, Columbus, OH 43215",
        "daily_send_cap": 10,
    })
    print("workspace:", ws["id"])

    call("/api/businesses/import", [{
        "name": "E2E Garage Door", "category": "garage_door_repair", "city": "Columbus",
        "state": "OH", "source": "csv", "source_ref": "e2e-1",
        "contact_email": "owner@example.com", "contact_name": "Pat Owner",
        "email_status": "verified",
    }])
    found = call(f"/api/search?workspace_id={ws['id']}&city=Columbus&limit=5")
    items = found.get("results", found if isinstance(found, list) else [])
    biz = next((b for b in items if b.get("name") == "E2E Garage Door"), None)
    if not biz:
        print("FAIL: imported business not found by search")
        return 1
    print("business:", biz["id"])

    seq = call("/api/sequences", {
        "workspace_id": ws["id"], "name": "E2E Sequence", "send_window": "always",
        "steps": [{
            "step_order": 1, "delay_days": 0,
            "subject_template": "Quick question about {{business_name}}",
            "body_template": "Hi {{contact_name}},\n\nIs {{business_name}} taking new work?\n\nRyan",
        }],
    })
    call(f"/api/sequences/{seq['id']}/activate", {})
    enrolled = call(f"/api/sequences/{seq['id']}/enroll",
                    {"business_ids": [biz["id"]], "skip_blocked": True})
    print("enrolled:", enrolled)
    if not enrolled.get("enrolled"):
        print("FAIL: nothing enrolled —", enrolled.get("skipped"))
        return 1

    result = call(f"/api/worker/tick?workspace_id={ws['id']}", {})
    print("tick:", json.dumps(result)[:400])
    statuses = [r["status"] for r in result.get("results", [])]
    if "sent" not in statuses:
        print("FAIL: expected at least one 'sent', got", statuses)
        return 1

    files = sorted(OUTBOX.glob("*.json"))
    if not files:
        print("FAIL: nothing in the outbox at", OUTBOX)
        return 1
    mail = json.loads(files[-1].read_text())
    headers = {k.lower(): v for k, v in mail.get("headers", {}).items()}
    checks = {
        "From is info@": "info@cyberalsolutions.com" in mail.get("from", ""),
        "unsubscribe header present": "list-unsubscribe" in headers,
        "one-click header present": headers.get("list-unsubscribe-post", "").lower()
        == "list-unsubscribe=one-click",
        "no open-tracking pixel": "o:tracking-opens" not in headers,
        "personalized subject": "E2E Garage Door" in mail.get("subject", ""),
        "signed unsubscribe url": "/u/u2." in headers.get("list-unsubscribe", ""),
    }
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    print("stats:", call(f"/api/sequences/{seq['id']}/stats"))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
