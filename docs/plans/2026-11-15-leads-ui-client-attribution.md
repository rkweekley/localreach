# Leads UI: who is this lead for?

**Date:** 2026-11-15
**Status:** plan, not started
**Scope:** `app/static/index.html`, `app/routes_research.py`, `app/routes_data.py`, `db/schema.sql`, new `app/routes_leads.py`, new `tests/test_client_leads.py`

## 1. What Ryan saw

"The leads UI doesn't seem complete — it's missing who the lead is for."

Captured live from `https://reach.cyberalsolutions.com/`, Leads tab, as rendered today:

| # | Business | Category | Reviews | Contact | Fit |
|---|---|---|---|---|---|
| 1 | Ridgeline Excavation LLC | Excavation and site work contractor | 0 · — | ridgelinedig@gmail.com `verified` | — |
| 2 | Cyberal Solutions | Managed IT Services / Cybersecurity | 0 · — | info@cyberalsolutions.com `verified` | — |
| 3–19 | 17 more rows | **all `—`** | 0 · — | 8 have email, 9 "no email" | **all `—`** |

The complaint is accurate, but the missing column is a symptom. Six defects are visible in that one table:

1. **No client attribution.** Six of these rows are prospects researched for Ridgeline, six for Cyberal; some are neither. The table renders one flat list and the user cannot tell which is which.
2. **The clients are in the list as if they were leads.** Ridgeline Excavation and Cyberal Solutions are rows 1 and 2 — a business cannot be a lead for itself.
3. **Nine rows belong to nobody.** They exist in `businesses` with no attribution anywhere.
4. **A duplicate survives.** `Ataraxia Properties LLC` and `Ataraxia Properties` are both listed. The name-only dedupe added in `903896d` does not normalise legal suffixes.
5. **Five of six columns are dead.** `Category`, `Reviews`, `Rating` and `Fit` are `—` for 17 of 19 rows. `scores` has **0 rows**, so `fit_score` is always NULL and "ORDER BY review_count" orders by a column that is 0 for every research lead — the sort is arbitrary.
6. **The useful fields are all hidden.** `research_leads` holds `why_now`, `signal_kind`, `signal_detail`, `signal_source`, `contact_hint`, `fit` and a per-lead first-touch email. None of it appears in the Leads table; it only exists inside the research modal, which is transient.

## 2. Root cause

This is not a forgotten column. Lead ownership is stored **three different ways**, none of them queryable, and the Leads UI reads a fourth thing that has no ownership concept at all.

| Encoding | Where | Live state | Queryable by client id? |
|---|---|---|---|
| `research_leads.client_business_id` | `db/schema.sql` | 12 rows; **4 have `business_id` NULL** | yes, but only for researched rows |
| `lists.name = "Leads for {client}"` | `app/routes_research.py:609` | 2 lists, 9 + 8 members, client only in the **string** | no |
| `businesses.source_ref = client_name` | `app/routes_research.py:652` | used as the dedupe key | no |
| `/api/search` → `businesses` | `app/routes_data.py:148` | what the Leads tab reads | **no client concept at all** |

`businesses` has no ownership column. `scores` is empty. `sequences` is empty, and sequences have no client either — so even after enrolling a lead, nothing records whose outreach it is.

Two consequences worth naming:

- **`routes_data.py:97` dedupes on `(source, source_ref)`.** Because `source_ref` carries the client name, the same company imported for two clients produces **two business rows**. That is the duplicate engine, and it is why `Ataraxia Properties` and `Ataraxia Properties LLC` both exist.
- **`business_id` is linked by domain only** (`routes_research.py:644-648`). A lead with no domain, or a domain learned after the fact, silently gets `business_id = NULL` — which is exactly why 4 of 12 researched leads are unattributed today.

The reference product does not have this problem because Resquared is **single-seller**: one workspace = one seller, "which local businesses would buy from you?". LocalReach is being operated as an **agency** with two clients in one workspace, so client ownership has to become a first-class row instead of a string in a list name.

## 3. Design decision

**Adopt one ownership table: `client_leads (client_business_id, business_id, ...)`.** `businesses` stays a pure company registry.

Why not the obvious alternative — `businesses.client_business_id`:

- A single column forces one client per company. A bank is a legitimate prospect for Ridgeline (site work) *and* Cyberal (managed IT). One column means duplicating the business row, which splits contacts and breaks suppression ("one company, one do-not-contact").
- The import path dedupes per `(source, source_ref)`, so per-client rows already multiply today. A nullable column on `businesses` would leave that intact.
- The association table is the standard Account-vs-Opportunity split (Salesforce Account/Opportunity, HubSpot company/deal) and it costs only a join.

