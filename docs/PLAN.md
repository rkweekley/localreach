# LocalReach — full build and launch plan

Written 2026-11-13. Companion to the working code in this repo.

**Scope note on research:** the target you asked me to study, `app-v2.re2.ai`,
sits behind a login. I read its public front end (Next.js App Router shell,
positioning copy) and then took the product mechanics from Resquared's own
documentation at `learn.re2.ai` and `re2.ai`. Everything below marked
**[observed]** came from those public sources; everything marked **[assumed]**
is my inference and should be treated as a hypothesis until it earns revenue.

---

## 1. What Resquared actually sells

[observed] Positioning: *"AI-powered outreach platform to discover and connect
with small business."* It is a sequence of five mechanics, and the product is
the fact that they hand off to each other without copy-paste:

1. **Search** over a claimed 14M+ US business database, Yelp-style category
   taxonomy, filtered by radius, rating, review count, years in business,
   ownership, and whether an email is verified.
2. **AI Profile** — you feed it your website and documents; it extracts offer,
   ideal customer profile, and tone. A "training bar" fills as it ingests.
3. **Snippets** — per-prospect copy at 60 / 100 / 150 words with a
   tone-creativity dial, generated from the profile plus the prospect's facts.
4. **Market Demand** — Google search volume, seasonality, competition by
   category and geography, so you know *who to call this month*.
5. **Sequences** — multi-step email plus call tasks, auto-stop on reply,
   shared across a team. **AI Agent** is literally an email address
   (`agent@re2.ai`): you email it "find garage door repair in Columbus" and it
   builds and runs a campaign.

Two deliberate product choices worth copying exactly:

- **No open tracking.** Pixels hurt deliverability and lie anyway. Reply rate is
  the only metric they optimize.
- **Shared warmed sending domains** rather than customer-owned mailboxes, so
  reputation is pooled and the customer never has to think about DNS.

## 2. What we build, and what we do not

**Build:** the mechanics above, on our own domain, with our own data and our own
provider accounts — so there is no seat license, no per-record fee, and no
vendor who can change terms on us.

**Do not build (phase 1):** a 14M-record verified-email database. That is a data
business, not a software feature; §5 covers buying access instead. Also out of
scope: a full CRM, a shared team inbox, and a mobile app.

**Non-negotiable:** the compliance gate is part of the product, not a checkbox.
Cold email at volume without it produces domain burns and, in some states,
statutory damages. This is the part where being cheap is expensive.

## 3. Architecture

```
              ┌──────────────────────────────────────────┐
  browser ───▶│  FastAPI (app/main.py)                   │
              │                                          │
              │  routes_data    workspaces/biz/search    │
              │  routes_campaigns  sequences/enroll/tick │
              │  routes_ai      profile/snippets/agent   │
              │  routes_webhooks   Mailgun inbound+events│
              └───────┬───────────────┬──────────────────┘
                      │               │
              ┌───────▼──────┐  ┌─────▼──────────┐
              │ SQLite (dev) │  │ Mailgun HTTP   │
              │ Postgres(prd)│  │ api.mailgun.net│
              └──────────────┘  └─────┬──────────┘
                                      │
                        inbound route /events ──▶ /webhooks/mailgun/*
```

Three deliberate architectural choices:

**Pull-based worker, not a background thread.** `POST /api/worker/tick` claims
due enrollments and sends. Consequences: it is testable without waiting on a
clock, running two replicas cannot double-send (the claim is a conditional
`UPDATE`), and every send has a row explaining itself. A cron job pings it every
five minutes; that is the whole scheduler.

**Compliance at send time.** See README. Suppression is evaluated when the
message is claimed, not when the sequence is built, which is the only ordering
that is actually true under concurrency.

**LLM optional.** `app/ai.py` calls an OpenAI-compatible endpoint if
`AI_BASE_URL` is set, otherwise it falls back to a deterministic template built
from the prospect's real facts. The exact text sent is stored on the message
row, so "why did you email me that" is answerable months later. Prompts are
constants in code, so a prompt change is a diff, not a mystery.

**Data model** (`db/schema.sql`): `workspaces`, `users`, `businesses`,
`contacts`, `ai_profiles`, `ai_profile_docs`, `sequences`, `enrollments`,
`messages`, `suppressions`, `scores`, `category_demand`, `tasks`. TEXT UUID
hex ids, ISO-8601 UTC timestamps, INTEGER booleans — one DDL file that runs on
both SQLite and Postgres.

