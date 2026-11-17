# LocalReach → full marketing platform, with OSINT lead discovery

**Date:** 2026-11-17
**Status:** plan, not started
**Scope:** `db/schema.sql`, new `app/places.py`, new `app/sources/`, `app/discover.py`, `app/routes_discovery.py`, `app/routes_inbound.py`, `app/routes_crm.py`, `app/routes_reporting.py`, `app/deliverability.py`, `app/static/index.html`, new `tests/test_places.py`, `tests/test_discovery.py`, `tests/test_deliverability.py`, `tests/test_inbound.py`, `docs/`

---

## 1. What was asked

> "do some research and plan out what it would take to make this a full on marketing app, with the ability to use OSINT to find lead data"

...followed by two corrections on review:

> "i want it to be based on what ever the business that is being researched is, look up their geography"
> "3. use common osint techneques to find emails"

So: geography is **derived from the researched business** (§ 4.3), and email discovery uses **standard OSINT technique** rather than published-addresses-only (§ 4.4) — with the statutory limits in § 3.4 as hard edges, not suggestions.

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

### 3.4 Two statutory landmines in email discovery

These are not vendor policy — they are federal law, and both are the *default* behaviour of every naive email-finder build.

**15 U.S.C. § 7704(b)(1)(A)(i) — harvesting.** It is an aggravated violation to originate a message when the address was "obtained using an automated means from an Internet website or proprietary online service operated by another person, and such website or online service included, at the time the address was obtained, a notice stating that the operator ... will not give, sell, or otherwise transfer addresses maintained by such website or online service to any other party for the purposes of initiating ... electronic mail messages."

Consequence: **automated collection of addresses from third-party sites that post an anti-harvesting notice is off limits.** Directory and portal sites are exactly where such notices live (BBB, chamber listings, lead-gen portals, SaaS directories). We therefore never scrape addresses from a third-party site. First-party sites — the prospect's own domain — are crawled only for addresses the business itself publishes, and we honour `robots.txt`. If a crawled site declares a no-transfer policy, we record `harvest_notice=1` and drop the address rather than argue about it.

**15 U.S.C. § 7704(b)(1)(A)(ii) — dictionary attacks.** Likewise aggravated: addresses "obtained using an automated means that generates possible electronic mail addresses by combining names, letters, or numbers into numerous permutations."

Consequence: **no brute-force `first.last` sweeps.** Generating 40 permutations per person and probing SMTP is precisely the prohibited pattern, and it also gets the domain blocklisted. What we do instead is narrower and defensible: **derive a pattern from an already-confirmed address at that domain, apply it to one named individual whose name we observed on that domain's own site, label the result `inferred`, and verify it before it is ever sendable.** One evidenced candidate per person, never a sweep.

Both provisions are tethered to a message that violates § 7704(a), so a compliant message is the primary defence — but we are not going to build a product that depends on that reading.

**Third-party API terms, checked not assumed:**

| Provider | What its terms say | Effect on us |
|---|---|---|
| **Brave Search API** | §Restrictions (i): "store, cache, or create a database of Search Results, in whole or in part, other than transient storage required for operation" | **Cannot** be used to mine or retain prospect data. Usable, if ever, only as a live single lookup whose result is not persisted. |
| **Bing Search API** | Retired **11 Aug 2025**; replaced by "Grounding with Bing" inside Azure AI | Not an option at all. |
| **Hunter.io** | Content Policy: sharing or reselling Hunter data requires prior written authorisation; no bulk export of individual-level data for redistribution; records labelled `public` vs `inferred`; SMTP-verified at entry | Usable as **internal** enrichment for our own outreach, **never** white-labelled as our database. Their public/inferred labelling is the same discipline §3.4 imposes on us. |
| **Common Crawl** | 501(c)(3), free copy of the web for research and analysis | Lawful bulk source for **finding** contact pages; we still only keep addresses published by the business itself. |
| **Wayback Machine CDX** | Public archive | Excellent for addresses published then removed. Same rule: first-party only. |

That is the whole OSINT budget: no SERP vendor, no scraped social graph, no purchased data.


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

-- Geography is DERIVED from the researched business, never typed in by hand.
CREATE TABLE IF NOT EXISTS client_geo (
  id              TEXT PRIMARY KEY,
  workspace_id    TEXT NOT NULL REFERENCES workspaces(id),
  business_id     TEXT NOT NULL REFERENCES businesses(id),
  label           TEXT,                   -- "Marietta shop", "Parkersburg branch"
  lat REAL, lon REAL,
  city TEXT, state TEXT, postal TEXT, county TEXT,
  radius_km       REAL NOT NULL DEFAULT 40,
  use_county      INTEGER NOT NULL DEFAULT 0,
  derived_from    TEXT NOT NULL,          -- overture_match | site_address | public_record | manual
  evidence        TEXT,                   -- the URL / place id / filing that produced it
  confidence      REAL,
  created_at      TEXT NOT NULL,
  UNIQUE (workspace_id, business_id, label)
);
CREATE INDEX IF NOT EXISTS idx_client_geo ON client_geo(workspace_id, business_id);

