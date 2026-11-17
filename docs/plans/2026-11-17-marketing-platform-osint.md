# LocalReach → full marketing platform, with OSINT lead discovery

**Date:** 2026-11-17
**Status:** plan, not started
**Scope:** `db/schema.sql`, new `app/places.py`, new `app/sources/`, `app/discover.py`, `app/routes_discovery.py`, `app/routes_inbound.py`, `app/routes_crm.py`, `app/routes_reporting.py`, `app/deliverability.py`, `app/static/index.html`, new `tests/test_places.py`, `tests/test_discovery.py`, `tests/test_deliverability.py`, `tests/test_inbound.py`, `docs/`

---

## 1. What was asked

> "do some research and plan out what it would take to make this a full on marketing app, with the ability to use OSINT to find lead data"

Two separate asks, and they get conflated constantly by vendors:

1. **Top-of-funnel discovery** — find businesses that might buy the client's service, without the user typing a domain first. Today LocalReach cannot do this. You must already know a domain.
2. **Full marketing app** — outbound is one leg. A marketing app also needs inbound capture, attribution, a pipeline you can work, and reporting a client will pay for.

The interesting finding from the research is that **the obvious OSINT approach is contractually forbidden**, and the lawful alternative is better data at zero cost. Section 3 covers that; it is the most important section in this document.

This plan builds on what exists. It does not propose a rewrite.

---

## 2. Where LocalReach actually stands

Verified against the repo at HEAD `feb3ab4`, not from memory:

| Capability | State | Where |
|---|---|---|
| Multi-tenant workspaces | **exists** | `workspaces`, `/api/overview` scoped by `workspace_id` |
| Client registry | **exists** | `workspace_clients`, `POST /api/leads/clients` |
| Site research on a known domain | **exists**, templated when no LLM | `app/research.py`, `research_queue` |
| Prospect records + per-lead first-touch email | **exists** | `research_leads` (`why_now`, `signal_kind`, `signal_detail`, `signal_source`, `contact_hint`, `fit`) |
| Website defect audit with evidence | **exists, and is the best asset in the repo** | `app/audit.py` — `audit_site()`, `GAP_SERVICES`, `_finding(key, evidence)` |
| Scoring | **exists** | `scores.fit_score` / `demand_score` / `reach_score`, `factors` JSON |
| Email verification | **DNS-only** | `app/verify.py` — `verify_address()`, MX/A/TXT via DoH; no SMTP, no paid verifier |
| Guard rails on contacts | **exists** | `is_placeholder`, `foreign_domain`, `directory_host`, `_registrable` |
| Sequences + A/B steps | **exists** | `sequence_steps.variant_of`, `UNIQUE (sequence_id, step_order, variant_of)` |
| Send via Mailgun, tracking, unsubscribe | **exists** | `app/mailer.py`, pixel/click in `events` |
| Event log | **exists** | `events(type, payload)` — delivered/failed/complained/opened/clicked/unsubscribed |
| Task queue with writer + closer | **exists** | `tasks`, `GET /api/tasks`, `PATCH /api/tasks/{id}` |
| **Business/POI database** | **absent** | no `places` table of any kind |
| **Geographic search** | **absent** | discovery is domain-in, not area-in |
| **Discovery run model** | **absent** | `research_queue` is one domain per row |
| **Sending domains / warmup / rotation** | **absent** | single `MAILGUN_SENDER`, one identity |
| **Inbound capture (forms/landing pages)** | **absent** | only Mailgun inbound webhook parsing |
| **CRM (deals, stages, notes)** | **absent** | `client_leads` is a list, not a pipeline |
| **Client-facing reporting** | **absent** | dashboard is operator-facing only |

**Conclusion:** roughly 60% of an outbound engine exists and is genuinely good — the audit-with-evidence module is the differentiator competitors charge $550/mo for. What is missing is the front (where do prospects come from) and the back (what happens after they reply, and how do you prove value to the client).