## 4. Build phases

Phase 0 is done and verified in this repo. Everything after it is a plan with
acceptance criteria, not a promise.

### Phase 0 — core mechanics ✅ done

- [x] Portable schema, sqlite3 layer with WAL + busy timeout
- [x] Compliance gate: verified-only, role-mailbox block, daily cap, suppression, signed unsubscribe tokens
- [x] Three-part scoring with labelled proxies
- [x] Snippet generation with LLM + deterministic fallback
- [x] Mailgun transport with `DRY_RUN=1` writing to an outbox
- [x] Sequences, enrollments, claim-based worker tick
- [x] Inbound reply handling: classify → stop sequences → suppress
- [x] Email agent: plain-English instruction → drafted campaign (never auto-sends)
- [x] `scripts/demo.py` end-to-end, 9 passing tests

Acceptance evidence: `python3 scripts/demo.py` imports 8 businesses, scores them
(95/82/57 fit), enrolls all 8, blocks 4 at send time with named reasons
(`role_address` ×2, `unverified`, `suppressed`), sends 3 to the outbox, and the
agent drafts a 5-match campaign that stays in `draft`.

### Phase 1 — make it send real mail (1 week)

1. Buy/verify `mg.cyberalsolutions.com` in Mailgun: SPF, DKIM, DMARC at
   `p=none` first. Confirm the domain shows **verified** before anything else.
2. Inbound route: `match_recipient("agent@mg.cyberalsolutions.com")` →
   `forward("https://reach.cyberalsolutions.com/webhooks/mailgun/inbound")`
   plus a store step so a webhook failure does not lose the email.
3. Delivery events webhook → `/webhooks/mailgun/events` (delivered, bounced,
   complained, failed). Complaints must auto-suppress globally, immediately.
4. Send **five** emails to yourself and two colleagues. Confirm they land in
   the inbox, the unsubscribe link works, and the reply stops the sequence.
5. Only then `DRY_RUN=0`, at 10/day for a week.

Acceptance: a real reply from a real stranger stops the sequence and appears in
`suppressions`, and `bounced`/`complained` events measurably reduce later sends.

### Phase 2 — data layer (1–2 weeks, the real constraint)

Pick one source to start. See §5 for the tradeoffs; the recommendation is
OpenStreetMap plus manual verification for the first 500 records, because it is
free, legal, and forces the verification step to be real.

1. Import script: source → `POST /api/businesses/import` (CSV in, JSON out).
2. `email_status` must come from a verifier (ZeroBounce/NeverBounce, [assumed]
   ~$0.003–0.01 per address — confirm at signup). No verifier, no `verified`.
3. `category_demand` import from Google Ads Keyword Planner CSV
   (`keyword, state, city, monthly_volume, competition, seasonality`).
4. De-dupe by domain + normalized phone; a business with two locations is two
   rows, a business re-imported is the same row.

Acceptance: 500 real Ohio businesses, ≥60% with verified owner-level addresses,
and a search that returns them sorted by score in under 200ms.

### Phase 3 — sequences that survive contact with prospects (1 week)