`research_leads` keeps its job — it is the *evidence* for one research pass (signal, source URL, why-now, first-touch draft). `client_leads` is the *relationship*. `research_lead_id` links them so a lead can always show the claim it came from.

### Schema

```sql
CREATE TABLE IF NOT EXISTS client_leads (
  id                 TEXT PRIMARY KEY,
  workspace_id       TEXT NOT NULL REFERENCES workspaces(id),
  client_business_id TEXT NOT NULL REFERENCES businesses(id),   -- who it is for
  business_id        TEXT NOT NULL REFERENCES businesses(id),   -- the prospect
  fit                INTEGER,                                   -- 0-100 vs this client
  status             TEXT NOT NULL DEFAULT 'new',
                     -- new | contacted | replied | meeting | won | lost | unfit
  origin             TEXT NOT NULL,                             -- research | import | manual | csv
  research_lead_id   TEXT REFERENCES research_leads(id),
  assigned_by        TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,
  UNIQUE (client_business_id, business_id)                      -- one relationship per pair
);
CREATE INDEX IF NOT EXISTS idx_client_leads_client ON client_leads(client_business_id, status);
CREATE INDEX IF NOT EXISTS idx_client_leads_business ON client_leads(business_id);
```

`UNIQUE (client_business_id, business_id)` is the dedupe guarantee the current code lacks: the same prospect can never appear twice under the same client, and the same prospect may still legitimately exist under two different clients.

## 4. Migration and backfill

Must run on `/data/localreach.db` inside the `localreach` container, after a `VACUUM INTO` backup. **Four passes, strongest evidence first, and nothing inferred twice.** Anything left over stays unassigned rather than being guessed — a wrong attribution is worse than a blank one, because it produces outreach signed by the wrong business.

**Pass 1 — researched leads already linked:**

```sql
INSERT OR IGNORE INTO client_leads
  (id, workspace_id, client_business_id, business_id, fit, status, origin, research_lead_id, created_at, updated_at)
SELECT lower(hex(randomblob(16))), rl.workspace_id, rl.client_business_id, rl.business_id,
       rl.fit, 'new', 'research', rl.id, rl.created_at, rl.created_at
FROM research_leads rl
WHERE rl.business_id IS NOT NULL AND rl.client_business_id IS NOT NULL AND rl.workspace_id = ?;
```

**Pass 2 — repair `business_id` for the 4 unlinked research rows**, by domain first, then normalised name:

```sql
UPDATE research_leads SET business_id = (
  SELECT b.id FROM businesses b
  WHERE (trim(COALESCE(b.domain,'')) <> '' AND lower(b.domain) = lower(trim(rl.domain)))
     OR norm_company(b.name) = norm_company(rl.name)
  ORDER BY (lower(COALESCE(b.domain,'')) = lower(trim(COALESCE(rl.domain,''))) COLLATE NOCASE) DESC, b.created_at
  LIMIT 1)
WHERE research_leads.id = ? AND business_id IS NULL;
```

`norm_company()` is registered as a SQLite function in Python (pass 2 only; the app-side helper is the same function, single definition):

```python
LEGAL = ("llc","l l c","inc","incorporated","co","company","corp","corporation",
         "ltd","limited","lp","llp","pllc","pc","plc","dba")

def norm_company(name: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (name or "").lower())
    toks = [t for t in s.split() if t and t not in LEGAL]
    return " ".join(toks)
```

**Pass 3 — the `Leads for {client}` lists**, matching the client name in the list title:

```sql
INSERT OR IGNORE INTO client_leads
  (id, workspace_id, client_business_id, business_id, fit, status, origin, created_at, updated_at)
SELECT lower(hex(randomblob(16))), l.workspace_id,
       (SELECT b.id FROM businesses b WHERE norm_company(b.name) = norm_company(trim(substr(l.name, 11))) LIMIT 1),
       m.business_id, NULL, 'new', 'import', l.created_at, l.created_at
FROM lists l JOIN list_members m ON m.list_id = l.id
WHERE l.name LIKE 'Leads for %'
  AND (SELECT COUNT(*) FROM businesses b WHERE norm_company(b.name) = norm_company(trim(substr(l.name, 11)))) = 1;
```

The `= 1` guard refuses to guess when a list title matches zero or several clients.

**Pass 4 — `businesses.source_ref` fallback** for rows where the client name was stashed there, same `= 1` guard.

**Pass 5 — surface the remainder.** Do not insert. Report them:

