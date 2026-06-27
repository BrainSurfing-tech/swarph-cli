-- Swarph mesh-gateway schema. Idempotent.
-- Safe to re-run via `sqlite3 <db-path> < schema.sql`.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─── PEERS — capability-aware node registry ────────────────────────
-- Phase 5.5 (PLAN.md §15): ratification columns gate task_claim. New
-- peers default ratified=0; witness flips to 1 via PATCH /peers/{name}.
-- The grandfather backfill in server.py:_init_db ratifies pre-existing
-- peers at PR-A migration time; fresh installs land here directly.
CREATE TABLE IF NOT EXISTS claude_peers (
  name TEXT PRIMARY KEY,                 -- 'node-a', 'node-b'
  url TEXT NOT NULL,                     -- 'http://node-b:8787'
  capabilities TEXT NOT NULL,            -- JSON: full /health.capabilities snapshot
  registered_at TIMESTAMP NOT NULL,
  last_health TIMESTAMP,
  last_seen TIMESTAMP,
  enabled INTEGER NOT NULL DEFAULT 1,
  ratified INTEGER NOT NULL DEFAULT 0,   -- Phase 5.5: server-side §15 contract gate
  ratified_at TIMESTAMP,
  ratified_by TEXT,                      -- canonical witness peer name
  ratification_reason TEXT
);

