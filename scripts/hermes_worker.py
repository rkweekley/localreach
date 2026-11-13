#!/usr/bin/env python3
"""Hermes worker: pull queued domains, research them with the Hermes agent, write back.

The dashboard enqueues a domain; this process claims the row, runs one
``hermes -z`` agent turn (the agent fetches the site with its own tools), and
posts the lead plus a grounded draft to /api/research/queue/<id>/complete.

Pull, not push: nothing on the LocalReach side has to reach this machine, so no
agent endpoint is exposed to the internet and a reboot just resumes on the next
poll.

Environment:
  LOCALREACH_URL    base URL of the app (default http://127.0.0.1:8124)
  LOCALREACH_TOKEN  bearer token, same one the dashboard uses (required)
  WORKSPACE_ID      workspace to drain; auto-detected when exactly one exists
  HERMES_BIN        hermes executable (default "hermes")
  BATCH             items per run (default 1, capped at 5)
  AGENT_TIMEOUT_S   per-item agent budget (default 300)
  DRY_RUN           1 = claim and research but never write back (default 1)

Usage:
  python3 scripts/hermes_worker.py --once
  python3 scripts/hermes_worker.py --once --domain acme.com   # ad-hoc, no queue
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("LOCALREACH_URL", "http://127.0.0.1:8124").rstrip("/")
TOKEN = os.environ.get("LOCALREACH_TOKEN", "")
WORKSPACE = os.environ.get("WORKSPACE_ID", "")
HERMES = os.environ.get("HERMES_BIN", "hermes")
BATCH = max(1, min(int(os.environ.get("BATCH", "1") or 1), 5))
TIMEOUT = int(os.environ.get("AGENT_TIMEOUT_S", "300") or 300)
DRY_RUN = (os.environ.get("DRY_RUN", "1") or "1").strip().lower() not in ("0", "false", "no", "off")

PROMPT = """You are researching one sales prospect for a cold email. Use your web tools to read the company's own website, then return the findings.

Domain: {domain}

Rules:
- Only state facts you actually read on that site. Never invent an email address or phone number.
- `service_area` is "City, ST" when the site names one; leave it "" if it does not.
- `what_they_do` is one plain sentence a stranger would understand.
- `talking_point` is one specific, quotable detail from the site that proves a human looked at it (a service, a named customer type, a guarantee, a years-in-business claim).
- The email is 60-90 words, first person, no greeting line like "I hope this finds you well", no flattery, one concrete observation from their site, one sentence on what we do, one low-friction question. No em dashes. Sign as Ryan.
- The subject is 6 words max, specific, no clickbait, no colon. Keep company and place names capitalized.

Return ONLY this JSON object, no prose, no code fence:
{{"profile": {{"business_name": "", "what_they_do": "", "industry": "", "customers": "", "service_area": "", "talking_point": "", "services": [], "differentiators": [], "emails": [], "phones": [], "url": "https://{domain}", "domain": "{domain}"}}, "draft": {{"subject": "", "body": "", "grounded_on": ""}}}}
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


TRANSIENT = ("service_draining", "gateway draining", "connection refused", "temporarily unavailable")


def call(method: str, path: str, payload: dict | None = None) -> dict:
    if not TOKEN:
        raise SystemExit("LOCALREACH_TOKEN is not set")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {body}") from exc


def resolve_workspace() -> str:
    if WORKSPACE:
        return WORKSPACE
    raw = call("GET", "/api/workspaces")
    spaces = raw if isinstance(raw, list) else (raw.get("workspaces") or raw.get("items") or [])
    if len(spaces) != 1:
        raise SystemExit(
            "Set WORKSPACE_ID: found "
            + (f"{len(spaces)} workspaces" if spaces else "no workspaces")
        )
    return spaces[0]["id"]


def extract_json(text: str) -> dict:
    """The agent may wrap JSON in prose or a fence; take the first balanced object."""
    if not text:
        raise ValueError("agent returned nothing")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    for blob in candidates:
        start = blob.find("{")
        while start != -1:
            depth = 0
            for i in range(start, len(blob)):
                if blob[i] == "{":
                    depth += 1
                elif blob[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            parsed = json.loads(blob[start : i + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(parsed, dict) and "profile" in parsed:
                            return parsed
                        break
            start = blob.find("{", start + 1)
    raise ValueError("no JSON object with a 'profile' key in the agent output")


def research_with_agent(domain: str) -> dict:
    """One agent turn. A draining gateway is a restart, not a failure - retry it."""
    last = ""
    for attempt, pause in enumerate((0, 20, 45), start=1):
        if pause:
            log(f"{domain}: {last} - waiting {pause}s (attempt {attempt})")
            time.sleep(pause)
        started = time.time()
        proc = subprocess.run(
            [HERMES, "-z", PROMPT.format(domain=domain)],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        took = round(time.time() - started, 1)
        if proc.returncode != 0:
            last = f"hermes exited {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}"
            if any(mark in last.lower() for mark in TRANSIENT) and attempt < 3:
                continue
            raise RuntimeError(last)
        result = extract_json(proc.stdout)
        log(f"{domain}: agent finished in {took}s")
        return result
    raise RuntimeError(last or "agent failed")


def handle_item(item: dict) -> None:
    qid, domain = item["id"], item["domain"]
    log(f"claim {qid} {domain}")
    try:
        result = research_with_agent(domain)
    except subprocess.TimeoutExpired:
        if not DRY_RUN:
            call("POST", f"/api/research/queue/{qid}/complete", {"status": "error", "error": f"agent timed out after {TIMEOUT}s"})
        log(f"{domain}: timed out")
        return
    except Exception as exc:  # noqa: BLE001 - reported to the queue, not raised
        if not DRY_RUN:
            call("POST", f"/api/research/queue/{qid}/complete", {"status": "error", "error": str(exc)})
        log(f"{domain}: failed - {exc}")
        return

    profile = result.get("profile") or {}
    draft = result.get("draft") or None
    log(f"{domain}: {profile.get('business_name')!r} | {len(profile.get('services') or [])} services"
        f" | emails={profile.get('emails')} | draft={'yes' if draft else 'no'}")
    if DRY_RUN:
        log(f"{domain}: DRY_RUN=1, not writing back. Payload: {json.dumps(result)[:600]}")
        return
    done = call("POST", f"/api/research/queue/{qid}/complete",
                {"status": "done", "profile": profile, "draft": draft})
    log(f"{domain}: written back, status={done['item']['status']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass instead of polling")
    ap.add_argument("--domain", help="research one domain directly, skipping the queue")
    args = ap.parse_args()

    if args.domain:
        print(json.dumps(research_with_agent(args.domain), indent=2))
        return 0

    ws = resolve_workspace()
    log(f"worker start url={URL} workspace={ws} batch={BATCH} dry_run={DRY_RUN}")

    while True:
        try:
            got = call("POST", "/api/research/queue/claim", {"workspace_id": ws, "limit": BATCH})
        except Exception as exc:  # noqa: BLE001 - keep polling through transient errors
            log(f"claim failed - {exc}")
            if args.once:
                return 1
            time.sleep(30)
            continue
        items = got.get("claimed") or []
        if not items:
            if args.once:
                log("queue empty")
                return 0
            time.sleep(20)
            continue
        for item in items:
            handle_item(item)
        if args.once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
