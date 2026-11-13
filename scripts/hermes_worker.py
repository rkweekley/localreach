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
  AGENT_TIMEOUT_S   per-item agent budget (default 840)
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
# A lead-hunting turn measures 200-350s; a bare --domain run must not inherit a
# budget that kills it. The wrapper sets the same value; keep them in step.
TIMEOUT = int(os.environ.get("AGENT_TIMEOUT_S", "840") or 840)
DRY_RUN = (os.environ.get("DRY_RUN", "1") or "1").strip().lower() not in ("0", "false", "no", "off")

PROMPT = """You are a prospecting researcher. The domain below is ONE EXAMPLE of a type of business we sell to. Your job: work out exactly what type of business it is, then find OTHER real companies of that same type in the same market. Those companies are the leads.

Domain: {domain}

Read their site first. Work out what trade they are in, who pays them, how big they are, and where they work (city/region). That defines the business type you are hunting.

Then build a list of OTHER companies of that type: same trade, same kind of customer, same region. Think like a local salesperson who reads the town, not like a directory scraper. Ways in:

- ROSTERS: chamber of commerce member lists, trade association directories, BBB new members, manufacturer and supplier "find a contractor" pages, state contractor licence lookups, Google Maps and Yelp category pages for that trade in that metro.
- HIRING: companies advertising the trade's own roles. Somebody hiring an operator, estimator, installer or technician is growing and has money coming in. Check Indeed, ZipRecruiter, Craigslist jobs, the local paper's classifieds, local Facebook job groups.
- PERMITS AND FILINGS: new contractor licences, new LLC registrations, building permits in that trade's name, utility taps, rezoning.
- EXPANSION: new location, new yard, ribbon cutting, new franchise, "now serving" announcement, acquisition.
- WEAK WEB PRESENCE: the strongest signal when we sell marketing. A company that is clearly busy but has no website of its own, uses a Facebook page instead of a site, has a site that is not mobile friendly, shows no photos of real work, has an out of date copyright year, has no service pages, or never answers reviews. Say exactly what is missing and where you saw it.
- BID WINS AND AWARDS: award announcements, bid tabulations, project award notices, "best of" lists.
- REVIEWS: recent reviews that mention being booked out, turning work away, or being hard to reach. Demand they cannot handle is a marketing problem.

Rules for leads:
- Work fast: the whole turn must finish in about five minutes, so cap yourself at 8 lookups. If a lead's signal cannot be confirmed in one or two reads, drop it and move on.
- Every lead must be a REAL company of the same type as the example: same trade, same region. A general contractor is not an excavator, and a national chain is not a local prospect.
- Never list the example domain itself, or an alias of it.
- Every lead needs a signal you READ, not inferred. Put the exact URL you read in signal_source. If you cannot source it, drop the lead.
- Prefer 3 verified leads over 8 guesses.
- why_now is one sentence: what you saw, and why that makes them worth a first call this week.
- signal_detail quotes what you actually read, in a few words.
- contact_hint is a named person or role you actually saw (owner, president, office manager, estimator) plus where you saw it. Never invent an email address; if you only saw a role mailbox, say so.
- fit is 0-100: how closely this company matches the example in type, size and market. 90 and up means they could be the example's twin.

Also fill `icp`: the target company in plain words (trade, size, geography), and `buying_signals`: the triggers that mean a company of this type is ready to spend money on marketing.

Rules for the profile:
- Only state facts you actually read on that site. Never invent an email address or phone number.
- `service_area` is "City, ST" when the site names one; leave it "" if it does not.
- `what_they_do` is one plain sentence a stranger would understand.
- `talking_point` is one specific, quotable detail from the site that proves a human looked at it.
- The draft is the first-touch email to a company of this type, offering what WE do: 60-90 words, first person, no "I hope this finds you well", no flattery, one concrete observation about their trade, one sentence on what we do, one low-friction question. No em dashes. Sign as Ryan.
- The subject is 6 words max, specific, no clickbait, no colon. Keep company and place names capitalized.

Return ONLY this JSON object, no prose, no code fence:
{{"profile": {{"business_name": "", "what_they_do": "", "industry": "", "customers": "", "service_area": "", "talking_point": "", "services": [], "differentiators": [], "emails": [], "phones": [], "icp": "", "buying_signals": [], "url": "https://{domain}", "domain": "{domain}"}}, "leads": [{{"name": "", "domain": "", "why_now": "", "signal_kind": "hiring|permit|expansion|review|rfp|license|news|social|web|other", "signal_detail": "", "signal_source": "https://...", "contact_hint": "", "fit": 80}}], "draft": {{"subject": "", "body": "", "grounded_on": ""}}}}
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
    # The Hermes agent structured this profile with a model, so say so: the
    # UI otherwise labels agent output "page only" and understates the work.
    profile["used_llm"] = True
    leads = [x for x in (result.get("leads") or []) if isinstance(x, dict)]
    profile["leads"] = leads
    draft = result.get("draft") or None
    log(f"{domain}: {profile.get('business_name')!r} | {len(profile.get('services') or [])} services"
        f" | emails={profile.get('emails')} | draft={'yes' if draft else 'no'}"
        f" | leads={len(leads)}")
    for lead in leads[:8]:
        log(f"{domain}:   lead {lead.get('name')!r} signal={lead.get('signal_kind')}"
            f" fit={lead.get('fit')} src={lead.get('signal_source')}")
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
