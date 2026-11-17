# LocalReach — usability review as a marketing app

**Date:** 2026-11-17
**Subject:** the shipped SPA at `https://reach.cyberalsolutions.com`
**Reviewed build:** `3972e43`; fixes landed on top of it in this pass
**Method:** ran the real container image against a seeded throwaway database on `localhost`,
walked every screen at 1440×900 and at 390×844 (phone), exercised every control, then queried
the database to check whether the control actually did what the toast claimed. Competitor
benchmarks: Instantly (pre-launch checklist, warmup), Smartlead (campaign sequence), Resquared
(positioning) — read from their docs.

A toast is not proof. Every finding below was confirmed twice: once in the UI, once in SQL
(or in DOM geometry, where the defect was layout).

---

## 1. Does it make sense as a marketing app?

Mostly yes, and the core loop is the right one for this product:

    import prospects → research their site → assign to a client → enroll in a sequence
    → review what the engine wrote in the Outbox → send

That is the same loop Instantly and Smartlead sell, minus the multi-mailbox warmup machinery,
plus the thing they don't have: an agent that reads the prospect's own website and writes the
first email about what it found. The "as the client" voice model (outreach signed as the
client business, with the agency's address in the footer) is correct for an agency selling
local SEO/IT.

The gaps were not conceptual. They were comprehension and trust gaps: several screens told the
operator something happened when it didn't, some controls acted on rows you couldn't see, a
second workspace could see the first workspace's prospects, and a phone couldn't reach half the
table.

## 2. Findings and their disposition

Severity: **P0** = wrong data or wrong action, **P1** = operator can't understand or can't
complete the job, **P2** = friction.

### P0 — correctness

**F1. "Score selected" reported success and persisted nothing. — FIXED, proven.**
Selecting rows and clicking *Score selected* toasted "Scored 2 leads" while the FIT column and
both `client_leads.fit` and `scores` stayed unchanged.

Three separate defects were behind it:

1. the handler never wrote anything;
2. `scoreSelected()` had no per-lead error handling, so one bad row would abort the rest of a
   batch silently;
3. the scorer itself had nothing to score against — see F12.

*Fixed:* the handler upserts `scores(business_id, fit_score, factors, computed_at)` and updates
`client_leads.fit` for the selected client; the front end buckets the selection **by client**
(fit is client-relative) and wraps each lead in try/catch, reporting the first failure and the
count instead of dying.
*Proof:* selecting all five rows produced the toast **"Scored 5 leads"** and the fits
recomputed in place; `scores` now holds 7 rows with populated `factors`, and `client_leads.fit`
holds 95 / 90 / 82 / 95 / 90.

**F2. Every workspace could see every other workspace's unassigned prospects. — FIXED, proven.**
A second workspace immediately showed `Unassigned 4` — the prospects imported into Cyberal.
`routes_leads._unassigned()` selected `businesses` with no `client_leads` link and no workspace
predicate, so the prospect pool was global: workspace B could assign and email workspace A's
imports.
*Fixed:* a `workspace_businesses` table (cascade FKs) written at both ingestion seams — import
and research discovery — with the unassigned bucket and its count filtered through it.
*Proof:* `test_a_second_workspace_cannot_see_another_workspaces_prospects` is green, and the
second workspace's leads view reads empty in the container.

**F3. Row selection survived a filter change, so bulk actions hit invisible rows. — FIXED.**
Selected a lead under *Unassigned*, switched to a client filter showing one row, clicked
*Score*: "Scored 2 leads". A bulk action that silently includes rows the operator can no longer
see is how the wrong forty people get emailed.
*Fixed:* changing the lead filter, the list filter or the client clears the selection, and the
selection bar states the count.

### P1 — the job can't be completed or understood

**F4. No way to change the send cap or the footer address after creation. — OPEN.**
`daily_send_cap` and `physical_address` are collected once in *New workspace*, then read-only.
CAN-SPAM makes the address legally load-bearing and the cap is the safety brake on a cold
outreach tool. The field is also labelled *Physical address (required in every footer)* with no
validation, so `Marietta, OH` (a city) is accepted — and that is exactly what production ships
in every footer.
*Fix:* a per-workspace *Settings* modal (address + cap), with a warning when the address has no
street number. Not done in this pass: it changes data the compliance footer depends on, so it
wants the owner's eye on the copy.

**F5. The phone layout was unusable. — FIXED, measured.**
At 390 px the nav rendered its six items in one 774 px row, so the page overflowed. Once the nav
wrapped, a second, subtler defect remained: the leads table was **766 px inside a 348 px panel**
and every ancestor reported `overflowX: visible`, so the Fit and Contact columns were not
scrollable — they were silently clipped and unreachable.
*Root cause:* `overflow-x: auto` alone does nothing here. A block-level table still grows to fit
its content, so the parent merely clips it. The table needs a cap *and* a scroll container.
*Fixed:* `@media (max-width:820px)` now sets
`table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch;white-space:nowrap;max-width:100%}`
plus `.panel,.content{min-width:0}` on the containers that were refusing to shrink.
*Proof:* at 390×844 DPR 2 on the final image — viewport 390, page `scrollWidth` 390, table 348 px
wide with `scrollWidth` 750 and `canScroll: true`.

**F6. The unsubscribe footer promised "one click" and delivered two. — FIXED.**
`GET /u/<token>` is a confirmation page by design; the suppression is only written on POST. The
copy now matches the behaviour at both call sites in `compliance.py`.

**F7. Empty states don't tell a new operator what to do. — PARTIAL.**
Outbox reads "Nothing here yet. Enroll leads and run a tick." — "tick" is engine vocabulary, and
there is no CTA. Research on a fresh workspace offers three buttons (*Research site*, *Research
with the agent*, *Research all with sites*) with no explanation of the difference, and a
`templates only` pill that doesn't say what it means or how to change it. The latter matters
much more than it looks: see F13.

### P2 — friction

**F8. Inbound messages show no sender. — OPEN.** Rows for inbound read `To —`, `From —`; the
table and the message modal never name the person who replied.
**F9. Sequence detail is engine-facing. — OPEN.** A call step renders as the literal
`(no subject)`; schedule is `weekdays_9_16`; no sent/open/reply counts.
**F10. `{{ai_snippet}}` appears as a raw template token — OPEN** in the Voice tab explainer.
**F11. Assign is one lead at a time. — OPEN BY CHOICE.** With 40 imported prospects that is 40
modals, but bulk assignment needs a UI decision about multi-client assignment and is the one
item here that can email the wrong person if rushed.

### Found in this pass

**F12. The scorer had nothing to score against, and said so by returning "40". — FIXED.**
`Fit 91` collapsed to `40` with an empty `factors` object. Two causes:

* the *seed data* had `category = NULL` on every business, so no client had an ICP at all. The
  harness was producing "every score is 40" and the scorer was telling the truth.
* the app treated an empty ICP as a neutral factor set and emitted a base score anyway.

*Fixed:* an empty ICP with no supplied categories now derives one from the client's own
categorised leads, and if that set is still empty the endpoint returns **422 with the reason**
rather than overwriting curated fits with a meaningless 40. `compute()` opens with
`factors = {"base": 40}` so every score explains its baseline. The seeder was rewritten to set a
category on every business and to print `categorised: N`; it now reports `categorised: 9`.
*Proof:* Belpre scores **75** (40 base + 25 category + 10 established), `explains: true`.

**F13. "Save as list" was a one-way write. — FIXED.**
`POST /api/lists` wrote `lists` and `list_members`, toasted "Saved list with 2 members", and the
list was then unreadable forever: no collection route, no list filter, no way back to the
selection a marketer had just built. A saved list you cannot read is not a saved list.
*Fixed:* `GET /api/lists?workspace_id=` returns lists with member counts;
`GET /api/leads?list_id=` filters through `list_members` with an explicit ownership check
(another tenant's list id is a 404, not a peek); and the Leads toolbar has an *All lists* select
that repopulates after a save.
*Proof:* the select reads `All lists | Q4 roofers — tier A (2)`, and choosing it takes the table
from 6 rows to the 2 members plus their client group header.

---

## 3. What shipped

| # | Fix | Files | Proof |
|---|---|---|---|
| 1 | score persists (`scores` upsert + `client_leads.fit`) | `routes_data.py` | toast "Scored 5 leads"; 7 `scores` rows; fits 95/90/82 |
| 2 | per-workspace prospect pool + scoping | `schema.sql`, `routes_data.py`, `routes_leads.py` | isolation test green, second workspace empty |
| 3 | selection cleared on filter/list/client change | `index.html` | manual |
| 4 | empty ICP derives from client leads, else **422** | `routes_data.py` | `test_scoring_refuses_to_rank_a_client_with_no_icp` |
| 5 | every score explains its baseline (`{"base": 40}`) | `scoring.py` | Belpre 75, `explains: true` |
| 6 | batch scoring survives one bad row | `index.html` | per-lead try/catch, failure toast |
| 7 | phone: nav wraps, table becomes its own scroll container | `index.html` | table 348 px, `scrollWidth` 750, page 390 |
| 8 | footer copy matches the two-step opt-out | `compliance.py` | existing footer tests |
| 9 | saved lists readable and filterable | `routes_data.py`, `routes_leads.py`, `index.html` | `All lists | Q4 roofers — tier A (2)` |
| 10 | seeder mirrors production scoping and categories | `lr_review_seed.py` | `leads: 5`, `categorised: 9` |

**Still open:** F4 (workspace settings — needs owner sign-off on compliance copy), F7 empty
states, F8 inbound sender, F9 sequence detail, F10 token in copy, F11 bulk assign.
**Non-code blocker:** `llm.configured: false` — research runs on templates, not a model. This is
the largest gap between the app and "AI outreach platform", and it is a config decision
(base URL + key), not a code fix.

## 4. Verification

- `172 passed` in 2.58 s (`.venv-test`), including the new saved-list and empty-ICP tests.
- inline JS re-extracted (80,377 bytes) and `node --check` → **JS OK**.
- final image `sha256:a426c44176cb57f12f99399289a2584a57beff3b0beebc004453abf77760a70a`,
  run as `lr_rev_9` on `:8091` against the seeded throwaway DB; `GET /` → 200.
- list route read live from that image before the UI walk, so the UI was tested against the
  behaviour that exists, not the behaviour intended.
- every changed screen re-walked at 1440×900 and 390×844.
- verified against the rendered page and the database, never against intent.
