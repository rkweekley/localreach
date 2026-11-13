-- LocalReach schema — portable DDL (SQLite dev / Postgres prod)
-- All ids are TEXT (uuid4 hex). Timestamps are ISO-8601 UTC TEXT.
-- Booleans are INTEGER 0/1.

PRAGMA foreign_keys = ON;

-- ============ tenancy ============
CREATE TABLE IF NOT EXISTS workspaces (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  slug          TEXT NOT NULL UNIQUE,
  physical_address TEXT NOT NULL,      -- required in every email footer (CAN-SPAM)
  daily_send_cap   INTEGER NOT NULL DEFAULT 40,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  email         TEXT NOT NULL,
  name          TEXT,
  role          TEXT NOT NULL DEFAULT 'member',  -- owner | manager | member
  created_at    TEXT NOT NULL,
  UNIQUE (workspace_id, email)
);

-- ============ business + contact graph ============
CREATE TABLE IF NOT EXISTS businesses (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  category      TEXT,                  -- taxonomy leaf, e.g. 'garage_door_repair'
  category_group TEXT,
  address1      TEXT,
  city          TEXT,
  state         TEXT,
  postal_code   TEXT,
  country       TEXT DEFAULT 'US',
  latitude      REAL,
  longitude     REAL,
  phone         TEXT,
  website       TEXT,
  domain        TEXT,
  rating        REAL,
  review_count  INTEGER DEFAULT 0,
  years_in_business INTEGER,
  is_franchise  INTEGER DEFAULT 0,
  employee_estimate INTEGER,
  -- provenance is mandatory: we must be able to say where every field came from
  source        TEXT NOT NULL,         -- osm | places | yelp | manual | csv
  source_ref    TEXT,                  -- e.g. place_id, osm id
  fetched_at    TEXT,                  -- when we last refreshed provider data
  license_note  TEXT,                  -- caching/attribution constraint for this row
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_biz_geo ON businesses (state, city, category);
CREATE INDEX IF NOT EXISTS idx_biz_domain ON businesses (domain);

CREATE TABLE IF NOT EXISTS contacts (
  id            TEXT PRIMARY KEY,
  business_id   TEXT NOT NULL REFERENCES businesses(id),
  full_name     TEXT,
  title         TEXT,
  email         TEXT,
  email_status  TEXT DEFAULT 'unverified', -- verified | risky | invalid | unverified | guessed
  email_source  TEXT,                  -- website | hunter | apollo | manual
  verified_at   TEXT,
  linkedin      TEXT,
  is_primary    INTEGER DEFAULT 0,
  created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_email ON contacts (email) WHERE email IS NOT NULL;

-- ============ lists ============
CREATE TABLE IF NOT EXISTS lists (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  name          TEXT NOT NULL,
  filter_json   TEXT,                  -- saved search filter for dynamic lists
  is_dynamic    INTEGER DEFAULT 0,
  created_by    TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_members (
  list_id       TEXT NOT NULL REFERENCES lists(id),
  business_id   TEXT NOT NULL REFERENCES businesses(id),
  added_at      TEXT NOT NULL,
  PRIMARY KEY (list_id, business_id)
);

-- ============ sequences ============
CREATE TABLE IF NOT EXISTS sequences (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  name          TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'draft', -- draft | active | paused | archived
  send_window   TEXT DEFAULT 'weekdays_9_16',  -- local-time window for the recipient
  stop_on_reply INTEGER DEFAULT 1,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_steps (
  id            TEXT PRIMARY KEY,
  sequence_id   TEXT NOT NULL REFERENCES sequences(id),
  step_order    INTEGER NOT NULL,
  channel       TEXT NOT NULL DEFAULT 'email', -- email | call | linkedin
  delay_days    INTEGER NOT NULL DEFAULT 0,
  subject_template TEXT,
  body_template TEXT,                  -- supports {{variables}} and {{ai_snippet}}
  variant_of    TEXT,                  -- A/B: step id this is a variant of
  created_at    TEXT NOT NULL,
  UNIQUE (sequence_id, step_order, variant_of)
);

CREATE TABLE IF NOT EXISTS enrollments (
  id            TEXT PRIMARY KEY,
  sequence_id   TEXT NOT NULL REFERENCES sequences(id),
  business_id   TEXT NOT NULL REFERENCES businesses(id),
  contact_id    TEXT REFERENCES contacts(id),
  status        TEXT NOT NULL DEFAULT 'active', -- active | replied | finished | stopped | bounced | unsubscribed
  current_step  INTEGER NOT NULL DEFAULT 0,
  next_send_at  TEXT,
  stopped_reason TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (sequence_id, business_id)
);

-- ============ messages ============
CREATE TABLE IF NOT EXISTS messages (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  enrollment_id TEXT REFERENCES enrollments(id),
  contact_id    TEXT REFERENCES contacts(id),
  business_id   TEXT REFERENCES businesses(id),
  direction     TEXT NOT NULL,         -- outbound | inbound
  step_order    INTEGER,
  from_email    TEXT,
  to_email      TEXT,
  subject       TEXT,
  body          TEXT,
  provider_id   TEXT,                  -- Mailgun message id
  provider_status TEXT,                -- accepted | delivered | failed | complained
  failure_reason TEXT,
  in_reply_to   TEXT,                  -- Message-Id we are replying to
  thread_key    TEXT,                  -- normalized subject/refs for threading
  intent        TEXT,                  -- interested | not_interested | wrong_person | ooo | unsubscribe | question | unknown
  snippet_text  TEXT,                  -- the AI snippet actually used
  sent_at       TEXT,
  received_at   TEXT,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_thread ON messages (thread_key);
CREATE INDEX IF NOT EXISTS idx_msg_contact ON messages (contact_id, created_at);

CREATE TABLE IF NOT EXISTS events (
  id            TEXT PRIMARY KEY,
  message_id    TEXT,
  type          TEXT NOT NULL,         -- delivered | failed | complained | opened | clicked | unsubscribed
  payload       TEXT,
  created_at    TEXT NOT NULL
);

-- ============ compliance ============
CREATE TABLE IF NOT EXISTS suppressions (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  email         TEXT NOT NULL,
  domain        TEXT NOT NULL,
  reason        TEXT NOT NULL,         -- unsubscribed | complained | hard_bounce | manual | legal
  scope         TEXT NOT NULL DEFAULT 'global', -- global | workspace | domain
  note          TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (workspace_id, email)
);
CREATE INDEX IF NOT EXISTS idx_suppress_domain ON suppressions (domain);

-- ============ AI profile (the "training bar" analogue) ============
CREATE TABLE IF NOT EXISTS ai_profiles (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  name          TEXT NOT NULL,
  offer         TEXT,
  icp           TEXT,                  -- ideal customer profile
  tone          TEXT DEFAULT 'direct, peer-to-peer, no hype',
  proof_points  TEXT,                  -- JSON array
  banned_phrases TEXT,                 -- JSON array
  corpus_chars  INTEGER DEFAULT 0,     -- size of training material ingested
  readiness     INTEGER DEFAULT 0,     -- 0-100, drives the UI progress bar
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_profile_docs (
  id            TEXT PRIMARY KEY,
  profile_id    TEXT NOT NULL REFERENCES ai_profiles(id),
  kind          TEXT NOT NULL,         -- url | file | paste
  label         TEXT,
  content       TEXT NOT NULL,
  char_count    INTEGER NOT NULL,
  created_at    TEXT NOT NULL
);

-- ============ scoring ============
CREATE TABLE IF NOT EXISTS scores (
  business_id   TEXT PRIMARY KEY REFERENCES businesses(id),
  fit_score     INTEGER NOT NULL,      -- 0-100
  demand_score  INTEGER,               -- local search demand for its category
  reach_score   INTEGER,               -- email reachability
  factors       TEXT,                  -- JSON breakdown so we can explain the number
  computed_at   TEXT NOT NULL
);

-- Monthly keyword-volume import (Google Ads Keyword Planner CSV). Until this is
-- populated, scoring.demand_for() falls back to review density as a proxy.
CREATE TABLE IF NOT EXISTS category_demand (
  category      TEXT NOT NULL,
  state         TEXT NOT NULL,
  city          TEXT,
  monthly_volume INTEGER NOT NULL,
  competition   TEXT,                  -- low | medium | high
  seasonality   TEXT,                  -- JSON, 12 monthly index values
  source        TEXT DEFAULT 'keyword_planner',
  fetched_at    TEXT NOT NULL,
  PRIMARY KEY (category, state, city)
);

-- Hermes research queue. The operator types a domain in the dashboard and the
-- row lands here; the Hermes agent worker (scripts/hermes_worker.py) claims it,
-- researches the site with its own tools, and posts the lead + draft back.
-- This is how the UI uses the agent without exposing an agent endpoint to the
-- internet: the worker pulls, it never gets called.
CREATE TABLE IF NOT EXISTS research_queue (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  domain        TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending | claimed | done | error
  attempts      INTEGER NOT NULL DEFAULT 0,
  business_id   TEXT REFERENCES businesses(id),
  profile       TEXT,                  -- JSON research payload from the worker
  draft         TEXT,                  -- JSON {subject, body, grounded_on}
  error         TEXT,
  requested_by  TEXT,
  requested_at  TEXT NOT NULL,
  claimed_at    TEXT,
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_rq_pending ON research_queue (status, requested_at);
CREATE INDEX IF NOT EXISTS idx_rq_ws ON research_queue (workspace_id, requested_at);

-- Leads the agent discovered from a researched business. The researched domain is
-- only an example of the type of business we sell to; every row here is another
-- company of that same trade in the same market, plus the signal that makes them
-- worth contacting now. A lead is only worth keeping if the signal was read
-- somewhere real, so signal_source is the URL the agent actually opened.
CREATE TABLE IF NOT EXISTS research_leads (
  id            TEXT PRIMARY KEY,
  queue_id      TEXT REFERENCES research_queue(id),
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  client_business_id TEXT REFERENCES businesses(id),  -- the example business whose type defines the list
  business_id   TEXT REFERENCES businesses(id),       -- the prospect, once created
  name          TEXT NOT NULL,
  domain        TEXT,
  why_now       TEXT,                  -- one sentence: why contact them this week
  signal_kind   TEXT,                  -- hiring | permit | expansion | review | rfp | license | news | social | other
  signal_detail TEXT,
  signal_source TEXT,                  -- URL the signal was read on
  contact_hint  TEXT,                  -- named person/role found, never a guessed address
  fit           INTEGER,               -- 0-100 match to the client's best customer
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rlead_client ON research_leads (client_business_id, created_at);
CREATE INDEX IF NOT EXISTS idx_rlead_ws ON research_leads (workspace_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rlead_dedupe
  ON research_leads (client_business_id, domain) WHERE domain IS NOT NULL AND domain != '';

-- Search-task queue: steps whose channel is 'call'/'linkedin' land here instead
-- of being sent by email. This is what turns unreachable leads into work.
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  workspace_id  TEXT NOT NULL REFERENCES workspaces(id),
  enrollment_id TEXT REFERENCES enrollments(id),
  business_id   TEXT REFERENCES businesses(id),
  contact_id    TEXT REFERENCES contacts(id),
  kind          TEXT NOT NULL,         -- call | linkedin | research
  due_at        TEXT,
  notes         TEXT,
  status        TEXT NOT NULL DEFAULT 'open',  -- open | done | skipped
  completed_at  TEXT,
  created_at    TEXT NOT NULL
);
