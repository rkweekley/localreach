# LocalReach

A self-hosted rebuild of the workflow Resquared AI sells: find local businesses,
score them, write a personalized first line for each one, run a multi-step
email + call sequence, and stop the moment someone replies.

Built for Cyberal Solutions to run on its own Mailgun domain
(`mg.cyberalsolutions.com`), with the part that actually matters — the
compliance gate — enforced in code rather than in a policy document.

> This is a working system, not a mockup. `python3 scripts/demo.py` runs the
> whole pipeline against a throwaway database and writes rendered emails to
> `./outbox/` without sending anything.

## What it does

| Resquared feature | LocalReach equivalent | Status |
|---|---|---|
| 14M business database | `POST /api/businesses/import` — bring your own source (OSM, DataAxle, your CRM) | working |
| AI Search with filters | `GET /api/search` — rating, reviews, category, city, sorted by score | working |
| Re2 Score | `fit_score` / `demand_score` / `reach_score`, kept separate | working |
| AI Profile ("training bar") | `POST /api/ai-profiles` + `/docs`, readiness 0–100 | working |
| Snippets (60/100/150 words) | `POST /api/ai-snippets` with deterministic fallback | working |
| Market Demand | `category_demand` table, Keyword Planner CSV import | working (needs data) |
| Sequences + call tasks | `POST /api/sequences`, `/enroll`, `POST /api/worker/tick` | working |
| AI Agent (`agent@re2.ai`) | Mailgun inbound route → `POST /webhooks/mailgun/inbound` | working |
| No open tracking | Deliberately omitted | by design |
| Team shared inbox | Not built | roadmap |

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set UNSUB_SECRET at minimum
uvicorn app.main:app --reload
```

Then:

```bash
python3 scripts/demo.py        # full pipeline, no network, nothing sent
python3 -m pytest tests -q     # 9 tests covering the guarantees
```

`DRY_RUN=1` is the default. Nothing reaches Mailgun until you set it to `0`,
and the app reports `"dry_run": true` on `/health` so you can never wonder.

## The one design decision that matters

Every other cold-outreach tool decides whether to send when you *enqueue*. This
one decides at **send time**, in a single function (`app/compliance.py::check_send`),
which returns a named reason when it refuses:

```
unverified | role_address | suppressed | domain_suppressed | daily_cap | no_contact
```

A prospect who unsubscribes two seconds before the worker ticks gets skipped.
The demo shows exactly this: an opt-out is processed *before* the tick, and that
business is not in the send log.

Blocks by default, each overridable only by explicit env var:

- **Unverified addresses** — `email_status` must be `verified`. `risky`/`unknown` never send.
- **Role mailboxes** — `info@`, `sales@`, `contact@` … rejected (`ALLOW_ROLE_ADDRESSES=1` to override).
- **Daily cap** — per workspace, default 40 (`daily_send_cap`).
- **Suppression** — address, domain, or global; checked every send.
- **Postal address + one-click unsubscribe** — injected into every message body.

## Layout

```
app/
  main.py            FastAPI app, /health
  db.py              sqlite3 (WAL) now, Postgres later — same DDL
  compliance.py      the gate: verified, role, cap, suppression, tokens
  scoring.py         fit / demand / reach, with labelled proxies
  ai.py              snippet prompt + template fallback + reply classifier
  mailer.py          Mailgun transport, dry-run outbox
  routes_data.py     workspaces, business import, search
  routes_campaigns.py sequences, enrollments, worker tick
  routes_ai.py       AI profile, snippets, email agent
  routes_webhooks.py inbound parse, delivery events, unsubscribe
db/schema.sql        portable DDL
scripts/demo.py      end-to-end demo
docs/PLAN.md         the full build and launch plan
```

## Multi-tenancy note

`workspace_id` scopes businesses, contacts, sequences, suppressions and
messages. Webhook replies resolve the tenant from the contact's enrollment —
not from "first workspace in the table" — so a suppression can never land in
another company's list. There is a test for it, because I got it wrong once.

## License

MIT. Data you import is your problem: OpenStreetMap is ODbL (attribution
required), Google Places may not be cached beyond 30 days, and purchased
contact data carries its own terms. See `docs/PLAN.md` §5.