---

## 3. The OSINT question: what is lawful, and what is a lawsuit

This section is the research result. Each row was checked against the primary terms, not against a blog post.

### 3.1 Ruled out — do not build on these

| Source | What its terms say | Verdict |
|---|---|---|
| **Google Maps Platform / Places API** | §3.2.3(a)(iii) forbids copying and saving "business names, addresses, or user reviews"; §3.2.3(c) "No Creating Content From Google Maps Content"; §3.2.3(d)(iii) forbids using it "in a listings or directory service or to create or augment an advertising product". Only the `place_id` may be stored indefinitely (Google's own caching doc). | **Dead.** A lead database *is* a directory/augmented advertising product. This is the exact use the ToS names. |
| **Foursquare Places API (commercial)** | EULA §2.5.4 forbids using Places Data "to contact businesses included in Places Data as prospective customers". | **Dead, explicitly.** Not "unclear" — it names our use case. Confirmed at $15/1,000 calls at tier, so it would also be expensive. |
| **LinkedIn / Facebook / Instagram scraping** | Prohibited by ToS + CFAA exposure post-*hiQ*; no public licence for bulk business data. | **Dead.** |
| **Yelp Fusion API** | Display-only with restrictions on storage and on combining with competing data. | **Avoid** unless legal sign-off. |

Also note: Google is currently litigating against SERP-scraping vendors, and the winner-take-all risk there is on the *scraper*, not the customer. Building a paid product on scraped Google results is uninsurable.

### 3.2 Green light — the lawful base layer

| Source | Licence | Can we store? | Can we redistribute derived lead lists? | Cost |
|---|---|---|---|---|
| **Overture Maps — Places theme** | **CDLA-Permissive-2.0** | Yes | Yes, with attribution | $0 |
| **Foursquare OS Places** (open dataset, distinct product from the API above) | **Apache-2.0** | Yes | Yes, with NOTICE | $0 |
| **OSM** (via Overpass/Geofabrik) | **ODbL-1.0** | Yes | Share-alike on derivative DBs | $0 |
| **Business's own website** | First-party public web | Yes | — | $0 |
| **DNS / MX / TXT / certificate transparency (crt.sh)** | Public protocol data | Yes | — | $0 |
| **Ohio Secretary of State filings, county recorder, Ohio licensing boards, city permit portals** | Public records, no restrictive licence | Yes | Yes | $0 |

**Overture is the recommendation** for the base layer:
- Published at `s3://overturemaps-us-west-2/release/2026-10-21.0/theme=places/type=place/*` (current release at time of writing), also on Azure Blob.
- "Tens of millions" of POIs, dated releases with a changelog, so ingest is reproducible.
- Fields that matter to us: `names`, `categories` (taxonomy), `confidence`, `websites`, `phones`, `addresses`, `brand`, `sources`, and point `geometry`.
- Regional extraction is a two-line DuckDB query with a bbox filter, verified from the docs:

```sql
LOAD spatial;
COPY (
  SELECT names, categories, confidence,
         CAST(websites AS JSON) AS websites,
         CAST(phones   AS JSON) AS phones,
         CAST(addresses AS JSON) AS addresses,
         geometry
  FROM read_parquet('s3://overturemaps-us-west-2/release/2026-10-21.0/theme=places/*/*')
  WHERE bbox.xmin BETWEEN -83.6 AND -80.4     -- Ohio Valley
    AND bbox.ymin BETWEEN  38.4 AND  42.4
) TO 'ohio_valley_places.parquet' (FORMAT PARQUET);
```

Or the CLI: `overturemaps download --bbox=-83.6,38.4,-80.4,42.4 -f geojson --type=place -o ohio.geojson`.

**Foursquare OS Places is the second adapter**, and it is not a contradiction of §3.1: it is a different product (an open data release under Apache-2.0, not the licensed Places API). Having two independent POI sources lets us cross-confirm a business exists and detect closures. Use OSM only as a third opinion, and keep OSM rows in a **separate logical partition** so ODbL share-alike never attaches to the proprietary tables — that is the "Collective Database" posture, and it needs a one-line legal review before we ship it.

### 3.3 The OSINT signals we can legally derive

This is where a lead list becomes a *reason to call*:

| Signal | Method | Legal basis |
|---|---|---|
| **No website at all** | Overture row has empty `websites` | Dataset field |
| **Broken/dead site** | `audit_site()` — existing | First-party crawl |
| **No HTTPS, no mobile viewport, stale copyright, no schema, slow TTFB** | `audit_site()` findings, each with evidence | First-party crawl |
| **No published email** | Crawl contact/about pages, `mailto:` + regex, existing guards | First-party crawl |
| **Tech stack** (WordPress version, no analytics, no booking widget) | headers + HTML markers | First-party crawl |
| **Business age** | Ohio SOS filing date | Public record |
| **Licence status** (HVAC/electrical/plumbing) | Ohio licensing board lookup | Public record |
| **New location / expansion** | Overture release-over-release diff on the same `id` | Dataset |
| **Hiring** | careers page on the site | First-party crawl |
| **Domain infra weakness** (no SPF, no DMARC, Gmail-hosted) | `verify.domain_auth()` — existing | Public DNS |
| **Email pattern** | `first.last@` style inferred from colleagues at the same domain | Inference, **never** presented as found |

Hard rule carried forward from the existing build: **a contact address is either printed on the prospect's own site or explicitly marked `inferred`, and inferred addresses are never sendable until verification passes.**

---

## 4. Architecture

```
                     ┌──────────────────────────────────────────┐
  Overture parquet ─▶│  ingest (bbox, release-pinned)          │
  Foursquare OS    ─▶│  app/sources/overture.py, fsq_os.py     │─▶ places
  OSM (partition)  ─▶│  app/sources/osm.py                     │   places_dataset
                     └──────────────────────────────────────────┘
                                     │
  geography + filters ──▶ discovery_run ──▶ discovery_candidates
                                     │              │
                                     ▼              ▼
                          place_links (resolve to businesses)
                                     │
                     ┌───────────────┴───────────────┐
                     ▼                               ▼
              enrichment_jobs                  scoring (fit/demand/reach)
        site crawl · audit · contacts ·            │
        tech · dns · email pattern                 ▼
                                          client_leads → sequences → Mailgun
                                                       │
                                    events ◀───────────┘
                                                       │
                            inbound (forms) ──▶ contacts ──▶ deals (CRM-lite)
                                                       │
                                                reporting (per client)
```

### 4.1 New tables

Keep licence provenance on the data itself — it is the only way to answer "where did this row come from" later.

```sql
CREATE TABLE IF NOT EXISTS places_dataset (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,         -- overture | fsq_os | osm
  release         TEXT NOT NULL,         -- 2026-10-21.0
  license         TEXT NOT NULL,         -- CDLA-Permissive-2.0 | Apache-2.0 | ODbL-1.0
  license_url     TEXT,
  attribution     TEXT NOT NULL,         -- rendered in UI + exports
  bbox            TEXT,                  -- JSON [w,s,e,n]
  row_count       INTEGER,
  ingested_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS places (
  id              TEXT PRIMARY KEY,
  dataset_id      TEXT NOT NULL REFERENCES places_dataset(id),
  source_place_id TEXT NOT NULL,
  name            TEXT NOT NULL,
  name_norm       TEXT NOT NULL,          -- lower, legal suffix stripped
  category_primary TEXT,
  categories      TEXT,                   -- JSON array
  lat             REAL, lon REAL,
  street TEXT, city TEXT, state TEXT, postal TEXT, country TEXT,
  phones          TEXT,                   -- JSON array
  websites        TEXT,                   -- JSON array
  socials         TEXT,                   -- JSON array
  confidence      REAL,
  is_closed       INTEGER DEFAULT 0,
  first_seen      TEXT NOT NULL,
  last_seen       TEXT NOT NULL,
  UNIQUE (dataset_id, source_place_id)
);
CREATE INDEX IF NOT EXISTS idx_places_geo  ON places(lat, lon);
CREATE INDEX IF NOT EXISTS idx_places_city ON places(state, city);
CREATE INDEX IF NOT EXISTS idx_places_norm ON places(name_norm);

CREATE TABLE IF NOT EXISTS place_links (
  place_id        TEXT NOT NULL REFERENCES places(id),
  business_id     TEXT NOT NULL REFERENCES businesses(id),
  match_method    TEXT NOT NULL,          -- domain | name_addr | phone
  match_score     REAL,
  created_at      TEXT NOT NULL,
  PRIMARY KEY (place_id, business_id)
);

CREATE TABLE IF NOT EXISTS discovery_runs (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
  client_business_id TEXT NOT NULL REFERENCES businesses(id),
  label           TEXT,
  center_lat REAL, center_lon REAL, radius_km REAL,
  cities          TEXT,                   -- JSON ["Marietta, OH", ...]
  categories      TEXT,                   -- JSON taxonomy keys
  filters         TEXT,                   -- JSON {has_website:false, min_confidence:0.6,...}
  status          TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|failed
  found           INTEGER DEFAULT 0,
  excluded        INTEGER DEFAULT 0,
  queued          INTEGER DEFAULT 0,
  error           TEXT,
  requested_by    TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  finished_at     TEXT
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
  run_id          TEXT NOT NULL REFERENCES discovery_runs(id),
  place_id        TEXT NOT NULL REFERENCES places(id),
  fit             INTEGER,
  reason          TEXT,                   -- "no website" | "site has no HTTPS"
  status          TEXT NOT NULL DEFAULT 'new', -- new|excluded|queued|converted
  exclusion       TEXT,                   -- competitor|client|duplicate|out_of_area
  created_at      TEXT NOT NULL,
  PRIMARY KEY (run_id, place_id)
);

CREATE TABLE IF NOT EXISTS enrichment_jobs (
  id              TEXT PRIMARY KEY,
  target_kind     TEXT NOT NULL,          -- place | business
  target_id       TEXT NOT NULL,
  kind            TEXT NOT NULL,          -- site_crawl|audit|contacts|tech|dns|email_pattern
  status          TEXT NOT NULL DEFAULT 'queued',
  attempts        INTEGER DEFAULT 0,
  last_error      TEXT,
  due_at          TEXT NOT NULL,
  claimed_at      TEXT,
  finished_at     TEXT
);
```

Marketing surface:

```sql
CREATE TABLE IF NOT EXISTS sending_domains (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
  domain          TEXT NOT NULL,
  provider        TEXT,                   -- mailgun | smtp
  spf_ok INTEGER, dkim_selector TEXT, dmarc_policy TEXT,
  warmup_started_at TEXT,
  daily_limit     INTEGER DEFAULT 10,
  sent_today      INTEGER DEFAULT 0,
  reputation      TEXT,                   -- JSON from provider
  UNIQUE (workspace_id, domain)
);

CREATE TABLE IF NOT EXISTS landing_pages (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
  client_business_id TEXT REFERENCES businesses(id),
  slug            TEXT NOT NULL UNIQUE,
  title TEXT, headline TEXT, body_md TEXT,
  form_fields     TEXT,                   -- JSON
  published       INTEGER DEFAULT 0,
  utm_defaults    TEXT,
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS form_submissions (
  id              TEXT PRIMARY KEY,
  landing_page_id TEXT REFERENCES landing_pages(id),
  email TEXT, name TEXT, phone TEXT, message TEXT,
  utm             TEXT,                   -- JSON
  ip_hash         TEXT, user_agent TEXT,
  contact_id      TEXT REFERENCES contacts(id),
  created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deals (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
  client_business_id TEXT NOT NULL REFERENCES businesses(id),
  business_id     TEXT REFERENCES businesses(id),
  stage           TEXT NOT NULL DEFAULT 'new', -- new|contacted|replied|meeting|won|lost
  value_cents     INTEGER,
  owner           TEXT,
  next_action_at  TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
  business_id     TEXT REFERENCES businesses(id),
  kind            TEXT NOT NULL,          -- email|call|note|meeting|form|stage
  body            TEXT,
  actor           TEXT,
  created_at      TEXT NOT NULL
);
```

`events` already exists and covers message lifecycle; `activities` is the human timeline. Do not merge them.

### 4.2 Modules

| File | Responsibility |
|---|---|
| `app/sources/base.py` | `Source` protocol: `fetch(bbox) -> Iterator[PlaceRecord]`, `license`, `attribution` |
| `app/sources/overture.py` | DuckDB over pinned S3 release → parquet → rows |
| `app/sources/fsq_os.py` | Foursquare OS Places parquet adapter |
| `app/sources/osm.py` | Overpass/Geofabrik adapter, writes to its own partition |
| `app/places.py` | Upsert, `name_norm`, geo search (bbox prefilter + haversine), release diffing |
| `app/taxonomy.py` | Overture category → our service taxonomy → client offer match |
| `app/discover.py` | **extend, don't replace** — `search_area()`, `enrich()`, `resolve_to_business()` |
| `app/deliverability.py` | domain auth checks, warmup ladder, cap enforcement, bounce/complaint circuit breaker |
| `app/routes_discovery.py` | `POST /api/discovery/runs`, `GET /api/discovery/runs/{id}`, `POST /api/discovery/candidates/{...}/queue` |
| `app/routes_inbound.py` | public `GET /p/{slug}`, `POST /api/inbound/{slug}` (no auth, rate-limited, honeypot) |
| `app/routes_crm.py` | deals CRUD, stage transitions, activities |
| `app/routes_reporting.py` | per-client rollup + monthly PDF |

### 4.3 Discovery flow, concretely

1. Operator picks a client (e.g. Cyberal), a geography (Marietta + 40 km), and a category set (contractors, dental, law, clinics — whatever the client's offer fixes).
2. `discovery_runs` row is created with `requested_by`; a worker claims it (`queued → running`), same claim pattern as `research_queue`.
3. Candidate query: places in bbox, `is_closed = 0`, `confidence >= X`, category in set, **not already a `businesses` row**, **not the client**, **not a client's competitor trade** (the direction guard from `cde7d28`).
4. Candidate filters that produce the money list:
   - `websites` empty → "no website" (the strongest signal for a web/IT seller)
   - website present → enqueue `site_crawl` + `audit`, then rank by `audit_site()` defect weight
   - no published email → mark as phone task, not email task (existing `tasks` behaviour)
5. Accepted candidates become `businesses` rows via `place_links` (match on domain, then `name_norm` + postal, then phone). Never create a duplicate business when a domain already exists.
6. `scores` are computed with the existing three-part model; `reach_score` now uses real verification.
7. Operator sees a review screen with reason + evidence per row, selects, and `POST /api/leads/queue` puts them into `client_leads` with `origin='discovery'`.

Every step is idempotent per `(run_id, place_id)`.

---

## 5. Deliverability — the part that decides whether any of this works

At 10 emails/day nobody cares. The moment this is a product, volume kills the domain. Google/Yahoo bulk-sender rules (5,000+/day) require SPF **and** DKIM **and** DMARC, one-click unsubscribe (RFC 8058), and a spam-complaint rate under 0.3%. We should meet those at 50/day, because inbox placement degrades long before the threshold.

Build:

1. **`sending_domains` + `sending_identities`** — never send from the primary domain. Use `outreach.cyberalsolutions.com` or a dedicated domain, separate Mailgun sending domain, own DKIM key.
2. **Warmup ladder** — 10/day for week 1, 20 week 2, 40 week 3, cap at 50 per identity. Store `warmup_started_at`; the cap is computed, not configured by hand.
3. **Circuit breakers** — if `events.complained / delivered > 0.1%` or `failed > 5%` in 24h, pause that domain and surface it on the dashboard. Non-negotiable; a runaway sender is how you lose the domain.
4. **One-click unsubscribe** — `List-Unsubscribe` + `List-Unsubscribe-Post` headers on every message, hitting the existing suppression path.
5. **DMARC reporting** — a `DMARC` TXT at `p=none` with `rua` first, tighten to `quarantine` after two weeks of clean reports.
6. **Verification upgrade** — keep DoH pre-filter, add optional paid verifier for catch-all resolution. Budget estimate at 10k contacts/month: roughly $10–50/mo depending on vendor; not verified in this research, confirm before committing.

Current blocker, unchanged: Mailgun sending records for `cyberalsolutions.com` are not configured — SPF is `v=spf1 include:spf.runbox.com -all` (**hard fail**, so Mailgun will fail SPF today), `k1._domainkey` is empty, no `email.` CNAME. Zone is on Google Cloud DNS with no `gcloud` CLI on either host. **This is the single highest-value 30-minute fix in the whole plan** and it needs dashboard access.

---

## 6. Execution order

Each phase ships independently and leaves the app working. Estimates assume focused solo work.

### Phase 0 — Truth and guardrails (0.5 day)
- `app/places.py` skeleton + `places_dataset` / `places` / `place_links` migrations via the existing additive migration path.
- Attribution string rendered in UI footer and in any export.
- Test: `test_places.py` asserts licence fields are non-null and that OSM rows carry a `partition='osm'` marker.
- **Done when:** schema migrates on the live DB without touching existing rows, and 146+ tests still pass.

### Phase 1 — Discovery base layer (3–4 days)
- `app/sources/overture.py` with a pinned release constant; CLI flag `python -m app.sources.overture --bbox ... --release ...`.
- Ingest `ohio_valley_places.parquet` → `places` (expect order 10^5 rows for that bbox; measure, don't assume).
- `app/places.py` geo search: bbox prefilter on `(lat, lon)` then haversine, `name_norm` dedupe.
- `POST /api/discovery/runs` + status endpoint + review list.
- **Done when:** a run for "Marietta, OH, 40 km, contractors with no website" returns a non-empty, de-duplicated list, and re-running it returns the identical set (idempotency test).

### Phase 2 — Enrichment pipeline (2–3 days)
- `enrichment_jobs` worker alongside the existing sequence worker.
- Site crawl → `audit_site()` findings → `contacts` with `mailto:` extraction behind the existing `is_placeholder` / `foreign_domain` / `directory_host` guards.
- Tech detection from headers/HTML; DNS auth via existing `domain_auth()`.
- Email pattern inference stored as `inferred`, verified before ever being `SENDABLE`.
- **Done when:** a candidate with a live site yields findings with evidence URLs, and a candidate with no email is never turned into an invented address (explicit test).

### Phase 3 — Deliverability infrastructure (3–4 days)
- `sending_domains` / identities, computed warmup cap, rotation.
- Circuit breaker + dashboard banner.
- One-click unsubscribe headers.
- **Done when:** sending is refused when the cap is hit or the breaker is open, and the header set is asserted in a test on the Mailgun payload.

### Phase 4 — Inbound and attribution (3 days)
- Public landing pages at `/p/{slug}`, no auth, honeypot + rate limit.
- `POST /api/inbound/{slug}` → `form_submissions` → `contacts` → `activities`; UTM captured and propagated.
- Attribution: a reply or form submission credits the sequence/step/variant that caused it.
- **Done when:** a form POST creates exactly one contact and one activity, and a duplicate POST within 60s does not duplicate the lead.

### Phase 5 — CRM-lite and client reporting (3 days)
- `deals` with stages, drag-free stage buttons, `activities` timeline on the business view.
- Per-client rollup: sent, delivered, opened, replied, meetings, won, revenue — **every number labelled with the table it counts** (the `feb3ab4` lesson, now a house rule).
- Monthly PDF via the existing `pdf-generation` approach.
- **Done when:** the client-facing report renders from real rows and no tile can show a count whose source table is empty.

### Phase 6 — Learning loop (2–3 days)
- Use existing `sequence_steps.variant_of`: variant assignment per contact, `events` rollup by variant, winner promotion.
- Reply classification (interested / not now / unsubscribe / out of office) → stage transition.
- **Done when:** variant stats come from `events`, not from a second counter that can drift.

**Total: roughly 17–20 focused days.** Phases 1–3 alone (≈10 days) are the difference between a demo and a product; 4–6 are what make it a *marketing app* rather than a mail-merge.

---

## 7. Cost model

| Item | Cost | Notes |
|---|---|---|
| Overture Places | $0 | CDLA-Permissive-2.0, attribution required |
| Foursquare OS Places | $0 | Apache-2.0 + NOTICE |
| OSM | $0 | ODbL; keep partitioned |
| Ohio public records | $0 | Manual/portal scraping, must respect robots + rate |
| Mailgun | already paid | existing account |
| Email verification (optional) | ~$10–50/mo at 10k | **estimate, unverified** |
| Disk | ~1–3 GB per state subset | parquet source is large; store only the bbox subset |
| Incremental vs Resquared | **$0/seat vs $550–600/mo** | Resquared bundles 14.7M businesses + managed warm domains; we replicate the first, own the second |

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Someone "just adds a Google Places key" | **High** — contractual violation, the exact prohibited use | Licence column on every dataset row; source adapters are the only ingest path; ToS note in `app/sources/README` and in the skill |
| ODbL share-alike attaches to our proprietary DB | Medium | Separate partition, Collective-Database posture, one legal review before shipping OSM rows to customers |
| Domain burned by volume | **High** | Warmup ladder + breaker + dedicated sending domain, phase 3 before any volume |
| Ingested POIs are stale (businesses closed) | Medium | Two sources + `is_closed` + release-over-release diff |
| Discovery returns the client's own competitors | Medium | Reuse the direction guard `cde7d28`; explicit test |
| Invented contacts creep back in | **High** | `inferred` status is never `SENDABLE`; existing three guards retained; test asserts no address without a source URL |
| Scope creep into a full CRM/SEO suite | Medium | Phases 4–6 are the boundary; content/SEO/social stay in the existing Cyberal tooling |

---

## 9. Open questions for Ryan

These change the build, so they come before code:

1. **Who is the customer?** Just Cyberal's own outreach, or do you intend to sell LocalReach to other agencies? If the latter, multi-tenant billing and per-tenant sending domains move into Phase 1, and that is +5 days.
2. **Geography?** Ohio Valley only (one bbox, ~10^5 POIs, simple) or nationwide from day one (~10^7, needs Postgres/PostGIS instead of SQLite)? I recommend Ohio Valley first — you sell to local businesses, and a smaller base keeps dedupe honest.
3. **Email finding policy?** Published-addresses-only (highest quality, lower volume, zero risk) or pattern-inferred-plus-verified (2–3× the volume, small bounce risk)? I recommend published-only until deliverability is proven, then add inferred behind a flag.
4. **Do you have dashboard access to Google Cloud DNS for `cyberalsolutions.com`?** SPF currently hard-fails Mailgun, so nothing sends legitimately today. This is the one item blocking real outreach regardless of the rest of the plan.

---

## 10. What I would do first

Wire the Mailgun DNS records (30 minutes, needs your dashboard), then Phase 1. Those two together give you something no competitor in your market has: a Marietta-area list of businesses with a *verifiable defect* their site has, at $0 per record, with a licence you can show a client.
