"""End-to-end demo: ingest -> score -> sequence -> send -> reply -> stop.

Runs entirely against the real FastAPI app with DRY_RUN=1, so every email is
written to ./outbox/ instead of being sent. This is the fastest way to see
whether the machinery is honest.

    python3 scripts/demo.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["LOCALREACH_DB"] = str(ROOT / "demo.db")
os.environ["DRY_RUN"] = "1"
os.environ["UNSUB_SECRET"] = "demo-secret"
os.environ["PUBLIC_BASE_URL"] = "http://localhost:8000"
os.environ["MAILGUN_SENDER"] = "ryan@cyberalsolutions.com"
os.environ["MAILGUN_SENDER_NAME"] = "Ryan Weekley"

for stale in (ROOT / "demo.db", ROOT / "test.db"):
    stale.unlink(missing_ok=True)
shutil.rmtree(ROOT / "outbox", ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import compliance  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
c = TestClient(app)


def show(label: str, payload) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(payload, indent=2, default=str)[:1600])


# 1 ---------- workspace --------------------------------------------------
ws = c.post("/api/workspaces", json={
    "name": "Cyberal Solutions",
    "slug": "cyberal",
    "physical_address": "1201 N Market St, Lisbon, OH 44432",
    "daily_send_cap": 25,
}).json()
show("workspace", ws)

# 2 ---------- AI profile ("training") -----------------------------------
prof = c.post("/api/ai-profiles", json={
    "workspace_id": ws["id"],
    "name": "Cyberal — IT support for Ohio small business",
    "offer": "Flat-rate IT support so a 10-person shop has the same coverage as a 200-person one",
    "icp": "Owner-operated Ohio businesses, 5-50 staff, no in-house IT, on Microsoft 365",
    "proof_points": ["We answer in under an hour.", "No retainer minimums."],
    "banned_phrases": ["hope this finds you well", "quick question", "circle back"],
}).json()
sj = print
c.post(f"/api/ai-profiles/{prof['id']}/docs", json={
    "kind": "paste",
    "label": "homepage",
    "content": (
        "Cyberal Solutions provides flat-rate managed IT for small businesses across eastern Ohio. "
        "We handle Microsoft 365, endpoint security, backups, and the phone call when the printer "
        "stops working. $75/hr, no retainer minimums, free proof of concept. " * 6
    ),
})
show("profile readiness after ingesting one doc", c.get(f"/api/ai-profiles/{prof['id']}").json()["readiness"])

# 3 ---------- businesses ------------------------------------------------
biz = [
    ("Buckeye Garage Door", "garage_door_repair", "Columbus", 4.7, 212, "owner@buckeyegaragedoor.com", "Dana Whitfield", "Owner", "verified"),
    ("Scioto Overhead Doors", "garage_door_repair", "Columbus", 4.4, 88, "mike@sciotooverhead.com", "Mike Rowan", "Owner", "verified"),
    ("Franklin Door Works", "garage_door_repair", "Columbus", 4.9, 341, "info@franklindoorworks.com", "Pat Simmons", "Manager", "verified"),
    ("Tri-County Roofing", "roofing", "Dublin", 4.2, 57, "bob@tricountyroof.com", "Bob Ellis", "President", "verified"),
    ("Olentangy Exteriors", "roofing", "Dublin", 3.9, 19, "sales@olentangyext.com", "Kim Vo", "Sales", "verified"),
    ("Hilliard Door Service", "garage_door_repair", "Hilliard", 4.6, 130, "owner@hilliarddoor.net", "Sam Ortiz", "Owner", "risky"),
    ("Westside Dock & Door", "garage_door_repair", "Columbus", 4.1, 44, "dana.westside@gmail.com", "Dana Kim", "Owner", "unverified"),
    ("Capital City Garage", "garage_door_repair", "Columbus", 4.8, 402, "contact@capitalcitygarage.com", "Chris Bell", "Owner", "verified"),
]
items = [{
    "name": n, "category": cat, "city": city, "state": "OH", "rating": r,
    "review_count": rc, "years_in_business": 12, "website": "https://" + e.split("@")[1],
    "source": "osm", "source_ref": f"osm:{i}", "contact_email": e, "contact_name": who,
    "contact_title": title, "email_status": status, "license_note": "ODbL-1.0 attribution required",
} for i, (n, cat, city, r, rc, e, who, title, status) in enumerate(biz)]
show("imported businesses", c.post("/api/businesses/import", json=items).json())

# 4 ---------- score -----------------------------------------------------
conn = connect()
ids = [r["id"] for r in conn.execute("SELECT id FROM businesses ORDER BY name")]
conn.close()
for bid in ids:
    c.post(f"/api/businesses/{bid}/score", json={"icp_categories": ["garage_door_repair"]})

search = c.get("/api/search", params={"workspace_id": ws["id"], "min_rating": 4.0, "limit": 10}).json()
show("search: garage doors, rating >= 4.0",
     [{"name": r["name"], "fit": r["fit_score"], "demand": r["demand_score"],
       "reach": r["reach_score"], "email": r["email"]} for r in search["results"]])

# 5 ---------- sequence + enroll -----------------------------------------
seq = c.post("/api/sequences", json={
    "workspace_id": ws["id"],
    "name": "Ohio garage door — IT intro",
    "steps": [
        {"step_order": 1, "channel": "email", "delay_days": 0,
         "subject_template": "{{business_name}} — IT coverage question",
         "body_template": "Hi {{contact_first_name}},\n\n{{ai_snippet}}\n"},
        {"step_order": 2, "channel": "call", "delay_days": 2, "body_template": "Call the owner."},
        {"step_order": 3, "channel": "email", "delay_days": 4,
         "subject_template": "Re: {{business_name}} — IT coverage question",
         "body_template": "Hi {{contact_first_name}},\n\nOne bump and then I'll leave it.\n"},
    ],
}).json()
show("sequence", seq)
c.post(f"/api/sequences/{seq['id']}/activate")
show("enroll (no pre-filtering — the gate runs at send time)",
     c.post(f"/api/sequences/{seq['id']}/enroll", json={"business_ids": ids, "skip_blocked": False}).json())

# 6 ---------- a reply lands BEFORE we send ------------------------------
conn = connect()
target = conn.execute(
    "SELECT c.email FROM contacts c JOIN businesses b ON b.id=c.business_id WHERE b.name='Capital City Garage'"
).fetchone()["email"]
conn.close()
show("inbound reply (opt-out) processed first", c.post("/webhooks/mailgun/inbound", json={
    "sender": target,
    "subject": "Re: Capital City Garage — IT coverage question",
    "stripped-text": "Please unsubscribe me, we already have someone.",
}).json())

# 7 ---------- worker tick ----------------------------------------------
tick = c.post("/api/worker/tick", params={"workspace_id": ws["id"]}).json()
show("worker tick", tick)

# 8 ---------- a real reply stops a live sequence ------------------------
conn = connect()
live = conn.execute(
    """SELECT c.email, b.name FROM enrollments e JOIN contacts c ON c.id=e.contact_id
       JOIN businesses b ON b.id=e.business_id
       WHERE e.status='active' LIMIT 1"""
).fetchone()
conn.close()
if live:
    show(f"reply from {live['name']}", c.post("/webhooks/mailgun/inbound", json={
        "sender": live["email"],
        "subject": f"Re: {live['name']} — IT coverage question",
        "stripped-text": "Not interested, thanks.",
    }).json())

# 9 ---------- email agent ---------------------------------------------
show("agent: 'how are my campaigns doing?'", c.post("/api/agent/email-in", json={
    "workspace_id": ws["id"], "from_email": "ryan@cyberalsolutions.com",
    "subject": "status", "body": "how are my campaigns doing",
}).json())
show("agent: 'find garage door repair in Columbus'", c.post("/api/agent/email-in", json={
    "workspace_id": ws["id"], "from_email": "ryan@cyberalsolutions.com",
    "subject": "new campaign", "body": "find garage door repair in Columbus",
}).json())

# 10 --------- outbox ---------------------------------------------------
files = sorted((ROOT / "outbox").glob("*.json")) if (ROOT / "outbox").exists() else []
print(f"\n=== outbox: {len(files)} rendered email(s), nothing sent ===")
if files:
    mail = json.loads(files[0].read_text())
    print(f"To: {mail['to']}\nSubject: {mail['subject']}\n\n{mail['body']}")

conn = connect()
supp = [dict(r) for r in conn.execute("SELECT email, reason, scope FROM suppressions")]
conn.close()
show("suppression list", supp)
print("\nDemo complete.")