-- ─── THREADS — UUID ↔ readable thread name mapping ─────────────────
-- claude --session-id requires UUID; humans/audit want readable names like
-- 'lab↔gpu-wsl:phase-7a-shadow'. This table mints + persists the mapping.
CREATE TABLE IF NOT EXISTS claude_threads (
  thread_uuid TEXT PRIMARY KEY,          -- UUID passed to /chat
  thread_name TEXT NOT NULL UNIQUE,      -- 'lab↔gpu-wsl:phase-7a-shadow'
  peer_pair TEXT NOT NULL,               -- 'lab↔gpu-wsl'
  topic TEXT NOT NULL,                   -- 'phase-7a-shadow'
  session_id TEXT,                       -- Peer-local session ID (e.g. 'e906aaa3')
  created_at TIMESTAMP NOT NULL,
  last_used_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_threads_pair ON claude_threads(peer_pair, last_used_at DESC);
CREATE INDEX IF NOT EXISTS idx_threads_session ON claude_threads(session_id);

-- ─── MESSAGES — chat tier audit log ────────────────────────────────
CREATE TABLE IF NOT EXISTS claude_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT,                        -- FK to claude_threads.thread_uuid (nullable for ad-hoc DMs)
  from_node TEXT NOT NULL,
  to_node TEXT NOT NULL,
  kind TEXT NOT NULL,                    -- 'status' | 'question' | 'answer' | 'unblock' | 'fyi'
  content TEXT NOT NULL,
  related_task_id INTEGER,               -- FK to claude_tasks (nullable)
  created_at TIMESTAMP NOT NULL,
  read_at TIMESTAMP,
  channel TEXT,                          -- mesh-channels §2: NULL = DM (today); set = a channel post
  mentions TEXT,                         -- JSON [peer,...] SERVER-DERIVED at post-time (client array discarded, B2)
  priority TEXT DEFAULT 'normal',        -- 'normal'|'high'; 'high' gated like @all (B3)
  FOREIGN KEY (thread_id) REFERENCES claude_threads(thread_uuid)
  -- NB (step 1 / drop seat-A): to_node stays NOT NULL here. §2's logical model
  -- has channel posts at to_node NULL; making it nullable needs a table-recreate
  -- (SQLite can't drop NOT NULL in place) — DEFERRED to the channel-post step
  -- (decide there: recreate-for-NULL vs a channel-post to_node convention).
);
CREATE INDEX IF NOT EXISTS idx_messages_to_unread ON claude_messages(to_node, read_at, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON claude_messages(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON claude_messages(channel, created_at);

-- mesh-channels §2 (build step 1) — additive; DMs untouched. Authority columns
-- (visibility, allow_broadcast) ship here but are ENFORCED in later steps that
-- open their surface (B1 membership-write, B5 broadcast/create-auth).
CREATE TABLE IF NOT EXISTS channels (
  name        TEXT PRIMARY KEY,            -- canonical names pre-seeded + reserved (B5, seeded at step 7)
  kind        TEXT NOT NULL,               -- 'announce' | 'topic' | 'group'
  visibility  TEXT NOT NULL DEFAULT 'open',-- 'open' (self-join) | 'invite' (owner-add only) (H3)
  description TEXT,
  created_by  TEXT NOT NULL,
  created_at  TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_members (
  channel         TEXT NOT NULL,
  peer            TEXT NOT NULL,
  wake_policy     TEXT NOT NULL DEFAULT 'mentions_only',  -- mentions_only|here_and_mentions|all|muted (per member per channel)
  allow_broadcast INTEGER NOT NULL DEFAULT 0,             -- may @all/priority=high? DECOUPLED from created_by (B5)
  joined_at       TIMESTAMP NOT NULL,
  PRIMARY KEY (channel, peer)
);
CREATE INDEX IF NOT EXISTS idx_channel_members_peer ON channel_members(peer);

-- swarph automation control plane (scheduled_events) — additive.
CREATE TABLE IF NOT EXISTS scheduled_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL UNIQUE,
  trigger_type  TEXT NOT NULL,                          -- 'time' | 'event'
  cron          TEXT,                                   -- time: 5-field cron (UTC)
  predicate     TEXT,                                   -- event: JSON {kind, args}
  target_cell   TEXT NOT NULL,
  task          TEXT NOT NULL,
  context_ref   TEXT NOT NULL,                          -- JSON durable anchor(s) §4
  out_channel   TEXT,
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_by    TEXT NOT NULL,
  created_at    TIMESTAMP NOT NULL,
  last_fired_at TIMESTAMP,
  last_status   TEXT,
  last_task_pk  INTEGER,
  fire_count    INTEGER NOT NULL DEFAULT 0,
  min_interval_sec       INTEGER,                      -- seat-A: minimum seconds between fires (optional)
  last_consumed_post_id  INTEGER                       -- seat-A: watermark for event-trigger dedup
);
CREATE INDEX IF NOT EXISTS idx_sched_enabled
  ON scheduled_events(enabled, trigger_type);

-- ─── TASKS — work queue ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS claude_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL UNIQUE,          -- 'pypi-survey-tail-deps'
  category TEXT,                         -- 'research' | 'audit' | 'survey' | 'doc' | 'debug'
  status TEXT NOT NULL,                  -- 'pending' | 'in_progress' | 'done' | 'failed' | 'stale' | 'cancelled'
  priority INTEGER NOT NULL DEFAULT 5,
  cost_cap_usd REAL,
  not_before TIMESTAMP,                  -- gate
  added_by TEXT NOT NULL,
  added_at TIMESTAMP NOT NULL,
  claimed_by TEXT,
  claimed_at TIMESTAMP,
  completed_at TIMESTAMP,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  spec_json TEXT NOT NULL,               -- prompt, target_node, scope, constraints
  output_path TEXT,                      -- 'research/lab_service/findings/...'
  cost_actual_usd REAL,
  parent_task_id INTEGER,                -- loop detection
  thread_id TEXT,                        -- session_id UUID for the claude-service call
  FOREIGN KEY (parent_task_id) REFERENCES claude_tasks(id),
  FOREIGN KEY (thread_id) REFERENCES claude_threads(thread_uuid)
);
CREATE INDEX IF NOT EXISTS idx_tasks_pending ON claude_tasks(priority DESC, added_at)
  WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_tasks_claimed ON claude_tasks(claimed_by, status);

-- ─── PEER RATIFICATIONS — append-only audit log (Phase 5.5) ────────
-- Per PLAN.md §15.4a step 5: every ratification flip is recorded forever
-- (peer, witness, when, reason, optional handshake DM pointer). Repudiation
-- impossible — a flip is on the record with the witness named.
--
-- Backfill convention: peers grandfathered at PR-A migration carry
-- ratified_by='grandfather_v0_phase_5_5_migration' so the cohort
-- distinction is queryable forever.
CREATE TABLE IF NOT EXISTS peer_ratifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  peer TEXT NOT NULL,
  ratified_by TEXT NOT NULL,
  ratified_at TIMESTAMP NOT NULL,
  reason TEXT,
  witness_dm_id INTEGER,
  -- R1 C3-A (v9): trust-epoch stamp. Which auth regime witnessed this
  -- ratification. DERIVED FROM THE AUTH PATH at write time (C3-B), never
  -- from a request body field. Legacy + grandfather rows are 'shared_token'.
  -- CHECK present on fresh installs only; existing DBs get the column via
  -- ALTER (no CHECK — SQLite can't add one in place) + the backfill UPDATE.
  binding_regime TEXT NOT NULL DEFAULT 'shared_token'
    CHECK (binding_regime IN ('shared_token', 'per_peer_token', 'signed')),
  FOREIGN KEY (peer) REFERENCES claude_peers(name),
  FOREIGN KEY (witness_dm_id) REFERENCES claude_messages(id)
);
CREATE INDEX IF NOT EXISTS idx_peer_ratifications_peer ON peer_ratifications(peer, ratified_at);

-- ─── PER-PEER TOKENS — R1 auth hardening step C1 (mesh task r1-per-peer-identity) ──
-- Replaces the two shared bearer tokens (MESH_GATEWAY_TOKEN + commander token)
-- with per-peer identity tokens, closing "peer A presents the shared token and
-- forges peer B". C1 is the foundation: mint + verify, DORMANT — _authorize
-- resolves a per-peer token to its peer identity but live behavior is unchanged
-- (shared tokens still authenticate; nothing enforces caller==peer yet — that's
-- C2/C4, gated behind warn-only flags).
--
-- token_sha256: SHA-256 hex of the bearer presented on the wire. The RAW token
-- is NEVER persisted (same discipline as public_chat_log hashes); it is returned
-- to the peer exactly ONCE at mint time.
-- key_generation: monotonic per-peer counter. C1 always mints generation 1 on a
-- fresh peer; C4 (revocation) bumps it so a revoked token can never be resurrected
-- by re-registering. UNIQUE(peer, key_generation) makes the (peer,gen) pair the
-- stable identity; UNIQUE(token_sha256) makes the hash a global lookup key.
-- minted_via: audit — 'register' (POST /peers/register) is the only minter in C1.
-- NO FK on peer (deliberate, mirrors C4's peer_token_revocations): peer_deregister
-- does a bare DELETE FROM claude_peers, and an FK here would either block the
-- delete or require ON DELETE CASCADE ordering the deregister path doesn't do.
-- The peer string is validated at mint time (register upserts the peer in the
-- same txn); a dangling token row after deregister is harmless (it can never
-- authenticate a live identity and C4's purge cleans it). Keeping the FK out
-- preserves deregister's single-statement semantics.
CREATE TABLE IF NOT EXISTS peer_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  peer TEXT NOT NULL,
  token_sha256 TEXT NOT NULL UNIQUE,     -- SHA-256 hex; raw token never stored
  key_generation INTEGER NOT NULL DEFAULT 1,
  minted_at TIMESTAMP NOT NULL,
  minted_via TEXT NOT NULL DEFAULT 'register',
  UNIQUE(peer, key_generation)
);
CREATE INDEX IF NOT EXISTS idx_peer_tokens_sha ON peer_tokens(token_sha256);

-- ─── PEER TOKEN REVOCATIONS — per-(peer,generation) kill log (R1 C4) ──
-- Append-only audit of revoke/unrevoke actions against a per-peer token
-- generation. _authorize folds the latest row per (peer, key_generation):
-- the most recent action wins (revoke → dead, unrevoke → live), so a token
-- can be killed and (witnessed) un-killed without ever DELETEing a row.
--
-- NO FK on peer (deliberate, MIRRORS peer_tokens above): a revoke row MUST
-- outlive the peer_deregister DELETE FROM claude_peers / DELETE FROM
-- peer_tokens (deregister inserts the revoke row in the SAME txn, just before
-- the purge — fail-closed). An FK would either block the delete or demand an
-- ON DELETE CASCADE ordering deregister doesn't do, and would destroy exactly
-- the audit row whose whole job is to survive the peer's disappearance.
-- action is CHECK-constrained to the two verbs; latest-wins makes the fold
-- order-independent of physical row order (ORDER BY id DESC).
-- witness_dm_id: optional pointer into claude_messages (the witnessing
-- handshake DM) for an un-revoke; NULL for a self/deregister revoke.
CREATE TABLE IF NOT EXISTS peer_token_revocations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  peer TEXT NOT NULL,
  key_generation INTEGER NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('revoke','unrevoke')),
  actor TEXT NOT NULL,                 -- who performed it (auth.peer or 'self' for deregister)
  reason TEXT,
  witness_dm_id INTEGER,               -- optional FK pointer to claude_messages; NULL for self/deregister
  created_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ptr_peer_gen ON peer_token_revocations(peer, key_generation, id);

-- ─── PEER OBSERVATIONS — cross-vertex observer primitive (RFC v1) ──
-- Each row is one observer's view of one
-- observed peer at a point in time. Full replacement on
-- (observer, observed) conflict — observers maintain the rolling view,
-- gateway just stores + joins.
--
-- The "observed-by" join (GET /peers/{name}/observed-by) is the
-- federation-relevant cross-vertex consensus surface: if N observers
-- agree on peer X's state, the federation-time consumer (§1.20
-- handshake) has high-confidence input for the AI² merge-decision.
--
-- Retention: caller-driven via expires_at; gateway never auto-purges.
-- Stale entries (expires_at past + grace) are filterable at query time.
CREATE TABLE IF NOT EXISTS peer_observations (
  observer TEXT NOT NULL,                -- peer-name doing the observing
  observed TEXT NOT NULL,                -- peer-name being observed
  last_seen_at TIMESTAMP NOT NULL,       -- timestamp of last observed signal
  last_seen_kind TEXT NOT NULL,          -- 'dm' | 'registry-presence' | 'task-claim' | 'inferred-from-silence'
  inferred_peer_health TEXT NOT NULL,    -- 4-state per §6.4a — 'Stop' | 'StopFailure' | 'Compacting' | 'Quota-exhausted'
  inferred_observer_health TEXT NOT NULL,-- 2-state — 'Active' | 'Watchdog-mute'
  confidence REAL NOT NULL,              -- 0.0-1.0 confidence in the inference
  observation_basis TEXT,                -- free-text: evidence backing the inference
  expires_at TIMESTAMP,                  -- auto-stale boundary (caller-set)
  updated_at TIMESTAMP NOT NULL,         -- when this row was written/replaced
  PRIMARY KEY (observer, observed)
);
CREATE INDEX IF NOT EXISTS idx_peer_obs_observed ON peer_observations(observed, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_peer_obs_observer ON peer_observations(observer, updated_at DESC);

-- ─── COUNCIL JOBS — subscription-path worker pool queue (RFC v1) ───
-- Queue-based dispatch of Council Claude-side debates (R1 Defender + R2
-- Defender Judge) to a worker pool. A producer POSTs /council-batch →
-- workers claim → workers spawn `claude -p` subprocess → write result back.
--
-- LEAN Council: only r1_defender +
-- r2_defender_judge fire in production (R3 defined but never invoked).
-- CHECK enforces what actually fires. Each debate emits exactly 2 jobs.
-- Post-scoring-cap-reduction (20→15): 15 × 2 = 30 jobs/cycle peak.
--
-- Idempotency: UNIQUE(debate_id, role) — same (debate_id, role) cannot
-- be inserted twice. Re-POSTing /council-batch with same payload returns
-- the existing job's id rather than duplicating.
CREATE TABLE IF NOT EXISTS council_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debate_id TEXT NOT NULL,           -- ULID/UUID minted by the producer per Council debate
  -- v6 widened CHECK: Claude side (r1_defender + r2_defender_judge) AND
  -- Gemini side (r1_challenger + r2_challenger_judge) per Phase 3 ship.
  -- Gemini side routes through `gemini -p` (Google subscription-tier CLI,
  -- /usr/local/bin/gemini v0.41.2, authenticated via commander Google account).
  -- council_worker.py dispatches on role prefix to choose claude vs gemini binary.
  --
  -- v8 widened CHECK further (2026-05-18): r0_public_chat for Phase 2
  -- chat-sync endpoint. Same Claude-side dispatch as r1_defender. See
  -- /api/chat-sync in server.py for the endpoint that inserts these rows.
  -- subscription_chat + narrative_risk (Grok $0 lane) widened via the
  -- behavioral-probe migrations in
  -- _init_db (SQLite can't ALTER a CHECK in place).
  role TEXT NOT NULL CHECK (role IN (
    'r1_defender', 'r2_defender_judge',
    'r1_challenger', 'r2_challenger_judge',
    'r0_public_chat',
    'subscription_chat',
    'narrative_risk',
    'r1_grok_defender'
  )),
  prompt TEXT NOT NULL,              -- full prompt text (producer-rendered)
  model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
  status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'claimed' | 'done' | 'failed'
  claimed_by TEXT,                   -- worker-id (e.g. 'council-worker-3')
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  created_at TIMESTAMP NOT NULL,
  claimed_at TIMESTAMP,
  completed_at TIMESTAMP,
  result_json TEXT,                  -- worker writes structured response here
  error TEXT,                        -- worker writes error context on failure
  UNIQUE(debate_id, role)
);
CREATE INDEX IF NOT EXISTS idx_council_jobs_pending ON council_jobs(status, created_at)
  WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_council_jobs_debate ON council_jobs(debate_id);
CREATE INDEX IF NOT EXISTS idx_council_jobs_claimed ON council_jobs(claimed_by, status);

-- ─── PEER HEALTH EVENTS — substrate-doc §6.4a R11 candidate (RFC PR #159) ───
-- RFC §1 deliberately 3-column
-- minimal primitive — no CHECK on event_type so enumeration extends freely.
--
-- Current event types (5-state §6.4a + 3 6th-state Path A discoveries):
--   5-state: Active / Stop / StopFailure / Compacting / Quota-exhausted
--   6th-state: rate_limit_429 / classifier_unavailable_bash_denied /
--              usage_limit_reset
-- Future additions slot in without schema change.
--
-- Cross-vertex aggregation: peer field carries the observed-peer name
-- (e.g. 'node-a', 'node-b'). Multiple vertices POST
-- events for the same peer; downstream aggregation via SELECT.
CREATE TABLE IF NOT EXISTS peer_health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  time TIMESTAMP NOT NULL,           -- ISO-8601 timestamp of the event
  peer TEXT NOT NULL,                -- observed peer (bare name v1, organism-qualified at federation)
  event_type TEXT NOT NULL,          -- enumeration above (no CHECK — extensible)
  source TEXT,                       -- writer attribution: 'session_jsonl_logger'/'cron'/'observer'/etc.
  source_session_id TEXT,            -- optional: claude session id when source is jsonl-derived
  source_jsonl_path TEXT,            -- optional: path to JSONL that captured the event (audit trail)
  metadata TEXT,                     -- optional JSON: free-form contextual fields
  created_at TIMESTAMP NOT NULL      -- when gateway received the POST (vs event time)
);
CREATE INDEX IF NOT EXISTS idx_phe_peer_time ON peer_health_events(peer, time DESC);
CREATE INDEX IF NOT EXISTS idx_phe_time ON peer_health_events(time DESC);
CREATE INDEX IF NOT EXISTS idx_phe_event_type ON peer_health_events(event_type, time DESC);

-- ─── SCHEMA VERSION ────────────────────────────────────────────────
-- Version-row history (drop PR #11 review carry-forward (b) DM #728):
--   v1 — initial schema (claude_peers + claude_threads + claude_messages
--        + claude_tasks). Recorded by INSERT OR IGNORE at first init.
--   v2 — IN-PLACE column migration only (claude_threads.session_id added
--        via _init_db ALTER TABLE before this executescript runs). No
--        version row was inserted by design — v2 is a code-only delta,
--        not a schema-change worth a version-row breadcrumb. Future
--        migrations may reuse the number 2 if they ship a corresponding
--        version row, OR skip past it; both are fine.
--   v3 — Phase 5.5 ratification (claude_peers ratified columns +
--        peer_ratifications audit table). Recorded by INSERT OR IGNORE.
--   v4 — Cross-vertex observer Phase 1 (peer_observations table).
--   v5 — Council subscription-path worker pool Phase 1 (council_jobs
--        table + indexes + CHECK on LEAN R1+R2 role enum).
--   v6 — Council Phase 3 Gemini-via-subscription (CHECK on council_jobs.role
--        widened from 2 to 4 roles: adds r1_challenger + r2_challenger_judge
--        for Gemini side via `gemini -p`). In-place CHECK migration via
--        _init_db RENAME+CREATE+INSERT+DROP pattern.
--   v7 — Phase 3 Path B (peer_health_events table). Cross-vertex 6th-state
--        observability replaces (graduates from) an earlier flat-file log.
--        3-column primitive per RFC §1 + audit-trail columns (source,
--        source_session_id, source_jsonl_path, metadata).
--   v8 — Phase 2 chat-sync endpoint (public_chat_log table). Audit trail
--        for POST /api/chat-sync. GDPR-clean (ip_hash + prompt_hash only,
--        never raw values). Used for abuse investigation + concern-B
--        threshold tracking (lab memory project_deferred_decisions.md
--        entry #3). Schema 2026-05-18.
--   v9 — R1 auth hardening step C3-A (mesh task r1-per-peer-identity).
--        peer_ratifications.binding_regime trust-epoch stamp (which auth
--        regime witnessed each ratification). Added via ALTER-before-
--        executescript in _init_db for existing DBs (no CHECK on legacy
--        rows — SQLite limitation), in CREATE TABLE for fresh installs
--        (with CHECK). All legacy + grandfather rows backfilled to
--        'shared_token'. ZERO behavior change. v2 was skipped historically —
--        C1 owns v10 (peer_tokens), C4 owns v11 (revocations). 2026-05-31.
--   v11 — R1 auth hardening step C4 (per-peer token revocation). New
--        peer_token_revocations table — append-only revoke/unrevoke log
--        folded latest-wins per (peer, key_generation) by _authorize. NEW
--        table (CREATE IF NOT EXISTS) so the schema.sql executescript creates
--        it on fresh AND existing DBs — no ALTER/RENAME migration needed
--        (unlike the council_jobs CHECK-widens). Enforce is env-gated
--        (MESH_REVOCATION_ENFORCE, default warn-only) → ZERO behavior change
--        on merge; a revoked token only 401s once the flag flips. 2026-05-31.
--   (version-independent) — subscription_chat role (generic spaced-usage
--        subscription worker lane: Skunkworks + future workers route through
--        the worker pool instead of metered SDK). council_jobs.role CHECK
--        widened to add 'subscription_chat'. Job params (system_prompt/
--        response_schema/purpose) are JSON-packed into the existing `prompt`
--        column — no new columns. Worker routes by model prefix to agy
--        (gemini/Flash) or claude -p. Applied via probe-guarded in-place
--        migration (server.py _init_db), NOT a schema_version bump — decoupled
--        from the parallel R1 auth-ladder's v9/v10/v11 reservation. 2026-05-31.

-- Phase 2 chat-sync audit log. One row per /api/chat-sync call regardless
-- of outcome. GDPR discipline: raw IP + raw prompt NEVER persisted —
-- SHA256 hashes only. prompt_length captured for size-distribution
-- analysis without leaking content. response_length similarly captured.
CREATE TABLE IF NOT EXISTS public_chat_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  time TEXT NOT NULL,
  ip_hash TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  prompt_length INTEGER NOT NULL,
  response_status TEXT NOT NULL CHECK (response_status IN
    ('done', 'failed', 'timeout', 'rate_limited', 'kill_switch', 'cap_exceeded')),
  latency_ms INTEGER,
  response_length INTEGER,
  job_id INTEGER,
  source TEXT,
  error TEXT,
  FOREIGN KEY (job_id) REFERENCES council_jobs(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_public_chat_log_time ON public_chat_log(time);
CREATE INDEX IF NOT EXISTS idx_public_chat_log_ip ON public_chat_log(ip_hash, time);
CREATE INDEX IF NOT EXISTS idx_public_chat_log_status ON public_chat_log(response_status, time);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TIMESTAMP NOT NULL
);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (1, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (3, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (4, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (5, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (6, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (7, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (8, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (9, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (10, CURRENT_TIMESTAMP);
INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (11, CURRENT_TIMESTAMP);