-- One row per address we ever considered, with the method that produced it.
-- `contacts.email_source` stays as the coarse label; this is the audit trail.
CREATE TABLE IF NOT EXISTS contact_sources (
  id              TEXT PRIMARY KEY,
  contact_id      TEXT REFERENCES contacts(id),
  business_id     TEXT NOT NULL REFERENCES businesses(id),
  email           TEXT NOT NULL,
  method          TEXT NOT NULL,          -- site_crawl | wayback | commoncrawl | rdap |
                                          -- security_txt | ct_subdomain | public_record |
                                          -- pattern_inferred | manual
  source_url      TEXT,                   -- the page the address was printed on
  observed_at     TEXT,
  is_inferred     INTEGER NOT NULL DEFAULT 0,
  harvest_notice  INTEGER NOT NULL DEFAULT 0,  -- site declared a no-transfer policy; unusable
  robots_allowed  INTEGER,
  confidence      REAL,                   -- 0..1 heuristic, not a verification verdict
  verification    TEXT,                   -- none | dns | smtp_ok | smtp_catchall | smtp_bad | paid
  verified_at     TEXT,
  rejected_reason TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contact_sources_email ON contact_sources(email);

-- Learned per-domain conventions, derived from confirmed addresses only.
CREATE TABLE IF NOT EXISTS email_patterns (
  id              TEXT PRIMARY KEY,
  domain          TEXT NOT NULL,
  pattern         TEXT NOT NULL,          -- {first}.{last} | {f}{last} | {first} | ...
  evidence_count  INTEGER NOT NULL DEFAULT 0,
  sample_email    TEXT,
  catch_all       INTEGER,
  mx_host         TEXT,
  last_seen       TEXT NOT NULL,
  UNIQUE (domain, pattern)
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
| `app/geo.py` | **new** — `resolve_client_geo(business_id)` → `client_geo` rows; radius by category; optional county polygons from Overture `divisions` |
| `app/emailfind.py` | **new** — the OSINT ladder (below): `find_addresses(business_id)` → `contact_sources` rows, each with method + evidence |
| `app/deliverability.py` | domain auth checks, warmup ladder, cap enforcement, bounce/complaint circuit breaker |
| `app/routes_discovery.py` | `POST /api/discovery/runs`, `GET /api/discovery/runs/{id}`, `POST /api/discovery/candidates/{...}/queue` |
| `app/routes_inbound.py` | public `GET /p/{slug}`, `POST /api/inbound/{slug}` (no auth, rate-limited, honeypot) |
| `app/routes_crm.py` | deals CRUD, stage transitions, activities |
| `app/routes_reporting.py` | per-client rollup + monthly PDF |

### 4.3 Discovery flow, concretely

The operator picks **a client and a category set. Nothing else.** Geography is a property of the business under research, so it is resolved from evidence:

1. **Resolve the client's geography** (`app/geo.py`), in this order, stopping at the first hit:
   - **Overture match on domain** — query `places` where `websites` contains the client's registrable host. Overture carries `websites`, so a domain match gives lat/lon, city, county and the POI's own category in one step. This is the primary path and it is free.
   - **Site address** — crawl the homepage/contact page for a `schema.org` `PostalAddress` or a footer address; geocode it against the nearest Overture place in the same postal code (no external geocoder, no Google).
   - **Public record** — Ohio SOS / county filing address.
   - **Manual pin** — UI fallback, recorded as `derived_from='manual'` with the operator as evidence.
   Every result lands in `client_geo` with `derived_from`, `evidence` and `confidence`, so "why is this searching Cleveland" is always answerable.
2. **Derive the search area.** Radius defaults by the client's own category, not a global constant — a home-services contractor serves a county; a dentist serves a town:

   | Client category | Default radius | Why |
   |---|---|---|
   | Contractors, excavating, roofing, HVAC | 40 km | service-area business, drives to the job |
   | IT/MSP, marketing, bookkeeping | 60 km | remote-capable, metro-wide |
   | Dental, medical, chiropractic | 10 km | travel tolerance is low |
   | Legal, accounting | 25 km | county seat gravity |
   | Retail, restaurant, salon | 8 km | walk/drive-by |

   `use_county=1` swaps the radius for the county polygon from Overture's `divisions` theme, which matters in rural SE Ohio where a 40 km circle spills into three states but the customer base is one county.
3. **Build the category set from the client's offer**, via `app/taxonomy.py` — Overture category → our taxonomy → "who buys this". A web/IT seller targets businesses whose site is weak *or absent*, across every local trade; a roofer targets property managers. This mapping is data, not code, so it can be edited without a deploy.
4. `discovery_runs` row is created carrying the resolved `client_geo.id`, the bbox and the category set; a worker claims it (`queued → running`), same claim pattern as `research_queue`.
5. Candidate query: places inside the area, `is_closed = 0`, `confidence >= X`, category in set, **not already a `businesses` row**, **not the client**, **not a client's competitor trade** (the direction guard from `cde7d28`).
6. Candidate filters that produce the money list:
   - `websites` empty → "no website" (the strongest signal for a web/IT seller)
   - website present → enqueue `site_crawl` + `audit`, then rank by `audit_site()` defect weight
   - no published email → mark as phone task, not email task (existing `tasks` behaviour), **unless** the ladder below finds a verified address
7. Accepted candidates become `businesses` rows via `place_links` (match on domain, then `name_norm` + postal, then phone). Never create a duplicate business when a domain already exists.
8. `scores` are computed with the existing three-part model; `reach_score` now uses real verification.
9. Operator sees a review screen with reason + evidence per row, selects, and `POST /api/leads/queue` puts them into `client_leads` with `origin='discovery'`.

Every step is idempotent per `(run_id, place_id)`.

### 4.4 The email-discovery ladder (`app/emailfind.py`)

Run cheapest and most lawful first; stop as soon as one address verifies for the person who matters. Every attempt writes a `contact_sources` row **even when it fails**, so the UI can show "we looked here and found nothing" instead of implying nobody tried.

| # | Method | What it does | Confidence | Notes |
|---|---|---|---|---|
| 1 | `site_crawl` | Fetch homepage, `/contact`, `/about`, `/team`, `/staff`, sitemap; extract `mailto:`, `schema.org` `email`, and regex over visible text; honour `robots.txt` | 0.95 | The address the business chose to publish |
| 2 | `security_txt` | `/.well-known/security.txt` per RFC 9116 — often names a real human | 0.8 | One request, frequently overlooked |
| 3 | `wayback` | Wayback CDX for the same paths; recovers addresses since removed | 0.7 | Public archive; first-party pages only |
| 4 | `commoncrawl` | CDX/WARC lookup for the domain's contact pages at scale, without touching the origin | 0.7 | Free, lawful bulk index |
| 5 | `rdap` | Registrant/tech email from RDAP (WHOIS successor) | 0.6 | Often redacted post-GDPR; when present it is authoritative |
| 6 | `ct_subdomain` | `crt.sh` → subdomains (`mail.`, `portal.`, `intranet.`) → crawl those for contact blocks | 0.5 | Surfaces staff directories the main site hides |
| 7 | `public_record` | Ohio SOS filings, licensing boards, county permits, public bid portals | 0.8 | Frequently contains the owner's real name — which feeds step 8 |
| 8 | `pattern_inferred` | **Only** with (a) a confirmed address at the domain establishing a pattern in `email_patterns`, and (b) a named person observed on that domain's own site. Generates **one** candidate | 0.4 → upgraded by verification | Never a sweep; see § 3.4 |

Then verification gates, using the existing `app/verify.py` primitives:

- DNS MX/A pre-filter (already implemented).
- **SMTP RCPT** — one connection, one recipient, polite HELO, per-domain daily ceiling, immediate backoff on greylisting. Verdicts: `smtp_ok`, `smtp_bad`, `smtp_catchall`.
- **Catch-all domains**: SMTP cannot confirm an individual. Mark `smtp_catchall`, keep the address out of the sendable set, and route that person to a phone/call task — which is exactly what `tasks` already supports.
- Optional paid verifier for catch-all resolution, internal use only per § 3.4.

`contacts.email_status` already accepts `verified | risky | invalid | unverified | guessed`, and `SENDABLE = {"verified"}`. **`guessed` maps to `pattern_inferred` and never becomes sendable without a verification verdict.** The ladder changes what fills that column, not what may be sent to.

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

### Phase 1 — Discovery base layer and derived geography (4–5 days)
- `app/sources/overture.py` with a pinned release constant; CLI flag `python -m app.sources.overture --bbox ... --release ...`.
- Ingest `ohio_valley_places.parquet` → `places` (expect order 10^5 rows for that bbox; measure, don't assume).
- `app/places.py` geo search: bbox prefilter on `(lat, lon)` then haversine, `name_norm` dedupe.
- **`app/geo.py`** — `resolve_client_geo()`: Overture domain match → site `PostalAddress` → public record → manual pin; category-derived radius table; `use_county` polygon option. Every row carries `derived_from` + `evidence`.
- `POST /api/discovery/runs` + status endpoint + review list.
- **Done when:** pointing the app at a client domain resolves its own geography without the operator typing a city, a run for "that area + that client's buyer profile" returns a non-empty de-duplicated list, and re-running returns the identical set (idempotency test). Second test: a Columbus client and a Marietta client produce different bboxes from the same code path.

### Phase 2 — Enrichment pipeline and the email ladder (3–4 days)
- `enrichment_jobs` worker alongside the existing sequence worker.
- Site crawl → `audit_site()` findings → `contacts` with `mailto:` extraction behind the existing `is_placeholder` / `foreign_domain` / `directory_host` guards.
- Tech detection from headers/HTML; DNS auth via existing `domain_auth()`.
- **`app/emailfind.py`** — the eight-step ladder in § 4.4, each step writing a `contact_sources` row whether it hits or misses: `site_crawl`, `security_txt`, `wayback`, `commoncrawl`, `rdap`, `ct_subdomain`, `public_record`, `pattern_inferred`.
- SMTP RCPT verification with per-domain ceilings and catch-all detection; `smtp_catchall` routes to a phone task instead of a send.
- `email_patterns` populated **only** from confirmed addresses; `pattern_inferred` produces exactly one candidate per named person.
- **Done when:** a candidate with a live site yields findings with evidence URLs; a no-email candidate is never turned into an invented address; and a test asserts `pattern_inferred` rows are created with exactly one candidate per person and are not `SENDABLE` until a verdict lands.

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

**Total: roughly 19–22 focused days.** Phases 1–3 alone (≈11 days) are the difference between a demo and a product; 4–6 are what make it a *marketing app* rather than a mail-merge.

---

## 7. Cost model

| Item | Cost | Notes |
|---|---|---|
| Overture Places | $0 | CDLA-Permissive-2.0, attribution required |
| Foursquare OS Places | $0 | Apache-2.0 + NOTICE |
| OSM | $0 | ODbL; keep partitioned |
| Ohio public records | $0 | Manual/portal scraping, must respect robots + rate |
| Common Crawl index | $0 | 501(c)(3); used to *locate* contact pages, not to keep third-party addresses |
| Wayback CDX | $0 | public archive, first-party pages only |
| RDAP / `security.txt` / `crt.sh` | $0 | registrant contact, RFC 9116, cert transparency |
| Gmail/Yahoo bulk-sender compliance | $0 | SPF + DKIM + DMARC + one-click List-Unsubscribe; DNS work only |
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
| Geography resolves wrong (client has no site, or a virtual address) | Medium | `derived_from` + `evidence` on every row; confidence threshold; manual pin always available and visibly labelled |
| Someone builds the naive email finder — 40 permutations per name, SMTP-probed | **High** — it is § 7704(b)(1)(A)(ii) by definition and it burns the sending domain | `email_patterns` requires ≥1 confirmed address at the domain; `pattern_inferred` emits exactly one candidate per named person; test asserts cardinality |
| Someone scrapes a directory for addresses that posts a no-transfer notice | **High** — § 7704(b)(1)(A)(i) aggravated violation | Third-party sites are never an address source; `harvest_notice` column records and rejects; crawl allowlist is first-party domains only |
| A SERP API is wired in to "just find contact pages" | Medium — Brave forbids storing results; Bing is retired | No SERP dependency anywhere in the design; Common Crawl/Wayback cover the need lawfully |
| Invented contacts creep back in | **High** | `inferred` status is never `SENDABLE`; existing three guards retained; test asserts no address without a source URL |
| Scope creep into a full CRM/SEO suite | Medium | Phases 4–6 are the boundary; content/SEO/social stay in the existing Cyberal tooling |

---

## 9. Open questions for Ryan

Settled since the first draft:

- **Geography** — *answered*: derived per researched business (see § 4.3), not a global input.
- **Email finding** — *answered*: common OSINT techniques, constrained by § 3.4 and gated by verification (see § 4.4).

Still open, and these change the build:

1. **Who is the customer?** Just Cyberal's own outreach, or do you intend to sell LocalReach to other agencies? If the latter, multi-tenant billing and per-tenant sending domains move into Phase 1, and that is +5 days.
2. **Do you have dashboard access to Google Cloud DNS for `cyberalsolutions.com`?** SPF currently hard-fails Mailgun, so nothing sends legitimately today. This is the one item blocking real outreach regardless of the rest of the plan.
3. **Is a phone number an acceptable fallback when a domain is catch-all?** Right now the design routes catch-all addresses to a call task rather than guessing. If you would rather send to catch-all addresses anyway, that is a deliberate deliverability bet and should be a flag, not a default.

---

## 10. What I would do first

Wire the Mailgun DNS records (30 minutes, needs your dashboard), then Phase 1. Those two together give you something no competitor in your market has: a Marietta-area list of businesses with a *verifiable defect* their site has, at $0 per record, with a licence you can show a client.