```sql
SELECT b.name, b.source FROM businesses b
WHERE b.id NOT IN (SELECT business_id FROM client_leads)
  AND b.id NOT IN (SELECT client_business_id FROM client_leads)
  AND b.id NOT IN (SELECT id FROM businesses WHERE id IN (SELECT client_business_id FROM research_leads));
```

Expected live result at time of writing: the 9 orphans, and **no row for Ridgeline or Cyberal themselves** (they are clients, not prospects).

**Stop the bleed** in the same change:

- `routes_research.py:609` — stop creating `Leads for {client}` lists, or create them as a derived view that is rebuilt from `client_leads` (never written to as storage). Recommend: delete the write, keep the two existing lists until pass 3 consumes them, then drop them.
- `routes_research.py:652` — `source_ref` returns to meaning "provider id" (`research:{queue_id}`); the client name moves to `client_leads.client_business_id`.
- `routes_data.py:97` — dedupe agent leads on `norm_company(name)` (+ domain when present), not `(source, source_ref)`.

## 5. API

New `app/routes_leads.py`:

```
GET /api/leads?workspace_id=&client_business_id=&q=&bucket=&limit=
POST /api/leads/assign      { business_id, client_business_id }
PATCH /api/leads/{id}        { status }
```

One row per `client_leads` row, joined to the client's display name, the newest research evidence, and the primary contact:

```sql
SELECT cl.id, cl.status, cl.origin, cl.fit,
       cl.client_business_id, cb.name AS client_name,
       b.id AS business_id, b.name, b.category, b.city, b.state, b.domain, b.website,
       rl.why_now, rl.signal_kind, rl.signal_detail, rl.signal_source, rl.contact_hint,
       ct.email, ct.email_status,
       (rl.email_subject IS NOT NULL AND rl.email_subject <> '') AS has_first_touch
FROM client_leads cl
JOIN businesses cb ON cb.id = cl.client_business_id
JOIN businesses b  ON b.id  = cl.business_id
LEFT JOIN research_leads rl ON rl.id = cl.research_lead_id
LEFT JOIN contacts ct ON ct.business_id = b.id AND ct.is_primary = 1
WHERE cl.workspace_id = ?
ORDER BY cl.client_business_id, COALESCE(cl.fit, 0) DESC, b.name;
```

`bucket`: `all` (default), `sendable` (`ct.email_status='verified'`), `no_email`, `unassigned` (the pass-5 query, read-only, no `client_leads` row).

`/api/search` stays for the local-business-search product; the Leads tab stops using it.

## 6. UI (`app/static/index.html`)

The table is rebuilt around the question the operator is actually asking. Grouped by client, because two clients' lead lists are two different work queues:

```
[ All clients ▾ ]  [ search prospects… ]        Ridgeline 6 · Cyberal 6 · Unassigned 9

▾ Ridgeline Excavation LLC — 6 prospects                         (view client profile)
  PROSPECT                    SIGNAL / WHY NOW              FIT  CONTACT              FIRST TOUCH
  Community Bank of Parkersburg   permit · new branch, Q1     92  no email             ✓ drafted
  3D Construction, LLC            hiring · 4 laborers         88  mike@3dc…  verified ✓ drafted
  …

▾ Cyberal Solutions — 6 prospects
  WVU Medicine Camden Clark       rfp · managed services      78  no email             ✓ drafted
  …
```

- **`For` is a group heading, not a column** — a repeated column value in every row wastes width; a grouping answers "whose leads am I looking at" once. A `For` chip also appears in the lead detail modal for single-row context.
- **Drop `Reviews`, `Rating`, `Fit(score)`.** Those belong to the local-search product. `Fit` becomes the client-specific `research_leads.fit` / `client_leads.fit`.
- **Signal / why-now column** carries `signal_kind` as a pill plus the truncated `why_now`, with `signal_source` as the title attribute so every claim is checkable.
- **Contact column** shows the status honestly: `verified`, `invalid` (red), `risky` (amber, with reason on hover: `foreign_domain` / `directory_host`), `no email → call/LinkedIn`.
- **First touch** keeps the existing `rsLeadEmailCell` behaviour: ✓ if `email_subject` exists, "uses template" otherwise, click to preview.
- **Clients never appear as leads** because the table is built from `client_leads`.
- **Unassigned bucket** is a separate group with an explicit `Assign to…` control per row (calls `POST /api/leads/assign`). Nothing is auto-assigned.
- **Empty states** are explicit: "No prospects for this client yet — run research on their domain" with a link to the research panel pre-filled.
- **Row click** opens the lead detail: signal + source link, contact history, first-touch draft, and the enroll action (which records `client_leads.id`, so the message layer finally knows whose outreach it is).