1. Send-window enforcement (`weekdays_9_16` in the prospect's timezone, not ours).
2. Threading: step 2 sends as a reply to step 1 (same subject `Re:`, In-Reply-To).
3. Call tasks surface in `tasks` with `due_at`, and completing one advances the enrollment.
4. A/B variants (`variant_of` on steps) with reply rate per variant.
5. Hard bounce handling: 2 hard bounces on a domain → suppress the domain.

Acceptance: a 3-step sequence with one call task runs unattended for two weeks
with zero duplicate sends and zero sends outside the window.

### Phase 4 — deliverability at volume (ongoing, 4–8 weeks of patience)

1. Warm the domain: 10/day → 20 → 40 → 80, doubling no faster than weekly, and
   only while bounce rate < 2% and complaint rate < 0.1%.
2. Keep one sending subdomain for cold and a separate one for transactional.
3. Content rules enforced in code: no images, no more than one link, no
   attachments, plain-text-ish HTML.
4. Consider a dedicated IP only above roughly 50k/month — below that, shared IP
   reputation is better [assumed; confirm with Mailgun support at the threshold].

Acceptance: 30-day rolling bounce < 2%, complaint < 0.1%, and inbox placement
above 80% on a seed list (GlockApps/Mail-Tester).

### Phase 5 — the parts Resquared charges for (2–3 weeks)

1. Minimal UI: search table, business drawer with snippet preview, sequence
   builder, campaign stats. Server-rendered Jinja is enough; this is a tool for
   two people, not a SaaS launch.
2. Auth: one login, workspace-scoped, no public signup.
3. Team sharing: multiple users per workspace, assignment on `tasks`.

Acceptance: Ryan can go from "search Columbus roofers" to "3-step campaign in
draft" without touching curl.

## 5. Data layer: the honest options

| Source | Cost | Legal / practical | Verdict |
|---|---|---|---|
| **OpenStreetMap** (Overpass API) | free | ODbL — attribution required, share-alike on derived data | Start here. Names, categories, addresses, phones, websites. Rarely emails. |
| **Google Places API** | [assumed] ~$17–32 per 1k requests by SKU | **Terms forbid caching beyond 30 days** — cannot build a database from it | Usable only for live lookups, never as your store |
| **Yelp Fusion** | free tier, then paid | Display restrictions, no bulk storage | Skip for outbound |
| **Outscraper / similar scrapers** | [assumed] ~$1–3 per 1k records | Their ToS is your problem; quality varies | Tempting, treat with suspicion |
| **DataAxle / ZoomInfo-style** | [assumed] thousands/yr, contract | Clean licensing, real emails, minimums | The "grown up" answer once there is revenue |
| **State licensing boards** (Ohio eLicense, etc.) | free | Public record; often includes owner names | Best owner-name source. High value per hour of work. |
| **Chamber of commerce / BNI directories** | free | Public listings; small volume | Good for a 200-name hand-built list |

The 14M-record database is not the moat — the *verified, owner-level email* is.
One accurate `owner@` beats fifty `info@` addresses, and this app's gate already
refuses to send to the latter.

Practical recommendation: hand-build the first 500 from OSM + Ohio licensing
boards + a verifier. That list will out-perform a purchased 50k list on reply
rate, and it costs a weekend instead of a contract.

## 6. Deliverability plan

The failure mode that kills these projects is not code, it is a burned domain.

- **Subdomain for cold only.** `mg.cyberalsolutions.com`. Never let marketing
  blasts or a compromised WordPress plugin touch the cold reputation.
- **Authentication:** SPF, DKIM (Mailgun's), DMARC `p=none` → `p=quarantine`
  only after 30 clean days. Publish `rua` and actually read the reports.
- **Volume:** 40/day per mailbox default is deliberately conservative. Warm up
  in doublings. Never spike for a "campaign launch".
- **Content:** plain text, one link, no images, no tracking pixel, real postal
  address, working one-click unsubscribe (RFC 8058 `List-Unsubscribe-Post`).
- **List hygiene:** verified-only sends, hard-bounce → suppress domain, complaint
  → suppress globally. Both are already enforced or one line away.
- **Monitor:** Google Postmaster Tools, plus your own `/api/stats` bounce and
  complaint rates. If bounce > 2%, stop sending and fix the list — do not push
  through.

## 7. Legal and compliance

[CAN-SPAM, 15 U.S.C. §7704] requires: accurate `From`/`Reply-To`, non-deceptive
subject, a physical postal address, a functioning opt-out honored within 10
business days, and no sending to harvested/scraped addresses where the site
prohibited it. All of the first four are implemented; the fifth is a sourcing
decision and belongs to whoever imports the list.

State law matters more than people expect: [assumed, verify with counsel] 
Washington's CEMA and Ohio's own provisions can carry per-message statutory
damages, and several states treat role-address cold email as commercial
solicitation with extra requirements. The role-mailbox block is not just
deliverability hygiene — it is risk reduction.

Also: **do not** email addresses whose source prohibited it, do not mask the
sender, and keep the exact sent content (this app stores it) in case of a
complaint. Document the opt-out for any address that asks, ever.

## 8. Deployment

The recommendation is the box you already own, behind the proxy you already run.

| Option | Cost | Notes |
|---|---|---|
| **Mac Mini + Nginx Proxy Manager** (recommended) | $0 marginal | Already running Docker behind NPM. Add a container, proxy `reach.cyberalsolutions.com`, TLS via NPM. Downside: Docker does not auto-start after reboot — same fix you already know (open Docker, restart NPM). |
| Fly.io / Render | [assumed] $5–7/mo + Postgres | Auto-restart, managed TLS. Best if you want it to survive your house losing power. |
| VPS (Hetzner/DO) | $6–12/mo | Cheapest reliable always-on; you own the patching. |

Deployment steps (Mac Mini path):

```bash
docker build -t localreach .
docker run -d --name localreach --restart unless-stopped \
  -p 127.0.0.1:8030:8000 --env-file .env \
  -v /Users/cyberal/localreach/data:/data localreach
```

Then in NPM: proxy host `reach.cyberalsolutions.com` → `127.0.0.1:8030`, request
a Let's Encrypt cert, force SSL. Worker cron on the host, every 5 minutes:

```
*/5 * * * * curl -s -X POST -H "Authorization: Bearer $WORKER_TOKEN" \
  https://reach.cyberalsolutions.com/api/worker/tick > /dev/null
```

(`WORKER_TOKEN` is not yet enforced on the tick endpoint — add it before this
is exposed to the internet. Tracked as Phase 1 item 6.)

**Postgres migration trigger:** when you have more than one process writing or
more than ~100k businesses. The DDL is portable; the sqlite3 calls in `app/db.py`
are the only thing to swap.

## 9. Cost model

Marginal monthly cost at Phase 2 volumes (500 emails/month):

| Line | Cost |
|---|---|
| Mailgun (Foundation tier, 50k/mo) | [assumed] ~$35/mo — verify current pricing at signup |
| Email verification (500 @ ~$0.005) | ~$2.50 |
| Snippets via local Ollama | $0 |
| Snippets via cloud LLM (500 × ~1k tokens) | [assumed] <$2 |
| Hosting (Mac Mini) | $0 marginal |
| Domain/subdomain | $0 (existing) |
| **Total** | **~$40/month** |

Resquared's own plans are quote-based; published third-party figures put their
seats in the hundreds per month. The comparison is roughly $40/mo plus your
time versus a per-seat license — which is only a win if your time is cheaper
than the license, so the honest framing is *control and data ownership*, not
savings.

## 10. Risks, and what would make me stop

| Risk | Severity | Mitigation | Kill signal |
|---|---|---|---|
| Domain reputation burn | high | verified-only, caps, warmup, no pixels | bounce > 4% or complaint > 0.3% in a week → pause all sending |
| Data supplier ToS breach | high | OSM/boards first; no Places caching | a provider sends a letter |
| Reply classification misfires and keeps emailing an annoyed prospect | medium | classifier + human inbox review for first 200 replies | 2 misclassifications → route all replies to a human queue |
| Ohio/Northeast market too small for the ICP | medium | 500-record test list, measure reply rate before building more | reply rate < 0.5% on 300 sends with good copy |
| Maintenance rot on a two-person team | medium | one dependency-light app, no k8s, no queue server | anything that needs a dedicated on-call |
| LLM cost creep | low | local model default, template fallback | per-email cost > $0.02 |

The "kill signal" column is the point. A project like this fails quietly; these
are the numbers that tell you it is failing.

## 11. What to measure

- **Reply rate** (positive replies / delivered). The only metric Resquared
  optimizes. Target: 3–8% for a tight local list, <1% means the copy or the
  list is wrong.
- **Verified coverage** — % of imported businesses with a verified address.
  Below 50% and the data source is the problem, not the sequences.
- **Bounce rate** < 2%, **complaint rate** < 0.1% — these are stop-the-line numbers.
- **Time to first reply** — if the first 50 sends produce nothing, stop and
  reread the snippet output before scaling.

## 12. Next ten steps, in order

1. Verify `mg.cyberalsolutions.com` in Mailgun (SPF/DKIM/DMARC), confirm DNS.
2. Deploy the container behind NPM at `reach.cyberalsolutions.com`.
3. Add `WORKER_TOKEN` auth to `/api/worker/tick`.
4. Create the Cyberal workspace with the real postal address and cap of 10.
5. Ingest the Cyberal homepage/docs into the AI Profile; check readiness ≥ 60.
6. Build a 100-business list from OSM (Columbus garage doors + roofers).
7. Verify those 100 addresses; import with honest `email_status`.
8. Write one 2-step sequence + 1 call task. Send to yourself. Read it out loud.
9. `DRY_RUN=0`. Send 10. Read every reply, including the hostile ones.
10. Only then: warm up toward 40/day, and build the UI once the emails prove
    themselves.

Steps 1–3 are infrastructure. Steps 4–8 are the actual test of whether this is
worth running. Steps 9–10 are where you find out.