## 7. Tests — `tests/test_client_leads.py`

Run inside the production image (the test container mounts nothing):

```
docker run --rm --user root -v "$PWD":/app -w /app -e API_TOKEN=x localreach:1.0.0 python -m pytest -q -p no:warnings
```

1. `test_two_clients_cannot_share_a_flat_list` — 3 prospects for A, 2 for B → `GET /api/leads` returns 5 rows with correct `client_name`; filtering by A returns 3.
2. `test_a_client_is_never_its_own_lead` — client rows never appear as `business_id`.
3. `test_same_prospect_for_two_clients_is_two_relationships_one_business` — insert twice with different clients → 2 `client_leads`, 1 `businesses` row, contacts not duplicated.
4. `test_duplicate_prospect_for_same_client_is_refused` — `INSERT OR IGNORE` + UNIQUE holds.
5. `test_legal_suffix_duplicate_collapses` — `Ataraxia Properties` vs `Ataraxia Properties LLC` → one row (this is the live defect).
6. `test_lead_without_domain_still_gets_attributed` — the 4-NULL case; name match links `business_id`, `client_leads` row created.
7. `test_unlinked_lead_is_never_guessed` — ambiguous name across two clients → stays unassigned, assignable via API.
8. `test_assign_endpoint_records_who_and_when` — `assigned_by`/`updated_at` set, idempotent on re-assign.
9. `test_leads_surface_carries_signal_and_source` — `why_now`/`signal_kind`/`signal_source` present (the fields the old UI hid).
10. Regression: existing 124 tests stay green, especially `tests/test_direction.py` (placeholders survive to send time) and `tests/test_verify.py`.

## 8. Execution order

| Phase | Work | Files | Done when |
|---|---|---|---|
| 1 | Schema + `norm_company` + migration script (idempotent, `--dry-run`) | `db/schema.sql`, `scripts/migrate_client_leads.py` | dry-run prints the exact attribution table on a copy of the live DB |
| 2 | Backfill on live DB after backup | `/data/localreach.db` | 12 researched leads attributed, orphans listed, 0 client-as-lead rows |
| 3 | `/api/leads` + assign/patch endpoints | `app/routes_leads.py`, `app/main.py` | `curl` returns grouped rows with `client_name`, `signal_*`, `fit` |
| 4 | Leads UI rebuild | `app/static/index.html` | live page shows `Ridgeline 6 · Cyberal 6 · Unassigned 9`, no dead columns |
| 5 | Dedupe identity fix (`norm_company`, `source_ref` no longer identity) | `routes_research.py`, `routes_data.py`, `tests/` | `Ataraxia` pair collapses on next research run; suite green |
| 6 | Deploy + verify | Mac mini | `git pull --ff-only && docker compose up -d --build`; grep the new symbols inside the running container; table matches the DB |

Deployment discipline unchanged: `git pull --ff-only` (never `reset --hard`), and confirm the running container serves the new file — a healthy container says nothing about which commit it is running.

## 9. Risks

- **Live migration.** Two containers share `/data/localreach.db` and the worker ticks every 60s. Take `VACUUM INTO` backup, run the migration inside the container with the worker stopped, then restart. Migration must be idempotent (`INSERT OR IGNORE` throughout).
- **Bad attribution is worse than none.** Outreach is written *as* the client and signed as them; a prospect filed under the wrong client sends the wrong company's pitch. Hence the `= 1` guards and the unassigned bucket.
- **Contacts are shared per company, not per client.** Correct (one mailbox, one suppression), but it means `verify` results apply to both relationships. State it in the UI.
- **Scope creep into campaigns.** `sequences`/`enrollments` have no client either. Out of scope here, but the enroll path should carry `client_leads.id` so that gap can be closed without a second migration.

## 10. Open questions for Ryan

1. **Can one prospect be a lead for two clients at once?** Plan assumes **yes** (`client_leads` is many-to-many). If it must be one at a time, the UNIQUE key becomes `business_id` alone and the table gets simpler — but a bank stops being approachable by both Ridgeline and Cyberal.
2. **What happens to the 9 unassigned rows?** Plan lists them for manual assignment. Alternative: archive them, since several look like stale research from before the direction fix.
3. **What defines `fit`?** Currently `research_leads.fit` (68–92, written by the agent) and `scores.fit_score` (0 rows, never computed). Plan shows the research number; confirm that is the one that should rank the queue.
