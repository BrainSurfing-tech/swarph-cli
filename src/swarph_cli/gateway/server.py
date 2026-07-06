"""mesh-gateway — coordination layer for a swarph mesh.

Single-node service. Provides HTTP endpoints for the LLM "cell" services on
each mesh node + operator/CLI access. Persists to a local SQLite DB.

Endpoints:
    GET  /health                        — public liveness + schema version
    GET  /peers                         — registry of known nodes + capabilities
    POST /peers/register                — peer self-registration with capabilities
    GET  /peers/{name}/health           — per-peer last health + capabilities
    POST /threads                       — mint UUID for a readable thread_name
    GET  /threads/{thread_uuid}         — thread metadata + message history
    POST /messages                      — post a peer DM (audit-logged)
    GET  /messages                      — query inbox (?to=&since=&kind=)
    POST /tasks                         — add a task to the queue
    GET  /tasks                         — list with filters (?status=&claimed_by=)
    POST /tasks/claim                   — atomic-claim next pending (race-safe)
    POST /tasks/{id}/complete           — mark done with output_path + cost
    POST /tasks/{id}/fail               — mark failed with error
    POST /tasks/{id}/unblock            — clear not_before (data-gate cleared)

Auth: shared bearer token from MESH_GATEWAY_TOKEN env. Per-peer identity comes
from request body fields (`from_node`, `claimed_by`). Hashed-token-per-peer is
deferred to v2 — 4 nodes don't need it yet.

NOT in v1:
    - Findings file serving (`/findings/{task_id}`) — caller reads git directly
    - Tick scheduler — that's MVP Day 4; this service only hosts state
    - GitHub poll integration — MVP Day 5
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

import jwt  # PyJWT — B1 Meta-Edge RS256 identity-token verification (gateway extra)
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .feature_registry import aggregate_features, apply_allowlist, apply_caps

# =====================================================================
# CONFIG
# =====================================================================

AUTH_TOKEN = os.environ.get("MESH_GATEWAY_TOKEN", "")
# Optional separate token for a privileged operator / PWA peer.
# Issued so the human-side client doesn't share the AI-peer credential.
# When unset, only AUTH_TOKEN is accepted (backward-compatible).
COMMANDER_TOKEN = os.environ.get("MESH_GATEWAY_COMMANDER_TOKEN", "")
DB_PATH = os.environ.get("MESH_DB_PATH", os.path.expanduser("~/.swarph/mesh.db"))
SCHEMA_PATH = os.environ.get(
    "MESH_SCHEMA_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql"),
)
PORT = int(os.environ.get("PORT", "8788"))

# Gateway-held gbrain proxy token (POST /brain/query). Cells authenticate to
# THIS gateway with their mesh token; the gateway then presents ITS OWN
# gbrain token upstream so cells never need a separate gbrain_ credential.
GATEWAY_GBRAIN_URL = os.environ.get("GATEWAY_GBRAIN_URL", "http://100.107.222.72:8792/mcp")
GATEWAY_GBRAIN_TOKEN = os.environ.get("GATEWAY_GBRAIN_TOKEN", "")

# B1 Meta-Edge identity (META_EDGE_IDENTITY_CONTRACT.md). Meta-Edge SSO ISSUES
# RS256 JWTs; the gateway only ever TRUSTS them with Meta-Edge's PUBLIC key (it
# can never forge one — the identity↔relay asymmetry). All three are read at
# CALL time (via os.environ, see _meta_edge_public_key) NOT cached at import, so
# the key can be rotated without a restart and monkeypatched directly in tests.
META_EDGE_ISSUER_DEFAULT = "meta-edge"
META_EDGE_AUDIENCE_DEFAULT = "swarph-gateway"

VALID_KINDS = {"status", "question", "answer", "unblock", "fyi"}
VALID_COUNCIL_ROLES = {
    "r1_defender",          # Claude side (claude -p)
    "r2_defender_judge",    # Claude side
    "r1_challenger",        # Gemini side (gemini -p) — Phase 3 added 2026-05-15
    "r2_challenger_judge",  # Gemini side
    "r0_public_chat",       # Claude side — Phase 2 chat-sync endpoint added 2026-05-18
    "subscription_chat",    # generic spaced-usage $0 subscription lane — Item 1 2026-05-31
    "narrative_risk",       # Grok ($0) structured narrative-risk lane
    "r1_grok_defender",     # Grok ($0) GENERATIVE bull-case defender — throttle-resilient council 2026-06-24
}
# subscription_chat per-day cap (substrate sprint Item 1 guard b). v1 is a single
# GLOBAL cap: the consumption query (council_batch_post) counts ALL
# subscription_chat rows created today across every caller — there is NO
# caller-key in it. (The prior comment claimed "keyed by the purpose prefix's
# caller"; that was aspirational and did NOT match the code — corrected per lab
# seat-A #1683 / drop seat-B #1668 minor-2 so the next reader isn't sent hunting
# a caller-key that doesn't exist.)
#
# R1 C2 SOURCE-9 FORWARD-GUARD: because the cap is global today, there is NO
# forgeable caller field here, so C2 adds NO binding at the cap site (it would
# have nothing to bind). BUT if this is ever lifted to a per-CALLER quota table
# (the "lift if multi-tenant pressure" note), the caller-key MUST be the
# authenticated `auth.peer` from _authorize — NEVER a body/purpose field — or the
# cost-cap becomes spoof-evadable. Wire it through _check_caller_binding at that
# time. The public /api/chat-sync lane (r0_public_chat) is intentionally no-auth
# with its own _PUBLIC_CHAT_DAILY_CAP and is OUT of caller-binding scope by
# design (anonymous endpoint, rate-limited separately).
SUBSCRIPTION_CHAT_DEFAULT_MAX_PER_DAY = int(os.environ.get("SUBSCRIPTION_CHAT_MAX_PER_DAY", "200"))
# Council job status values are hardcoded in the transition SQL (pending/
# claimed/done/failed). Per drop-mother seat-A point C #1261 iter-1
# YAGNI judgment: dropped placeholder 'cancelled' state — no endpoint
# transitioned to it. Re-add when mid-flight invalidation lands.
VALID_TASK_STATUS = {"pending", "in_progress", "done", "failed", "stale", "cancelled"}
VALID_CATEGORIES = {"research", "audit", "survey", "doc", "debug", "commander_approved"}

# Cross-vertex observer primitive (RFC v1 §2). 4-state PeerHealth + 2-state
# ObserverHealth taxonomies from swarph_substrate.md §6.4a R9. Validated at
# POST /peers/{name}/observations so observer-side typos surface as 422
# rather than landing as opaque strings nobody can join on.
VALID_PEER_HEALTH = {
    "Active",          # baseline-healthy (mother #1153 production-context catch)
    "Stop",            # clean session-end
    "StopFailure",     # API throttle / transient error
    "Compacting",      # context-window-resetting summary in-flight
    "Quota-exhausted", # weekly cap / subscription limit
}
VALID_OBSERVER_HEALTH = {"Active", "Watchdog-mute"}
VALID_LAST_SEEN_KINDS = {
    "dm",
    "registry-presence",
    "task-claim",
    "inferred-from-silence",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mesh-gateway")

app = FastAPI(title="Swarph Mesh Gateway", version="0.1.2")

# CORS — a browser client (e.g. a PWA) may be served from a separate origin
# (bound to a private/tailnet IP). Bearer auth on every endpoint
# means open CORS does NOT grant unauthenticated access; it only allows the
# browser to deliver a preflight + the Authorization header on cross-origin
# fetches. The cookie surface is empty, so credentialed-cookie attacks are N/A.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# =====================================================================
# DATABASE
# =====================================================================

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn():
    """Per-request connection. SQLite WAL handles concurrent readers fine.

    We don't pool — request volume on a 4-node mesh is single-digit per second
    at peak. Pooling adds bug surface (stale handles after schema migrations).
    """
    c = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit; we control transactions
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
    finally:
        c.close()


def _init_db() -> None:
    """Apply schema.sql at startup. Idempotent (CREATE IF NOT EXISTS)."""
    if not os.path.exists(SCHEMA_PATH):
        log.error("schema.sql not found at %s", SCHEMA_PATH)
        return
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        # v2 migration runs BEFORE schema.sql executescript to satisfy
        # the CREATE INDEX in schema.sql which references session_id.
        # On fresh installs, claude_threads doesn't exist yet — caught by
        # the OperationalError and skipped (schema.sql will create the
        # table with session_id directly).
        try:
            c.execute("SELECT session_id FROM claude_threads LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column: session_id" in str(e):
                log.info("migrating DB: adding session_id to claude_threads")
                c.execute("ALTER TABLE claude_threads ADD COLUMN session_id TEXT")
            # else: table doesn't exist yet (fresh install), schema.sql will create it

        # v3 migration (Phase 5.5 ratification) — must run BEFORE schema.sql
        # executescript so the new peer_ratifications table's FK references
        # land cleanly and the grandfather backfill UPDATE has columns to
        # write into. Same shape as the v2 session_id migration.
        try:
            c.execute("SELECT ratified FROM claude_peers LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column: ratified" in str(e):
                log.info("migrating DB: adding ratification columns to claude_peers (Phase 5.5)")
                c.execute("ALTER TABLE claude_peers ADD COLUMN ratified INTEGER NOT NULL DEFAULT 0")
                c.execute("ALTER TABLE claude_peers ADD COLUMN ratified_at TIMESTAMP")
                c.execute("ALTER TABLE claude_peers ADD COLUMN ratified_by TEXT")
                c.execute("ALTER TABLE claude_peers ADD COLUMN ratification_reason TEXT")
                # Grandfather backfill: every existing peer is treated as
                # already-ratified so task_claim doesn't suddenly 403.
                # Witnessing didn't exist before this PR — back-dating
                # ratified=true with a recognizable ratified_by tag keeps
                # the cohort distinction queryable forever.
                _grandfather_ts = _utcnow_iso()
                c.execute(
                    "UPDATE claude_peers SET ratified=1, ratified_at=?, "
                    "ratified_by='grandfather_v0_phase_5_5_migration', "
                    "ratification_reason='pre-existing peer at PR A migration; "
                    "no prior witness mechanism' WHERE ratified=0",
                    (_grandfather_ts,),
                )
                # Append corresponding rows to peer_ratifications for the
                # grandfather cohort. peer_ratifications doesn't exist yet
                # (created by schema.sql executescript below), so defer the
                # audit-row backfill to a second pass after schema.sql runs.
                _grandfather_pending = True
            else:
                _grandfather_pending = False
        else:
            _grandfather_pending = False

        # B2 migration (Meta-Edge login-scoped registry,
        # META_EDGE_IDENTITY_CONTRACT.md). Add the nullable `owner` column to
        # claude_peers for EXISTING DBs BEFORE schema.sql executescript (same
        # probe+ALTER pattern as the ratification ALTER above). Additive and
        # NULLABLE — existing/lab peers land owner=NULL and stay globally
        # visible; only Meta-Edge-registered cells carry an owner. On a fresh
        # install claude_peers doesn't exist yet — the OperationalError is
        # caught and skipped (schema.sql's CREATE includes `owner` directly).
        try:
            c.execute("SELECT owner FROM claude_peers LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column: owner" in str(e):
                log.info("migrating DB: adding owner column to claude_peers (B2 login-scoped registry)")
                c.execute("ALTER TABLE claude_peers ADD COLUMN owner TEXT")
            # else: "no such table" = fresh install → schema.sql creates it with
            # the column; any other OperationalError is a real fault → re-raise.
            elif "no such table: claude_peers" not in str(e):
                raise

        # v9 migration (R1 C3-A — trust-epoch stamp). Add binding_regime to
        # peer_ratifications BEFORE schema.sql executescript, mirroring the v3
        # ratification ALTER above. SQLite cannot add a CHECK constraint via
        # ALTER, so existing DBs get the column with the DEFAULT only; the
        # CHECK lives in the CREATE TABLE (fresh installs). Legacy rows are
        # backfilled to 'shared_token' in the post-executescript pass below.
        # On a fresh install peer_ratifications doesn't exist yet — the
        # OperationalError is caught and skipped (schema.sql creates it with
        # the column + CHECK directly).
        try:
            c.execute("SELECT binding_regime FROM peer_ratifications LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column: binding_regime" in str(e):
                log.info("migrating DB to v9: adding binding_regime to peer_ratifications (R1 C3-A)")
                c.execute(
                    "ALTER TABLE peer_ratifications ADD COLUMN "
                    "binding_regime TEXT NOT NULL DEFAULT 'shared_token'"
                )
            # else: table doesn't exist yet (fresh install) — schema.sql creates it

        # v6 migration (Phase 3 — Gemini-via-subscription): council_jobs.role
        # CHECK constraint widened from {r1_defender, r2_defender_judge} to
        # include Gemini side {r1_challenger, r2_challenger_judge}. SQLite
        # can't ALTER a CHECK constraint in place — must recreate the table.
        # Test by attempting to insert a Gemini-role row; if CHECK rejects,
        # we're on v5 schema and need recreate. If insert succeeds (v6 already
        # applied OR fresh install), clean up the probe row.
        try:
            _probe_id = f"__v6_migration_probe_{int(time.time()*1000)}__"
            c.execute(
                "INSERT INTO council_jobs (debate_id, role, prompt, status, created_at) "
                "VALUES (?, 'r1_challenger', '__probe__', 'pending', ?)",
                (_probe_id, _utcnow_iso()),
            )
            c.execute("DELETE FROM council_jobs WHERE debate_id = ?", (_probe_id,))
        except sqlite3.IntegrityError as _e:
            if "CHECK constraint" in str(_e) or "constraint failed" in str(_e):
                log.info("migrating DB to v6: widening council_jobs.role CHECK for Gemini roles")
                c.executescript("""
                    ALTER TABLE council_jobs RENAME TO council_jobs_v5_migration;
                    CREATE TABLE council_jobs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      debate_id TEXT NOT NULL,
                      role TEXT NOT NULL CHECK (role IN (
                        'r1_defender', 'r2_defender_judge',
                        'r1_challenger', 'r2_challenger_judge'
                      )),
                      prompt TEXT NOT NULL,
                      model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                      status TEXT NOT NULL DEFAULT 'pending',
                      claimed_by TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      max_attempts INTEGER NOT NULL DEFAULT 2,
                      created_at TIMESTAMP NOT NULL,
                      claimed_at TIMESTAMP,
                      completed_at TIMESTAMP,
                      result_json TEXT,
                      error TEXT,
                      UNIQUE(debate_id, role)
                    );
                    INSERT INTO council_jobs SELECT * FROM council_jobs_v5_migration;
                    DROP TABLE council_jobs_v5_migration;
                """)
                # Indexes get re-created by schema.sql executescript below
                # (CREATE INDEX IF NOT EXISTS is idempotent).
            else:
                raise
        except sqlite3.OperationalError as _e:
            # Table doesn't exist yet (fresh install) — schema.sql executescript
            # below will create with v6 CHECK directly. Nothing to migrate.
            if "no such table: council_jobs" not in str(_e):
                raise

        # v8 migration (Phase 2 — chat-sync endpoint): council_jobs.role
        # CHECK widened from v6's 4 roles to 5 (adds r0_public_chat for
        # /api/chat-sync). Same RENAME+CREATE+INSERT+DROP pattern as v6
        # since SQLite can't ALTER a CHECK constraint in place.
        # Probe by INSERTing a r0_public_chat row; CHECK fail = need migrate.
        try:
            _probe_id = f"__v8_migration_probe_{int(time.time()*1000)}__"
            c.execute(
                "INSERT INTO council_jobs (debate_id, role, prompt, status, created_at) "
                "VALUES (?, 'r0_public_chat', '__probe__', 'pending', ?)",
                (_probe_id, _utcnow_iso()),
            )
            c.execute("DELETE FROM council_jobs WHERE debate_id = ?", (_probe_id,))
        except sqlite3.IntegrityError as _e:
            if "CHECK constraint" in str(_e) or "constraint failed" in str(_e):
                log.info("migrating DB to v8: widening council_jobs.role CHECK for r0_public_chat")
                c.executescript("""
                    ALTER TABLE council_jobs RENAME TO council_jobs_v6_migration;
                    CREATE TABLE council_jobs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      debate_id TEXT NOT NULL,
                      role TEXT NOT NULL CHECK (role IN (
                        'r1_defender', 'r2_defender_judge',
                        'r1_challenger', 'r2_challenger_judge',
                        'r0_public_chat'
                      )),
                      prompt TEXT NOT NULL,
                      model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                      status TEXT NOT NULL DEFAULT 'pending',
                      claimed_by TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      max_attempts INTEGER NOT NULL DEFAULT 2,
                      created_at TIMESTAMP NOT NULL,
                      claimed_at TIMESTAMP,
                      completed_at TIMESTAMP,
                      result_json TEXT,
                      error TEXT,
                      UNIQUE(debate_id, role)
                    );
                    INSERT INTO council_jobs SELECT * FROM council_jobs_v6_migration;
                    DROP TABLE council_jobs_v6_migration;
                """)
                # Indexes get re-created by schema.sql executescript below
            else:
                raise
        except sqlite3.OperationalError as _e:
            # Table doesn't exist yet (fresh install) — same fall-through as v6
            if "no such table: council_jobs" not in str(_e):
                raise

        # subscription_chat role migration (version-INDEPENDENT — see schema.sql
        # comment): council_jobs.role CHECK widened to add subscription_chat.
        # SQLite can't ALTER a CHECK, so RENAME+CREATE+INSERT+DROP. Detection is
        # behavioral — probe-INSERT a subscription_chat row; CHECK fail = migrate;
        # success (already-widened) = harmless no-op delete. NOT schema_version-
        # gated, so it's fully decoupled from the parallel R1 v9/v10/v11 stream.
        try:
            _probe_id = f"__subchat_migration_probe_{int(time.time()*1000)}__"
            c.execute(
                "INSERT INTO council_jobs (debate_id, role, prompt, status, created_at) "
                "VALUES (?, 'subscription_chat', '__probe__', 'pending', ?)",
                (_probe_id, _utcnow_iso()),
            )
            c.execute("DELETE FROM council_jobs WHERE debate_id = ?", (_probe_id,))
        except sqlite3.IntegrityError as _e:
            if "CHECK constraint" in str(_e) or "constraint failed" in str(_e):
                log.info("migrating DB: widening council_jobs.role CHECK for subscription_chat")
                c.executescript("""
                    ALTER TABLE council_jobs RENAME TO council_jobs_subchat_migration;
                    CREATE TABLE council_jobs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      debate_id TEXT NOT NULL,
                      role TEXT NOT NULL CHECK (role IN (
                        'r1_defender', 'r2_defender_judge',
                        'r1_challenger', 'r2_challenger_judge',
                        'r0_public_chat',
                        'subscription_chat'
                      )),
                      prompt TEXT NOT NULL,
                      model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                      status TEXT NOT NULL DEFAULT 'pending',
                      claimed_by TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      max_attempts INTEGER NOT NULL DEFAULT 2,
                      created_at TIMESTAMP NOT NULL,
                      claimed_at TIMESTAMP,
                      completed_at TIMESTAMP,
                      result_json TEXT,
                      error TEXT,
                      UNIQUE(debate_id, role)
                    );
                    INSERT INTO council_jobs SELECT * FROM council_jobs_subchat_migration;
                    DROP TABLE council_jobs_subchat_migration;
                """)
            else:
                raise
        except sqlite3.OperationalError as _e:
            if "no such table: council_jobs" not in str(_e):
                raise

        # narrative_risk role migration (Grok $0 structured narrative-risk
        # lane): council_jobs.role CHECK widened to add
        # 'narrative_risk'. Same version-independent behavioral-probe pattern as
        # the subscription_chat migration above (SQLite can't ALTER a CHECK in
        # place — RENAME+CREATE+INSERT+DROP). Probe-INSERT a narrative_risk row;
        # CHECK fail = migrate; success (already-widened) = harmless no-op delete.
        try:
            _probe_id = f"__narrative_risk_migration_probe_{int(time.time()*1000)}__"
            c.execute(
                "INSERT INTO council_jobs (debate_id, role, prompt, status, created_at) "
                "VALUES (?, 'narrative_risk', '__probe__', 'pending', ?)",
                (_probe_id, _utcnow_iso()),
            )
            c.execute("DELETE FROM council_jobs WHERE debate_id = ?", (_probe_id,))
        except sqlite3.IntegrityError as _e:
            if "CHECK constraint" in str(_e) or "constraint failed" in str(_e):
                log.info("migrating DB: widening council_jobs.role CHECK for narrative_risk")
                c.executescript("""
                    ALTER TABLE council_jobs RENAME TO council_jobs_narrative_migration;
                    CREATE TABLE council_jobs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      debate_id TEXT NOT NULL,
                      role TEXT NOT NULL CHECK (role IN (
                        'r1_defender', 'r2_defender_judge',
                        'r1_challenger', 'r2_challenger_judge',
                        'r0_public_chat',
                        'subscription_chat',
                        'narrative_risk'
                      )),
                      prompt TEXT NOT NULL,
                      model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                      status TEXT NOT NULL DEFAULT 'pending',
                      claimed_by TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      max_attempts INTEGER NOT NULL DEFAULT 2,
                      created_at TIMESTAMP NOT NULL,
                      claimed_at TIMESTAMP,
                      completed_at TIMESTAMP,
                      result_json TEXT,
                      error TEXT,
                      UNIQUE(debate_id, role)
                    );
                    INSERT INTO council_jobs SELECT * FROM council_jobs_narrative_migration;
                    DROP TABLE council_jobs_narrative_migration;
                """)
            else:
                raise
        except sqlite3.OperationalError as _e:
            if "no such table: council_jobs" not in str(_e):
                raise

        # r1_grok_defender role migration (Grok $0 GENERATIVE bull-case defender
        # for a throttle-resilient council DEFENDER chain): widen the
        # council_jobs.role CHECK to add 'r1_grok_defender'. Same version-independent
        # behavioral-probe pattern as the narrative_risk migration above.
        try:
            _probe_id = f"__grok_defender_migration_probe_{int(time.time()*1000)}__"
            c.execute(
                "INSERT INTO council_jobs (debate_id, role, prompt, status, created_at) "
                "VALUES (?, 'r1_grok_defender', '__probe__', 'pending', ?)",
                (_probe_id, _utcnow_iso()),
            )
            c.execute("DELETE FROM council_jobs WHERE debate_id = ?", (_probe_id,))
        except sqlite3.IntegrityError as _e:
            if "CHECK constraint" in str(_e) or "constraint failed" in str(_e):
                log.info("migrating DB: widening council_jobs.role CHECK for r1_grok_defender")
                c.executescript("""
                    ALTER TABLE council_jobs RENAME TO council_jobs_grok_defender_migration;
                    CREATE TABLE council_jobs (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      debate_id TEXT NOT NULL,
                      role TEXT NOT NULL CHECK (role IN (
                        'r1_defender', 'r2_defender_judge',
                        'r1_challenger', 'r2_challenger_judge',
                        'r0_public_chat',
                        'subscription_chat',
                        'narrative_risk',
                        'r1_grok_defender'
                      )),
                      prompt TEXT NOT NULL,
                      model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                      status TEXT NOT NULL DEFAULT 'pending',
                      claimed_by TEXT,
                      attempts INTEGER NOT NULL DEFAULT 0,
                      max_attempts INTEGER NOT NULL DEFAULT 2,
                      created_at TIMESTAMP NOT NULL,
                      claimed_at TIMESTAMP,
                      completed_at TIMESTAMP,
                      result_json TEXT,
                      error TEXT,
                      UNIQUE(debate_id, role)
                    );
                    INSERT INTO council_jobs SELECT * FROM council_jobs_grok_defender_migration;
                    DROP TABLE council_jobs_grok_defender_migration;
                """)
            else:
                raise
        except sqlite3.OperationalError as _e:
            if "no such table: council_jobs" not in str(_e):
                raise

        # Channels (mesh-channels spec §2, build step 1) — additive columns on
        # claude_messages for EXISTING DBs (same probe+ALTER pattern as the
        # session_id / ratification migrations above). Fresh installs get them
        # from schema.sql's CREATE directly. DMs untouched: channel NULL = a DM
        # (today's rows unchanged); mentions is SERVER-DERIVED at post-time (B2,
        # step 2); priority defaults 'normal' ('high' gated like @all, B3).
        try:
            c.execute("SELECT channel FROM claude_messages LIMIT 1")
        except sqlite3.OperationalError as e:
            if "no such column: channel" in str(e):
                log.info("migrating DB: adding channels columns to claude_messages (mesh-channels step 1)")
                c.execute("ALTER TABLE claude_messages ADD COLUMN channel TEXT")
                c.execute("ALTER TABLE claude_messages ADD COLUMN mentions TEXT")
                c.execute("ALTER TABLE claude_messages ADD COLUMN priority TEXT DEFAULT 'normal'")
            # else: "no such table" = fresh install → schema.sql creates it with
            # the columns; any other OperationalError is a real fault → re-raise.
            elif "no such table: claude_messages" not in str(e):
                raise

        # seat-A B3/B4 migration — additive columns on scheduled_events for
        # EXISTING DBs (same probe+ALTER pattern as session_id / channels above).
        # Fresh installs get both columns from schema.sql's CREATE IF NOT EXISTS.
        # Only runs when scheduled_events already exists; a fresh DB where the
        # table is absent will be caught by "no such table" and skipped — the
        # executescript below handles fresh-install creation.
        try:
            c.execute("SELECT min_interval_sec FROM scheduled_events LIMIT 0")
        except sqlite3.OperationalError as e:
            if "no such column: min_interval_sec" in str(e):
                log.info("migrating DB: adding seat-A columns to scheduled_events (B3/B4)")
                c.execute(
                    "ALTER TABLE scheduled_events ADD COLUMN min_interval_sec INTEGER")
                c.execute(
                    "ALTER TABLE scheduled_events ADD COLUMN last_consumed_post_id INTEGER")
            # "no such table" = fresh install → schema.sql creates it with the
            # columns; any other OperationalError is a real fault → re-raise.
            elif "no such table: scheduled_events" not in str(e):
                raise

        with open(SCHEMA_PATH) as f:
            c.executescript(f.read())

        # v3 grandfather audit-row backfill — runs AFTER schema.sql created
        # peer_ratifications. Idempotent via NOT EXISTS guard so re-running
        # _init_db never duplicates the audit cohort.
        if _grandfather_pending:
            log.info("migrating DB: backfilling peer_ratifications grandfather cohort")
            grandfather_rows = c.execute(
                "SELECT name, ratified_at FROM claude_peers "
                "WHERE ratified_by='grandfather_v0_phase_5_5_migration'"
            ).fetchall()
            for r in grandfather_rows:
                exists = c.execute(
                    "SELECT 1 FROM peer_ratifications WHERE peer=? AND "
                    "ratified_by='grandfather_v0_phase_5_5_migration' LIMIT 1",
                    (r["name"],),
                ).fetchone()
                if exists:
                    continue
                c.execute(
                    "INSERT INTO peer_ratifications "
                    "(peer, ratified_by, ratified_at, reason, witness_dm_id) "
                    "VALUES (?, 'grandfather_v0_phase_5_5_migration', ?, "
                    "'pre-existing peer at PR A migration; no prior witness mechanism', NULL)",
                    (r["name"], r["ratified_at"]),
                )

        # v9 backfill (R1 C3-A) — runs AFTER schema.sql, inside this same
        # connection/transaction. The ALTER's DEFAULT already stamps
        # 'shared_token' on existing rows, but make it explicit + idempotent:
        # every pre-R1 ratification (grandfather cohort + drop's 2026-05-14
        # self-issued row + any witnessed flips) was authorized by a shared
        # token, so the trust epoch is 'shared_token'. Guarded on NULL/'' so
        # re-runs and per_peer_token rows (written by C3-B later) are never
        # clobbered.
        c.execute(
            "UPDATE peer_ratifications SET binding_regime='shared_token' "
            "WHERE binding_regime IS NULL OR binding_regime=''"
        )

    with _conn() as c:
        v = c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    log.info("DB ready at %s (schema v%s)", DB_PATH, v)


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row else None


# =====================================================================
# AUTH
# =====================================================================

def _meta_edge_public_key() -> Optional[str]:
    """Return the configured Meta-Edge RS256 PUBLIC key PEM, or None if unset.

    Read at CALL time (not cached at import) so the key can be rotated without a
    restart and monkeypatched directly in tests. ``META_EDGE_PUBLIC_KEY`` (inline
    PEM) takes precedence over ``META_EDGE_PUBLIC_KEY_FILE`` (path). A None return
    means Meta-Edge verification is simply NOT enabled (the gateway runs in
    lab-only mode); an unreadable key FILE also returns None (and logs) rather
    than raising — the JWT branch is only entered when this returns a key.
    """
    pem = os.environ.get("META_EDGE_PUBLIC_KEY", "").strip()
    if pem:
        return pem
    path = os.environ.get("META_EDGE_PUBLIC_KEY_FILE", "").strip()
    if path:
        try:
            return Path(path).read_text().strip() or None
        except OSError as e:
            log.warning("META_EDGE_PUBLIC_KEY_FILE unreadable (%s); Meta-Edge auth disabled", e)
            return None
    return None


def _looks_like_jwt(token: str) -> bool:
    """A compact JWS/JWT is three non-empty '.'-separated base64url segments."""
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def _verify_meta_edge_token(token: str) -> Optional[dict]:
    """Verify a Meta-Edge RS256 identity/join JWT — FAIL-CLOSED (B1 security core).

    Returns the verified claims dict on success, or None on ANY problem (the
    gateway TRUSTS Meta-Edge's tokens but never partial-trusts). Returns None when
    no Meta-Edge public key is configured (verification not enabled).

    The algorithm is PINNED to RS256 explicitly (``algorithms=["RS256"]``) — this
    single rule blocks BOTH ``alg:none`` (unsigned forgeries) AND the HS256
    algorithm-confusion attack (a token HMAC-signed with the RSA *public* key
    bytes as the shared secret). The token header NEVER picks the algorithm.
    iss/aud/exp and the presence of exp/iss/aud/sub are all required and verified;
    a 30s leeway absorbs clock skew only.
    """
    pem = _meta_edge_public_key()
    if not pem:
        return None
    issuer = os.environ.get("META_EDGE_ISSUER", META_EDGE_ISSUER_DEFAULT)
    audience = os.environ.get("META_EDGE_AUDIENCE", META_EDGE_AUDIENCE_DEFAULT)
    try:
        return jwt.decode(
            token,
            pem,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            leeway=30,
            options={
                "require": ["exp", "iss", "aud", "sub"],
                "verify_signature": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_exp": True,
            },
        )
    except Exception as e:
        # Fail-closed on EVERYTHING: bad signature, alg mismatch (none/HS256),
        # expired, wrong iss/aud, missing required claim, malformed token, or any
        # unexpected error. A security verifier denies on doubt and never leaks
        # which check failed to the caller (only logs server-side).
        log.warning("Meta-Edge token rejected: %s: %s", type(e).__name__, e)
        return None


class AuthContext(NamedTuple):
    """Resolved identity of an authenticated caller (R1 C1).

    Returned by _authorize. Three regimes:
      - 'shared_token'   : caller presented MESH_GATEWAY_TOKEN or the commander
                           token. peer=None (shared tokens carry no peer identity),
                           key_generation=None. This is today's behavior for every
                           caller until per-peer tokens are minted + adopted.
      - 'per_peer_token' : caller presented a per-peer token resolved via
                           peer_tokens.token_sha256. peer=<name>,
                           key_generation=<gen>.
      - 'signed'         : reserved for R2 (Ed25519 signed manifests); not issued
                           in R1.

    BACKWARD-COMPAT: the 35 existing _authorize() call sites ignore the return
    value, so returning an AuthContext instead of None is a no-op for them. Only
    C3-B (peer_ratify) and C2 (caller-binding) read it. Live behavior is
    unchanged: shared tokens authenticate exactly as before; per-peer resolution
    is additive and nothing enforces caller==peer yet.

    B1/B2 (META_EDGE_IDENTITY_CONTRACT.md) adds a THIRD principal kind alongside
    the two lab regimes, discriminated by ``kind`` (None for the lab regimes):
      - kind='user_identity' : a verified Meta-Edge SSO login. user=<sub> (the
                               canonical Meta-Edge user id / owner key),
                               provider/login informational. Scopes GET /peers to
                               owned cells; stamps owner=user on register.
      - kind='cell_join'     : a verified Meta-Edge join-key (the `tailscale up
                               --authkey` analog). owner=<owner claim>; a
                               one-purpose registration credential — stamps
                               owner on register, grants nothing else.
    All NamedTuple fields carry defaults so the two existing keyword call sites
    (shared_token / per_peer_token) are untouched and the identity branches
    construct with only their own fields.
    """
    peer: Optional[str] = None
    regime: str = "shared_token"
    key_generation: Optional[int] = None
    kind: Optional[str] = None
    user: Optional[str] = None
    provider: Optional[str] = None
    login: Optional[str] = None
    owner: Optional[str] = None


def _is_revoked(c, peer, key_generation) -> bool:
    """R1 C4: is this exact (peer, key_generation) token currently revoked?

    Takes an ALREADY-OPEN connection ``c`` and reuses it — _authorize calls
    this inside the SAME ``with _conn()`` block as the peer_tokens lookup, so a
    DB error here falls under the same try/except and fail-closes to 401 just
    like the token lookup does (no second connection, no extra latency).

    Latest-wins fold: the most recent row (MAX id) for the exact (peer,
    key_generation) pair decides — True iff its action='revoke'. No rows (or a
    None key) → False. Un-revoke is a NEW row with a higher id, so it flips the
    fold back to live without ever DELETEing the revoke row (append-only).
    """
    if peer is None or key_generation is None:
        return False
    row = c.execute(
        "SELECT action FROM peer_token_revocations WHERE peer=? AND key_generation=? "
        "ORDER BY id DESC LIMIT 1", (peer, key_generation)).fetchone()
    return row is not None and row["action"] == "revoke"


def _authorize(authorization: Optional[str]) -> AuthContext:
    if not AUTH_TOKEN:
        raise HTTPException(500, "server misconfigured: MESH_GATEWAY_TOKEN unset")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    # 0. Meta-Edge identity branch (B1/B2) — FIRST, and FAIL-CLOSED with NO
    #    fall-through. If the bearer is JWT-shaped AND a Meta-Edge public key is
    #    configured, it MUST be a valid Meta-Edge token: verification failure
    #    raises 401 here and is NEVER re-interpreted against the shared/peer-token
    #    paths (re-interpreting a forged/expired JWT would be a downgrade attack).
    #    Non-JWT bearers (the lab's opaque tokens) skip this branch untouched.
    if _looks_like_jwt(token) and _meta_edge_public_key():
        claims = _verify_meta_edge_token(token)
        if claims is None:
            raise HTTPException(401, "invalid Meta-Edge token")
        purpose = claims.get("purpose")
        if purpose in (None, "identity"):
            # Verified SSO login → read + owner-scope. sub is the canonical
            # Meta-Edge user id (the owner key); provider/login are informational.
            return AuthContext(
                kind="user_identity",
                user=claims.get("sub"),
                provider=claims.get("provider"),
                login=claims.get("login"),
            )
        if purpose == "cell-join":
            # A one-purpose join-key (tailscale up --authkey analog). It MUST
            # carry the owner it registers the cell under — a join-key without an
            # owner claim is fail-closed (it would otherwise register an
            # unowned/lab-visible cell, defeating the scoping it exists for).
            owner = claims.get("owner")
            if not owner:
                log.warning("Meta-Edge cell-join token missing owner claim; rejected")
                raise HTTPException(401, "invalid Meta-Edge token")
            return AuthContext(kind="cell_join", owner=owner)
        # A validly-signed token with an UNRECOGNIZED purpose is fail-closed —
        # never default an unknown purpose into the privileged identity path.
        log.warning("Meta-Edge token with unrecognized purpose=%r rejected", purpose)
        raise HTTPException(401, "invalid Meta-Edge token")
    # 1. Shared-token branch FIRST — verbatim from pre-C1, constant-time
    #    compare_digest preserved, and resolved before any DB hit so the common
    #    path takes no extra latency. Shared tokens carry no peer identity.
    accepted = [AUTH_TOKEN]
    if COMMANDER_TOKEN:
        accepted.append(COMMANDER_TOKEN)
    for candidate in accepted:
        if secrets.compare_digest(token, candidate):
            return AuthContext(peer=None, regime="shared_token", key_generation=None)
    # 2. Per-peer token branch (R1 C1) — resolve sha256 against peer_tokens via
    #    the indexed exact-match. Additive: a non-matching token still 401s
    #    exactly as before. The hash lookup leaks no timing about which peer
    #    (exact-match index, single row), and the raw token is never stored.
    token_sha = hashlib.sha256(token.encode()).hexdigest()
    revoked = False
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT peer, key_generation FROM peer_tokens WHERE token_sha256 = ?",
                (token_sha,),
            ).fetchone()
            # R1 C4: fold the revocation log for the resolved (peer, gen) using
            # the SAME open conn. Inside this try/with so a DB error in the fold
            # also fails-closed to 401 below (same fail-closed posture as the
            # token lookup itself — an auth layer denies on doubt).
            if row is not None:
                revoked = _is_revoked(c, row["peer"], row["key_generation"])
    except sqlite3.Error as e:
        # DB hiccup on the per-peer lookup must not convert a would-be 401 into
        # a 500 — fall through to the unchanged 401 (fail-closed: an auth layer
        # denies on doubt; a distinct 503 would be an availability-oracle + a
        # fail-open risk — daughter seat-B). Log so ops can distinguish a
        # "DB down → spurious 401 storm" from genuine bad-token traffic; the
        # 401 returned to the client is unchanged.
        log.warning("per-peer token lookup failed (DB error: %s); returning 401", e)
        row = None
    if row is not None:
        # R1 C4: a revoked token is DEAD on every endpoint. In enforce mode it
        # 401s (401 = dead/revoked token, NOT 403 — a 403 would mean valid
        # identity refused a scope; here the credential itself is dead). In the
        # warn-only default it still authenticates (return the AuthContext) but
        # logs so the adoption drive can see revoked-token traffic before the
        # flag flips. Commander-gated enforce, same discipline as C2.
        if revoked:
            log.warning(
                "revoked per-peer token presented: peer=%s gen=%s (enforce=%s)",
                row["peer"], row["key_generation"], MESH_REVOCATION_ENFORCE)
            if MESH_REVOCATION_ENFORCE:
                raise HTTPException(401, "token revoked")
        return AuthContext(
            peer=row["peer"],
            regime="per_peer_token",
            key_generation=row["key_generation"],
        )
    raise HTTPException(401, "bad token")


# =====================================================================
# CALLER→ACTOR BINDING (R1 C2 — warn-only by default)
# =====================================================================
# Closes "authenticated peer A claims to BE peer B in an actor-identity field"
# (from_node, claimed_by, ratified_by, posted_by, added_by, the observer
# path-param, peer, worker_id, and the subscription_chat cap-key). ONE canonical
# rule, applied per-site after _authorize, before the mutation.
#
# CRITICAL FRAMING (drop ultra #1667/#1669, lab #1672) — C2 IS NOT "SPOOFING
# FIXED": it is a NO-OP for shared-token callers (auth.peer is None →
# caller_unbound, never rejected, even with enforce=1), and 100% of live traffic
# uses the shared token today (peer_tokens has rows only after the adoption
# drive). The forge truly closes at C5 (retire BOTH shared tokens). C2 in warn
# mode is telemetry; in enforce mode it binds ONLY the per-peer-authenticated
# callers. Do not read "C2 enforce shipped" as "spoofing closed."
#
# Enforce flag is read at CALL time (module global) so it is monkeypatchable in
# tests and flips with an env change + restart (reversible in seconds).
MESH_CALLER_BINDING_ENFORCE = os.environ.get("MESH_CALLER_BINDING_ENFORCE", "0") == "1"
# R1 C4 — per-peer token revocation enforce flag. Read at CALL time (module
# global, monkeypatchable in tests, flips with an env change + restart). Default
# warn-only: a revoked token still authenticates but logs a warning, so merge is
# behavior-neutral (the enforce flip is commander-gated later — same discipline
# as the C2 binding flag above). When enforce=1, a revoked token 401s on EVERY
# endpoint (the check lives in _authorize, not per-site).
MESH_REVOCATION_ENFORCE = os.environ.get("MESH_REVOCATION_ENFORCE", "0") == "1"
# peer_health_events.peer is NOT NULL (schema) — shared-token (peer-less) callers
# record their telemetry under this sentinel so the INSERT can't fail.
_CALLER_BINDING_SHARED_SENTINEL = "__shared__"


def _record_caller_binding_event(event_type: str, peer: str, *, field: str,
                                  site: str, claimed, caller, regime: str) -> None:
    """Write one caller-binding telemetry row to peer_health_events.

    DELIBERATELY on its OWN connection, NOT the caller handler's transaction.
    Rationale (flag for seat-A/seat-B): a security-mismatch audit row must
    survive even if the subsequent business mutation rolls back, and a telemetry
    write hiccup must never roll back / 500 the real operation. Decoupling gives
    both properties. The enforce-mode 403 is raised by _check_caller_binding
    BEFORE the handler's mutation block is entered, so "deny before mutation"
    holds without sharing a txn. (The spec said "inside the existing txn"; this
    is the same security guarantee with cleaner failure isolation — call it out
    in review.)
    """
    md = json.dumps({
        "site": site, "field": field, "claimed_actor": claimed,
        "caller_peer": caller, "regime": regime,
        "enforce": MESH_CALLER_BINDING_ENFORCE,
    })
    now = _utcnow_iso()
    try:
        with _conn() as c:
            # seat-B guard (drop #2): WAL serializes writers, so a concurrent
            # handler write-txn could SQLITE_BUSY this telemetry INSERT. A
            # busy_timeout makes a transient collision WAIT (a few seconds)
            # rather than error-and-drop the security row. The helper runs +
            # commits BEFORE the handler opens its mutation txn (drop #1
            # ordering), so collisions should be rare; this is belt-and-suspenders
            # so a rejected-spoof record is never lost to a transient lock.
            c.execute("PRAGMA busy_timeout = 5000")
            c.execute(
                "INSERT INTO peer_health_events "
                "(time, peer, event_type, source, metadata, created_at) "
                "VALUES (?, ?, ?, 'c2_caller_binding', ?, ?)",
                (now, peer, event_type, md, now),
            )
    except sqlite3.Error as e:
        # Telemetry failure must NEVER break the request path.
        log.warning("caller-binding telemetry write failed (%s); continuing", e)


def _check_caller_binding(auth: AuthContext, actor_value, *, field: str, site: str,
                          enforce_identity: bool = True) -> None:
    """C2 canonical rule: the authenticated caller must match the actor-identity
    it claims in `actor_value` (the from_node/claimed_by/ratified_by/... field).

    - shared-token caller (auth.peer is None): records a 'caller_unbound'
      telemetry row under the __shared__ sentinel and RETURNS — NEVER raises,
      even in enforce mode. (This is the documented no-op: shared tokens carry no
      peer identity to bind. Real closure is C5.)
    - bound caller, auth.peer == actor_value: silent pass (the happy path —
      writes NO row).
    - bound caller, auth.peer != actor_value: records 'caller_binding_mismatch';
      then in enforce mode raises 403, in warn mode logs + returns (mutation
      proceeds).

    enforce_identity=False — SCOPE-carveout (#1693): for sites where `actor_value`
    is a SUB-SCOPE of the caller peer (e.g. a council ``worker_id`` like
    ``council-worker-7`` owned by pool peer ``node-a``), NOT a peer identity.
    auth.peer can structurally NEVER equal such a value, so the identity 403 is a
    category error. With this flag the telemetry row is STILL recorded
    (observe-only — the RBAC-scope rung keeps the data) but the enforce-mode 403
    is SKIPPED. Job-ownership (worker_id == council_jobs.claimed_by) is a SEPARATE,
    intact gate handled at the call site. Full RBAC pool-ownership is deferred to
    #1693.
    """
    if auth.peer is None:
        _record_caller_binding_event(
            "caller_unbound", _CALLER_BINDING_SHARED_SENTINEL,
            field=field, site=site, claimed=actor_value, caller=None,
            regime=auth.regime)
        return
    if auth.peer == actor_value:
        return
    _record_caller_binding_event(
        "caller_binding_mismatch", auth.peer,
        field=field, site=site, claimed=actor_value, caller=auth.peer,
        regime=auth.regime)
    if enforce_identity and MESH_CALLER_BINDING_ENFORCE:
        raise HTTPException(
            403,
            f"caller-binding: authenticated peer {auth.peer!r} may not act as "
            f"{field}={actor_value!r}")
    if not enforce_identity:
        # SCOPE-carveout (#1693): actor_value is a sub-scope (council worker_id),
        # NOT a peer identity — auth.peer can structurally never equal it, so the
        # identity 403 is a category error here. Telemetry STILL recorded above
        # (observe-only, so the RBAC-scope rung keeps the data); only the raise is
        # skipped. Job-ownership (worker_id==claimed_by) is a SEPARATE intact gate.
        log.info("caller-binding scope-carveout (observe-only): peer=%s %s=%r site=%s",
                 auth.peer, field, actor_value, site)
    else:
        log.warning("caller-binding MISMATCH (warn-only): peer=%s claimed %s=%r site=%s",
                    auth.peer, field, actor_value, site)


# =====================================================================
# HEALTH
# =====================================================================

@app.get("/health")
async def health() -> dict:
    """Public liveness — no auth, no peer-state-disclosing details."""
    try:
        with _conn() as c:
            v = c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
            n_peers = c.execute("SELECT COUNT(*) AS n FROM claude_peers WHERE enabled=1").fetchone()["n"]
            n_pending = c.execute("SELECT COUNT(*) AS n FROM claude_tasks WHERE status='pending'").fetchone()["n"]
        return {
            "status": "ok",
            "service": "mesh-gateway",
            "version": app.version,
            "schema_version": v,
            "db_path": DB_PATH,
            "n_peers_enabled": n_peers,
            "n_tasks_pending": n_pending,
        }
    except Exception as e:
        log.exception("health check failed")
        raise HTTPException(500, f"db unhealthy: {type(e).__name__}: {e}")


# =====================================================================
# PEERS
# =====================================================================

class PeerRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    url: str = Field(..., min_length=1, max_length=256)
    capabilities: dict = Field(default_factory=dict)


@app.post("/peers/register")
async def peers_register(req: PeerRegisterRequest,
                          authorization: Optional[str] = Header(None)) -> dict:
    """Peer claude-service self-registration.

    Called from each peer's systemd ExecStartPost or on-demand. Upserts on
    name. capabilities dict is the JSON snapshot from claude-service /health.
    """
    ctx = _authorize(authorization)
    # B2 owner-stamping (META_EDGE_IDENTITY_CONTRACT.md §Cell-join). A Meta-Edge
    # principal ties the registered cell to its owner (the node-in-your-tailnet
    # relationship); lab principals (shared/commander/per-peer token) leave owner
    # NULL — unchanged behavior:
    #   - cell_join     → owner = the join-key's owner claim (headless join).
    #   - user_identity → owner = the verified user `sub` (interactive join: the
    #                     user's app registers the cell on their behalf).
    owner: Optional[str] = None
    if ctx.kind == "cell_join":
        owner = ctx.owner
    elif ctx.kind == "user_identity":
        owner = ctx.user
    now = _utcnow_iso()
    # R1 C1: mint-once per-peer token. Set only when a fresh token is minted
    # on this call; a re-register of a peer whose LIVE token still exists returns
    # peer_token=None + token_status='existing' and NEVER re-mints (re-minting
    # would silently invalidate the peer's stored credential).
    # R1 C4: the one exception is a re-register whose latest generation has been
    # REVOKED — that mints a NEW generation (the revoked one stays dead), so a
    # rotated/falsely-revoked peer can re-onboard with a fresh key. See below.
    minted_raw_token: Optional[str] = None
    token_status = "existing"
    with _conn() as c:
        # Phase 5.5: new peers default ratified=0 on INSERT.
        # ON CONFLICT preserves existing ratification state (so a peer
        # re-registering after capability update doesn't lose its
        # already-witnessed ratified=1 flip). Witness flips ratified=1
        # via PATCH /peers/{name}.
        #
        # R1 C1: the peer upsert + the token mint must be ONE transaction so a
        # crash can't leave a peer row without its token (or vice-versa).
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                """INSERT INTO claude_peers
                     (name, url, capabilities, registered_at, last_seen, ratified, owner)
                   VALUES (?, ?, ?, ?, ?, 0, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     url = excluded.url,
                     capabilities = excluded.capabilities,
                     last_seen = excluded.last_seen,
                     -- B2: a Meta-Edge register stamps/refreshes owner; a lab
                     -- (owner-NULL) re-register PRESERVES an existing owner via
                     -- COALESCE rather than clobbering it back to NULL.
                     owner = COALESCE(excluded.owner, claude_peers.owner)""",
                (req.name, req.url, json.dumps(req.capabilities), now, now, owner),
            )
            # R1 C4: mint-once becomes mint-or-rotate-past-a-revocation.
            #   - NO token row          → C1's gen=1 mint (first onboarding).
            #   - latest gen REVOKED    → mint a NEW gen (max_gen+1); the
            #     revoked gen stays dead+auditable so a compromised/rotated key
            #     can never be resurrected by re-registering, while a falsely-
            #     revoked or rotated peer can re-onboard with a fresh key.
            #   - latest gen NOT revoked→ C1's mint-once (return existing,
            #     token_status='existing', NEVER re-mint — re-minting would
            #     silently invalidate the peer's stored live credential).
            # C5 gen-reuse guard (sweep #1897): seed new_gen from the high-water
            # mark of BOTH peer_tokens AND peer_token_revocations, not peer_tokens
            # alone. deregister PURGES peer_tokens but the revocation audit rows
            # survive (no FK, by design) — so after a deregister+re-register the
            # peer_tokens MAX resets to None and the old logic minted gen=1 again,
            # colliding with the surviving gen=1 revocation row → a BORN-REVOKED
            # token (works under warn-only, 401s the instant enforce flips). Seeding
            # from max(tokens_gen, revocations_gen) means a purged-then-recreated
            # peer always gets a fresh, never-revoked generation.
            max_gen_row = c.execute(
                "SELECT MAX(key_generation) AS g FROM peer_tokens WHERE peer = ?",
                (req.name,),
            ).fetchone()
            max_tok_gen = max_gen_row["g"] if max_gen_row else None
            max_rev_row = c.execute(
                "SELECT MAX(key_generation) AS g FROM peer_token_revocations WHERE peer = ?",
                (req.name,),
            ).fetchone()
            max_rev_gen = max_rev_row["g"] if max_rev_row else None
            hwm = max(max_tok_gen or 0, max_rev_gen or 0)  # gen high-water mark
            if max_tok_gen is None or _is_revoked(c, req.name, max_tok_gen):
                # first onboarding (hwm 0 → gen 1), purged peer (hwm = revoked gen
                # → next), or latest live gen revoked → always a fresh gen past hwm
                new_gen = hwm + 1
            else:
                new_gen = None  # live token exists → keep it (mint-once)
            if new_gen is not None:
                minted_raw_token = secrets.token_urlsafe(32)
                token_sha = hashlib.sha256(minted_raw_token.encode()).hexdigest()
                c.execute(
                    "INSERT INTO peer_tokens "
                    "(peer, token_sha256, key_generation, minted_at, minted_via) "
                    "VALUES (?, ?, ?, ?, 'register')",
                    (req.name, token_sha, new_gen, now),
                )
                token_status = "minted"
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            # A token_sha256 UNIQUE collision (astronomically unlikely with a
            # 32-byte secret) or any DB error rolls back the whole register.
            minted_raw_token = None
            raise
        registered = c.execute(
            "SELECT ratified FROM claude_peers WHERE name = ?", (req.name,)
        ).fetchone()
    log.info("peer registered: %s @ %s (ratified=%s, token=%s)",
             req.name, req.url, bool(registered["ratified"]), token_status)
    return {
        "status": "registered",
        "name": req.name,
        "registered_at": now,
        "ratified": bool(registered["ratified"]),
        "registered_unratified": not bool(registered["ratified"]),
        # R1 C1: raw token returned EXACTLY ONCE, only on mint. On every
        # subsequent register it is None with token_status='existing'.
        "peer_token": minted_raw_token,
        "token_status": token_status,
    }


@app.get("/peers")
async def peers_list(authorization: Optional[str] = Header(None),
                      enabled_only: bool = Query(True)) -> dict:
    ctx = _authorize(authorization)
    clauses: list[str] = []
    params: list = []
    if enabled_only:
        clauses.append("enabled = 1")
    # B2 login-scoping: a Meta-Edge user_identity sees ONLY its OWN cells
    # (owner == sub). Every other principal (shared/commander/per-peer token —
    # the lab paths) is UNCHANGED and still sees all peers.
    if ctx.kind == "user_identity":
        clauses.append("owner = ?")
        params.append(ctx.user)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as c:
        rows = c.execute(
            f"SELECT name, url, capabilities, registered_at, last_health, "
            f"last_seen, enabled, ratified, ratified_at, ratified_by, "
            f"ratification_reason, owner "
            f"FROM claude_peers {where} ORDER BY name",
            params,
        ).fetchall()
    return {
        "peers": [
            {
                **dict(r),
                "capabilities": json.loads(r["capabilities"]),
                "ratified": bool(r["ratified"]),
            }
            for r in rows
        ]
    }


def _fetch_peers_for_features() -> list[dict]:
    """Pull the peer rows /features needs.

    Extracted as its own function so tests can monkeypatch without touching
    the DB. Same shape as /peers: {name, capabilities=parsed_dict}.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT name, capabilities FROM claude_peers WHERE enabled = 1"
        ).fetchall()
    return [
        {"name": r["name"], "capabilities": json.loads(r["capabilities"])}
        for r in rows
    ]


@app.get("/features")
async def features_list(authorization: Optional[str] = Header(None)) -> dict:
    """Aggregate published_features across allowlisted cells.

    Derived-on-read from claude_peers.capabilities (spec §3.2(c)) — no new
    table. Pipeline: aggregate → allowlist-gate → cap-enforce. Each stage
    is a pure function in feature_registry, individually unit-tested.
    """
    _authorize(authorization)
    peers = _fetch_peers_for_features()
    entries = aggregate_features(peers)
    entries = apply_allowlist(entries)
    entries = apply_caps(entries)
    return {
        "features": entries,
        "count": len(entries),
        "generated_at": _utcnow_iso(),
    }


@app.get("/peers/{name}")
async def peer_get(name: str,
                    authorization: Optional[str] = Header(None)) -> dict:
    """Single-peer query — full row including ratification state.

    Phase 5.5 per PLAN.md §15.4a step 3: witness queries this at
    ttl_seconds=0 (i.e., bypass any client-side cache) before flipping
    ratified=true to confirm the peer is in registered_unratified state.
    """
    _authorize(authorization)
    with _conn() as c:
        r = c.execute(
            "SELECT name, url, capabilities, registered_at, last_health, "
            "last_seen, enabled, ratified, ratified_at, ratified_by, "
            "ratification_reason FROM claude_peers WHERE name = ?",
            (name,),
        ).fetchone()
    if not r:
        raise HTTPException(404, f"peer {name!r} not registered")
    d = dict(r)
    d["capabilities"] = json.loads(d["capabilities"])
    d["ratified"] = bool(d["ratified"])
    return d


@app.get("/peers/{name}/health")
async def peer_health(name: str, authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)
    with _conn() as c:
        r = c.execute(
            "SELECT name, url, capabilities, last_health, last_seen, enabled "
            "FROM claude_peers WHERE name = ?", (name,)
        ).fetchone()
    if not r:
        raise HTTPException(404, f"peer {name!r} not registered")
    d = dict(r)
    d["capabilities"] = json.loads(d["capabilities"])
    return d


class PeerPatchRequest(BaseModel):
    """Phase 5.5 ratification flip body. Only ``ratified=true`` is supported
    in this PR — un-ratification is structurally meaningless because
    peer_ratifications is append-only. Witness identity comes from the
    ``ratified_by`` field, NOT from auth header — auth gate is bearer-token
    on the gateway, but the witness-name semantics live in the request body
    so the audit row records WHO claimed witness, not just which token was
    used (one token may be shared by multiple peer processes).
    """
    ratified: bool = Field(..., description="must be true; un-ratify not supported")
    ratified_by: str = Field(..., min_length=1, max_length=64,
                              description="canonical witness peer name")
    reason: Optional[str] = Field(None, max_length=512,
                                   description="short free-text from witness")
    witness_dm_id: Optional[int] = Field(None,
                                          description="optional FK pointer to handshake DM in claude_messages")


@app.patch("/peers/{name}")
async def peer_ratify(name: str, req: PeerPatchRequest,
                       authorization: Optional[str] = Header(None)) -> dict:
    """Phase 5.5 ratification flip per PLAN.md §15.4a.

    Server-side enforcement of the contract:

    - Witness must itself be ratified=1 (no unratified peer can ratify another)
    - No self-ratification (witness != target)
    - Target must currently be ratified=0 (idempotent re-flip would muddle audit;
      silently no-op is rejected so callers see explicit state)

    On success, both `claude_peers` ratification fields update AND a new row
    lands in `peer_ratifications` (append-only audit). Both writes are in the
    same transaction.

    R1 C3-B: the audit row's binding_regime is DERIVED FROM THE AUTH PATH
    (whether the caller authenticated via a shared or per-peer token), never
    from a request body field — so the trust epoch of each ratification is
    recorded truthfully and cannot be spoofed by the witness.
    """
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.ratified_by, field="ratified_by", site="peer_ratify")
    if not req.ratified:
        raise HTTPException(400, "un-ratification not supported (audit is append-only)")
    if req.ratified_by == name:
        raise HTTPException(400, "self-ratification rejected (witness != target)")

    with _conn() as c:
        # 1. Witness must exist + be ratified
        witness = c.execute(
            "SELECT ratified FROM claude_peers WHERE name = ?", (req.ratified_by,)
        ).fetchone()
        if not witness:
            raise HTTPException(403, f"witness {req.ratified_by!r} not registered")
        if not bool(witness["ratified"]):
            raise HTTPException(
                403,
                f"witness {req.ratified_by!r} is not ratified — "
                f"unratified peers cannot ratify others",
            )
        # 2. Target must exist + be unratified
        target = c.execute(
            "SELECT ratified FROM claude_peers WHERE name = ?", (name,)
        ).fetchone()
        if not target:
            raise HTTPException(404, f"peer {name!r} not registered")
        if bool(target["ratified"]):
            raise HTTPException(
                409,
                f"peer {name!r} is already ratified (audit log preserves "
                f"the original flip; re-ratification would muddle provenance)",
            )
        # 3. Atomic flip + audit insert
        now = _utcnow_iso()
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "UPDATE claude_peers SET ratified=1, ratified_at=?, "
                "ratified_by=?, ratification_reason=? WHERE name=?",
                (now, req.ratified_by, req.reason, name),
            )
            c.execute(
                "INSERT INTO peer_ratifications "
                "(peer, ratified_by, ratified_at, reason, witness_dm_id, binding_regime) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, req.ratified_by, now, req.reason, req.witness_dm_id,
                 auth.regime),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        # 4. Re-read for response
        full = c.execute(
            "SELECT name, url, capabilities, registered_at, last_health, "
            "last_seen, enabled, ratified, ratified_at, ratified_by, "
            "ratification_reason FROM claude_peers WHERE name = ?",
            (name,),
        ).fetchone()

    log.info("peer ratified: %s by %s (reason=%r)", name, req.ratified_by, req.reason)
    d = dict(full)
    d["capabilities"] = json.loads(d["capabilities"])
    d["ratified"] = bool(d["ratified"])
    return d


@app.get("/peers/{name}/ratifications")
async def peer_ratifications_list(name: str,
                                    authorization: Optional[str] = Header(None)) -> dict:
    """Audit log query — returns all ratification rows for a peer.

    Read-only; appendable by PATCH /peers/{name} only. Used by witnesses
    + future audit jobs to confirm provenance ('who ratified this peer
    and when?').
    """
    _authorize(authorization)
    with _conn() as c:
        # Confirm peer exists for clean 404
        peer = c.execute(
            "SELECT name FROM claude_peers WHERE name = ?", (name,)
        ).fetchone()
        if not peer:
            raise HTTPException(404, f"peer {name!r} not registered")
        rows = c.execute(
            "SELECT id, peer, ratified_by, ratified_at, reason, witness_dm_id "
            "FROM peer_ratifications WHERE peer = ? ORDER BY ratified_at ASC",
            (name,),
        ).fetchall()
    return {"peer": name, "ratifications": [dict(r) for r in rows]}


@app.delete("/peers/{name}")
async def peer_deregister(name: str, authorization: Optional[str] = Header(None)) -> dict:
    """Remove a peer from the registry.

    INTENTIONAL: peer_ratifications rows are preserved across DELETE —
    they are NOT cascade-deleted (drop PR #11 review carry-forward (c)
    DM #728). The audit log is append-only by design; a peer's
    ratification history survives their deregistration so 'who ratified
    this name and when?' remains queryable forever even if the registry
    row is later reused or recycled.

    The schema FK (peer_ratifications.peer → claude_peers.name) does NOT
    declare ON DELETE CASCADE; SQLite's default ON DELETE NO ACTION
    leaves the audit rows orphaned-but-queryable, which is the desired
    shape. Operators wanting hard cleanup should issue an explicit
    DELETE on peer_ratifications WHERE peer = ? — manual, intentional,
    audit-trail-breaking.
    """
    auth = _authorize(authorization)
    # Ownership gate (drop ultra #1669 gap #3): a per-peer caller may deregister
    # ONLY itself (caller==name path-param) — else under C4 a peer could revoke a
    # rival's credential (authenticated cross-peer credential-DoS). Shared-token
    # callers stay unbound (admin path), same no-op semantics as the actor binder.
    _check_caller_binding(auth, name, field="deregister_target", site="peer_deregister")
    with _conn() as c:
        r = c.execute("SELECT name FROM claude_peers WHERE name = ?", (name,)).fetchone()
        if not r:
            raise HTTPException(404, f"peer {name!r} not registered")
        # R1 C1: a deregistered peer's per-peer token MUST stop authenticating —
        # otherwise its orphaned peer_tokens row would still resolve to the
        # (now-gone) identity via _authorize's per-peer branch. Purge the
        # token(s) in the SAME transaction as the peer delete so deregister
        # stays atomic + fail-closed. (peer_tokens has NO FK on peer by design
        # — see schema — so the delete order is ours to control; C4 layers a
        # revoke-audit row on top of this purge.) peer_ratifications rows are
        # deliberately NOT touched (append-only audit, per the docstring above).
        c.execute("BEGIN IMMEDIATE")
        try:
            # R1 C4: stamp a revoke row for EACH live generation BEFORE the
            # purge, in the SAME txn. A crash between the revoke-insert and the
            # DELETEs leaves the token revoked (fail-closed); the audit row has
            # NO FK on peer so it survives the claude_peers/peer_tokens delete
            # (schema rationale). actor=name (caller==name per the C2 ownership
            # gate above), reason='deregister', no witness (self-action).
            now = _utcnow_iso()
            gens = c.execute(
                "SELECT key_generation FROM peer_tokens WHERE peer = ?", (name,)
            ).fetchall()
            for g in gens:
                c.execute(
                    "INSERT INTO peer_token_revocations "
                    "(peer, key_generation, action, actor, reason, witness_dm_id, created_at) "
                    "VALUES (?, ?, 'revoke', ?, 'deregister', NULL, ?)",
                    (name, g["key_generation"], name, now),
                )
            c.execute("DELETE FROM peer_tokens WHERE peer = ?", (name,))
            c.execute("DELETE FROM claude_peers WHERE name = ?", (name,))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    log.info("peer deregistered: %s (per-peer token purged, %d gen(s) revoked)",
             name, len(gens))
    return {"status": "deregistered", "name": name}


class PeerUnrevokeRequest(BaseModel):
    """R1 C4 un-revoke body. Mirrors PeerPatchRequest style (witness identity in
    the body, not the auth header — the audit row records WHO witnessed, not just
    which token authed). Un-revoke is WITNESSED: a non-revoked ratified peer must
    vouch, and the caller must BE that witness (C2 binding on the witness field),
    so a peer can't forge another's name as its un-revoke witness.
    """
    key_generation: int = Field(..., description="the (peer,gen) token to un-revoke")
    witness: str = Field(..., min_length=1, max_length=64,
                         description="canonical witnessing peer name (must be ratified + non-revoked)")
    reason: Optional[str] = Field(None, max_length=512,
                                   description="short free-text from witness")
    witness_dm_id: Optional[int] = Field(None,
                                          description="optional FK pointer to handshake DM in claude_messages")


@app.post("/peers/{name}/unrevoke")
async def peer_unrevoke(name: str, req: PeerUnrevokeRequest,
                        authorization: Optional[str] = Header(None)) -> dict:
    """R1 C4 — witnessed, append-only un-revoke of a (peer, key_generation).

    Append-only: NEVER DELETEs the revoke row. An un-revoke is a NEW
    peer_token_revocations row (action='unrevoke') with a higher id; _is_revoked's
    latest-wins fold flips the token back to live. This keeps the full kill/revive
    history auditable forever.

    Gates:
      - caller must BE the witness (C2 binding on the `witness` field — a peer
        can't forge another as its un-revoke witness; shared-token callers stay
        unbound/admin per the binder's None-semantics).
      - the witness must be a registered, ratified=1 peer AND its own latest
        token generation must NOT be revoked (a revoked peer can't witness an
        un-revoke). 404/403 otherwise.
    """
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.witness, field="witness", site="peer_unrevoke")
    # SEPARATION OF DUTIES (C4 seat-B / build-review HIGH finding): a peer may
    # NEVER witness its own un-revoke. Without this, a peer whose key was revoked
    # rotates to a live generation (the resurrection-guard mints gen+1), then
    # self-witnesses the un-revoke of its OWN stolen generation — resurrecting the
    # compromised key and defeating the entire point of revocation. The witness
    # MUST be a different ratified, non-revoked, token-holding peer. Pure check,
    # fail-fast before any DB work.
    if req.witness == name:
        raise HTTPException(
            403,
            f"a peer may not witness its own un-revoke ({name!r}) — un-revoke "
            f"requires a DIFFERENT ratified, non-revoked witness (separation of duties)",
        )
    now = _utcnow_iso()
    with _conn() as c:
        # All preconditions run UNDER the write lock (BEGIN IMMEDIATE) so they
        # observe committed state — closes the TOCTOU where a concurrent revoke/
        # deregister of the witness could land between a pre-lock check and the
        # insert (the exact invariant C4 protects). Same read-then-act-under-lock
        # discipline as deregister.
        c.execute("BEGIN IMMEDIATE")
        try:
            # 1. Target must CURRENTLY be revoked — else there is nothing to
            #    un-revoke. Reject (409, mirroring peer_ratify's already-state
            #    discipline) so the append-only log never accrues a phantom/no-op
            #    unrevoke row, and a not-yet-minted generation can't be pre-seeded
            #    with an unrevoke that would later bless a freshly-minted key.
            if not _is_revoked(c, name, req.key_generation):
                raise HTTPException(
                    409,
                    f"({name!r}, gen {req.key_generation}) is not currently "
                    f"revoked — nothing to un-revoke",
                )
            # 2. Witness must exist + be ratified.
            witness = c.execute(
                "SELECT ratified FROM claude_peers WHERE name = ?", (req.witness,)
            ).fetchone()
            if not witness:
                raise HTTPException(403, f"witness {req.witness!r} not registered")
            if not bool(witness["ratified"]):
                raise HTTPException(
                    403,
                    f"witness {req.witness!r} is not ratified — "
                    f"unratified peers cannot witness an un-revoke",
                )
            # 3. Witness must HOLD a live per-peer token: a minted generation
            #    (w_gen NOT NULL — a ratified-but-tokenless peer cannot witness,
            #    since _is_revoked(None) is vacuously False and would wave it
            #    through) AND that generation must not itself be revoked.
            w_gen_row = c.execute(
                "SELECT MAX(key_generation) AS g FROM peer_tokens WHERE peer = ?",
                (req.witness,),
            ).fetchone()
            w_gen = w_gen_row["g"] if w_gen_row else None
            if w_gen is None:
                raise HTTPException(
                    403,
                    f"witness {req.witness!r} holds no per-peer token — "
                    f"cannot witness an un-revoke",
                )
            if _is_revoked(c, req.witness, w_gen):
                raise HTTPException(
                    403,
                    f"witness {req.witness!r} is itself revoked — "
                    f"a revoked peer cannot witness an un-revoke",
                )
            # 4. Append the un-revoke row (latest-wins fold flips _is_revoked).
            #    NO DELETE of the prior revoke row — append-only audit.
            c.execute(
                "INSERT INTO peer_token_revocations "
                "(peer, key_generation, action, actor, reason, witness_dm_id, created_at) "
                "VALUES (?, ?, 'unrevoke', ?, ?, ?, ?)",
                (name, req.key_generation, req.witness, req.reason,
                 req.witness_dm_id, now),
            )
            c.execute("COMMIT")
        except Exception:
            # HTTPException (precondition reject) OR a real DB error: roll back
            # the BEGIN IMMEDIATE (read-only so far on the reject paths) and
            # re-raise. ROLLBACK is safe on a write-free txn.
            c.execute("ROLLBACK")
            raise
        revoked_now = _is_revoked(c, name, req.key_generation)
    log.info("peer token un-revoked: %s gen=%s by %s (reason=%r)",
             name, req.key_generation, req.witness, req.reason)
    return {"peer": name, "key_generation": req.key_generation, "revoked": revoked_now}


@app.get("/migration-status")
async def migration_status(authorization: Optional[str] = Header(None),
                           enabled_only: bool = Query(True)) -> dict:
    """R1 C5 step 5b — per-peer per-peer-token adoption readout (the enforce-flip
    gate). Read-only: NO mutation, NO enforce change. Exposes WHICH peers have
    presented a working per-peer token vs are still shared-token-only, so the
    commander can see the blast radius BEFORE flipping any enforce or retiring the
    shared tokens. Token VALUES are never returned — only the adoption fact.

    A peer is `adopted` iff it has a peer_tokens row whose LATEST generation is
    NOT revoked (so a peer's revoked gen-1 + live gen-2 reads as adopted; a peer
    whose only/latest gen was revoked reads as NOT adopted and must re-mint).

    The C5 gate (per docs/SWARPH_PEER_TOKEN_ADOPTION.md) closes when every ENABLED
    peer is adopted: `safe_to_retire_shared` reflects exactly that. Disabled peers
    are excluded from the gate by default (enabled_only=True) but visible with
    enabled_only=false for a full audit.
    """
    _authorize(authorization)
    where = "WHERE enabled = 1" if enabled_only else ""
    peers_out = []
    with _conn() as c:
        rows = c.execute(
            f"SELECT name, enabled FROM claude_peers {where} ORDER BY name"
        ).fetchall()
        for r in rows:
            name = r["name"]
            mg = c.execute(
                "SELECT MAX(key_generation) AS g FROM peer_tokens WHERE peer = ?",
                (name,),
            ).fetchone()
            latest_gen = mg["g"] if mg else None
            if latest_gen is None:
                adopted, regime = False, "shared_token_only"
            elif _is_revoked(c, name, latest_gen):
                adopted, regime = False, "latest_gen_revoked"
            else:
                adopted, regime = True, "per_peer_token"
            peers_out.append({
                "name": name,
                "enabled": bool(r["enabled"]),
                "adopted": adopted,
                "latest_gen": latest_gen,
                "regime": regime,
            })
    total = len(peers_out)
    adopted_count = sum(1 for p in peers_out if p["adopted"])
    pending = [p["name"] for p in peers_out if not p["adopted"]]
    all_adopted = total > 0 and adopted_count == total
    return {
        "scope": "enabled" if enabled_only else "all",
        "total": total,
        "adopted_count": adopted_count,
        "pending": pending,
        "all_adopted": all_adopted,
        "enforce": {
            "caller_binding": MESH_CALLER_BINDING_ENFORCE,
            "revocation": MESH_REVOCATION_ENFORCE,
        },
        # The C5 gate: retiring the shared tokens 401s any not-yet-adopted peer,
        # so it's only safe once EVERY enabled peer is adopted. Commander-gated
        # regardless — this is a readiness signal, not an authorization.
        "safe_to_retire_shared": all_adopted,
        "peers": peers_out,
    }


# =====================================================================
# CROSS-VERTEX OBSERVER PRIMITIVE (RFC v1 §2)
# =====================================================================
# Cross-vertex observation of peer state: lets one node publish its view of
# another node's health/liveness, joined for federation-time consensus.
#
# Three endpoints. Observer-perspective publish (POST), observer-
# perspective read (GET observations), observed-perspective join read
# (GET observed-by). The third is the federation-relevant cross-vertex
# consensus surface — what does the mesh COLLECTIVELY think about peer X?


class PeerObservationItem(BaseModel):
    """One observer's view of one observed peer at a point in time.

    Wire shape — what observers publish. ``observer`` is implied by the
    URL path on POST so it isn't carried in the item itself (avoids
    server-side mismatch checks against the URL).
    """

    observed: str = Field(..., min_length=1, max_length=64)
    last_seen_at: str = Field(..., min_length=1)  # ISO-8601, gateway parses
    last_seen_kind: str
    inferred_peer_health: str
    inferred_observer_health: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    observation_basis: Optional[str] = None
    expires_at: Optional[str] = None


class PublishObservationsRequest(BaseModel):
    """Wire shape for POST /peers/{name}/observations.

    Observer publishes a batch of its peer-observation rows. Storage is
    full-replacement per (observer, observed) — observers maintain the
    rolling view, the gateway is a passive store.
    """

    observations: list[PeerObservationItem] = Field(default_factory=list)


def _validate_observation_enums(item: PeerObservationItem) -> None:
    if item.last_seen_kind not in VALID_LAST_SEEN_KINDS:
        raise HTTPException(
            422,
            f"invalid last_seen_kind {item.last_seen_kind!r}; "
            f"valid: {sorted(VALID_LAST_SEEN_KINDS)}",
        )
    if item.inferred_peer_health not in VALID_PEER_HEALTH:
        raise HTTPException(
            422,
            f"invalid inferred_peer_health {item.inferred_peer_health!r}; "
            f"valid: {sorted(VALID_PEER_HEALTH)}",
        )
    if item.inferred_observer_health not in VALID_OBSERVER_HEALTH:
        raise HTTPException(
            422,
            f"invalid inferred_observer_health "
            f"{item.inferred_observer_health!r}; "
            f"valid: {sorted(VALID_OBSERVER_HEALTH)}",
        )


@app.post("/peers/{name}/observations")
async def peer_observations_publish(
    name: str,
    req: PublishObservationsRequest,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Observer publishes its current view of peer states.

    The path-name `{name}` is the OBSERVER; each ``observations[].observed``
    is who they're observing. Full-replacement upsert per (observer,
    observed) — observers maintain the rolling view, gateway just stores.

    Returns the count of rows upserted. Peer registration of the OBSERVER
    is required (404 if unregistered); observed peers do NOT need to be
    registered (the whole point is observing peers that may be transient
    / federated / partially-visible).
    """
    auth = _authorize(authorization)
    # Source: the OBSERVER is the path-param {name} (a body-field binder would
    # structurally miss it — drop ultra #1669). The caller must be the observer
    # it claims to publish as.
    _check_caller_binding(auth, name, field="observer_name_path", site="peer_observations_publish")
    now = _utcnow_iso()
    with _conn() as c:
        r = c.execute(
            "SELECT name FROM claude_peers WHERE name = ?", (name,)
        ).fetchone()
        if not r:
            raise HTTPException(404, f"observer peer {name!r} not registered")
        n = 0
        for item in req.observations:
            _validate_observation_enums(item)
            c.execute(
                """INSERT INTO peer_observations
                     (observer, observed, last_seen_at, last_seen_kind,
                      inferred_peer_health, inferred_observer_health,
                      confidence, observation_basis, expires_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(observer, observed) DO UPDATE SET
                     last_seen_at = excluded.last_seen_at,
                     last_seen_kind = excluded.last_seen_kind,
                     inferred_peer_health = excluded.inferred_peer_health,
                     inferred_observer_health = excluded.inferred_observer_health,
                     confidence = excluded.confidence,
                     observation_basis = excluded.observation_basis,
                     expires_at = excluded.expires_at,
                     updated_at = excluded.updated_at""",
                (
                    name,
                    item.observed,
                    item.last_seen_at,
                    item.last_seen_kind,
                    item.inferred_peer_health,
                    item.inferred_observer_health,
                    item.confidence,
                    item.observation_basis,
                    item.expires_at,
                    now,
                ),
            )
            n += 1
    log.info("peer_observations: %s upserted %d row(s)", name, n)
    return {"status": "stored", "observer": name, "n_upserted": n}


@app.get("/peers/{name}/observations")
async def peer_observations_list(
    name: str,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Read what peer `{name}` observes about its peers (observer view).

    Returns all rows where observer = {name}. Used by operators + watchdog
    consumers to inspect a single peer's view of the world. Does NOT join
    across observers — see /peers/{name}/observed-by for the consensus
    surface.
    """
    _authorize(authorization)
    with _conn() as c:
        r = c.execute(
            "SELECT name FROM claude_peers WHERE name = ?", (name,)
        ).fetchone()
        if not r:
            raise HTTPException(404, f"peer {name!r} not registered")
        rows = c.execute(
            """SELECT observed, last_seen_at, last_seen_kind,
                      inferred_peer_health, inferred_observer_health,
                      confidence, observation_basis, expires_at, updated_at
               FROM peer_observations
               WHERE observer = ?
               ORDER BY observed""",
            (name,),
        ).fetchall()
    return {"observer": name, "observations": [dict(row) for row in rows]}


@app.get("/peers/{name}/observed-by")
async def peer_observations_observed_by(
    name: str,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Cross-vertex consensus view — what OTHER peers observe about `{name}`.

    Returns all rows where observed = {name}, grouped by observer. The
    response includes a ``consensus`` block summarizing agreement +
    dissent across observers. This is the federation-relevant surface —
    high observer-agreement means high confidence for §1.20 handshake-
    time AI² merge-decision inputs.

    Observed peer does NOT need to be registered — this endpoint
    supports observing peers that are transient / federated / no longer
    in the local registry.

    The consensus shape:
        - consensus_peer_health: most-common state across observers
          (ties broken alphabetically — deterministic, observable)
        - peer_health_distribution: full count by state
        - observer_count: how many distinct observers contributed
        - dissent_count: how many observers reported a state DIFFERENT
          from the consensus state
        - max_confidence: highest confidence across rows
    """
    _authorize(authorization)
    with _conn() as c:
        rows = c.execute(
            """SELECT observer, last_seen_at, last_seen_kind,
                      inferred_peer_health, inferred_observer_health,
                      confidence, observation_basis, expires_at, updated_at
               FROM peer_observations
               WHERE observed = ?
               ORDER BY observer""",
            (name,),
        ).fetchall()
    observations = [dict(row) for row in rows]
    consensus = _compute_observation_consensus(observations)
    return {
        "observed": name,
        "observations": observations,
        "consensus": consensus,
    }


def _compute_observation_consensus(observations: list[dict]) -> dict:
    """Summarize peer-health agreement across observers.

    v0.7.x — simple-majority consensus. v0.8+ may add confidence-weighting
    per RFC §5 O3. Ties broken alphabetically so the result is
    deterministic + observable. When zero observers contributed, returns
    a sentinel-shape with consensus_peer_health=None.
    """
    if not observations:
        return {
            "consensus_peer_health": None,
            "peer_health_distribution": {},
            "observer_count": 0,
            "dissent_count": 0,
            "max_confidence": None,
        }
    from collections import Counter
    counts = Counter(o["inferred_peer_health"] for o in observations)
    top_count = max(counts.values())
    top_states = sorted(s for s, c in counts.items() if c == top_count)
    consensus_state = top_states[0]
    return {
        "consensus_peer_health": consensus_state,
        "peer_health_distribution": dict(counts),
        "observer_count": len(observations),
        "dissent_count": len(observations) - counts[consensus_state],
        "max_confidence": max(o["confidence"] for o in observations),
    }


def _peer_capabilities(name: str) -> dict:
    """Fetch a peer's advertised capabilities by name. Returns {} if not registered."""
    with _conn() as c:
        r = c.execute(
            "SELECT capabilities FROM claude_peers WHERE name = ?", (name,)
        ).fetchone()
    if not r:
        return {}
    try:
        return json.loads(r["capabilities"])
    except Exception:
        return {}


def _peer_is_ratified(peer_name: str) -> bool:
    """True iff peer exists and ratified=1. Phase 5.5 per PLAN.md §15.6 #7
    server-side gate. Unregistered peers return False (not the historical
    "permissive default" of _peer_can_claim_tasks) — pre-Phase-5.5 grandfather
    backfill ratified all 8 existing peers, so any unregistered claimer
    post-Phase-5.5 is by definition unratified.

    INTENTIONAL TIGHTENING (drop PR #11 review carry-forward (a) DM #728):
    do NOT "fix" this back to the permissive ``True if not registered``
    default of _peer_can_claim_tasks. The whole point of the §15
    contract phase is that newcomers must demonstrate active understanding
    BEFORE getting any privilege; permissive-on-unregistered would defeat
    the structural enforcement. A registration-race window where a peer
    POSTs /peers then claims before the row commit will see a 403 rather
    than a soft-permit; that's the right side of the trade.
    """
    with _conn() as c:
        r = c.execute(
            "SELECT ratified FROM claude_peers WHERE name = ?", (peer_name,)
        ).fetchone()
    return bool(r["ratified"]) if r else False


def _peer_can_claim_tasks(peer_name: str) -> bool:
    """True if the peer is permitted to claim tasks from /tasks/claim.

    Phase 5.5 (PLAN.md §15.6 #7): defense-in-depth gate ANDs ratification
    on top of the existing capability check. Both must hold:

    1. Ratification gate: peer must exist AND be ratified=1. The
       grandfather backfill at PR-A migration set ratified=1 on all
       8 pre-existing peers; new peers default ratified=0 until a
       witness flips them via PATCH /peers/{name}.

    2. Capability gate (v0.1.1, unchanged): peer's advertised capabilities
       must NOT explicitly set `can_claim_tasks: false`. Lets nodes opt
       out of claim-routing (e.g., a node running a diagnostic-mode cell)
       independently of ratification.

    Pre-Phase-5.5 callers see no behavior change because of the grandfather
    backfill. Post-Phase-5.5 newcomers are blocked at the gateway until
    the §15 contract phase completes.
    """
    if not _peer_is_ratified(peer_name):
        return False
    caps = _peer_capabilities(peer_name)
    if not caps:
        # Backward-compat carve-out from the v0.1.1 capability gate is
        # now load-bearing-on-ratification: if a peer is ratified but
        # lacks an advertised capability snapshot (e.g., older claude-service),
        # ratification alone qualifies them.
        return True
    return caps.get("can_claim_tasks") is not False


# =====================================================================
# THREADS — UUID ↔ name mapping
# =====================================================================

class ThreadCreateRequest(BaseModel):
    thread_name: str = Field(..., min_length=1, max_length=128,
                              description="e.g. 'lab↔gpu-wsl:phase-7a-shadow'")
    session_id: Optional[str] = Field(None, description="Peer-local session ID (e.g. 'e906aaa3')")


@app.post("/threads")
async def thread_create(req: ThreadCreateRequest,
                         authorization: Optional[str] = Header(None)) -> dict:
    """Mint (or fetch) the UUID for a readable thread_name.

    Idempotent: same thread_name → same UUID across calls. Caller passes the
    UUID downstream to claude-service /chat as session_id.
    """
    _authorize(authorization)
    with _conn() as c:
        existing = c.execute(
            "SELECT thread_uuid, thread_name, peer_pair, topic, session_id, created_at, last_used_at "
            "FROM claude_threads WHERE thread_name = ?", (req.thread_name,)
        ).fetchone()
        if existing:
            # Update session_id if provided and different
            if req.session_id and existing["session_id"] != req.session_id:
                c.execute("UPDATE claude_threads SET session_id = ?, last_used_at = ? WHERE thread_uuid = ?",
                          (req.session_id, _utcnow_iso(), existing["thread_uuid"]))
            else:
                c.execute("UPDATE claude_threads SET last_used_at = ? WHERE thread_uuid = ?",
                          (_utcnow_iso(), existing["thread_uuid"]))
            
            # Refetch to return updated state
            updated = c.execute(
                "SELECT * FROM claude_threads WHERE thread_uuid = ?", (existing["thread_uuid"],)
            ).fetchone()
            return {**dict(updated), "created": False}

        # parse thread_name into peer_pair + topic
        # convention: '<requestor>↔<responder>:<kebab-topic>'
        peer_pair, topic = _parse_thread_name(req.thread_name)
        new_uuid = str(uuid.uuid4())
        now = _utcnow_iso()
        c.execute(
            "INSERT INTO claude_threads (thread_uuid, thread_name, peer_pair, topic, session_id, "
            "created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_uuid, req.thread_name, peer_pair, topic, req.session_id, now, now),
        )
    log.info("thread minted: %s = %s", req.thread_name, new_uuid)
    return {"thread_uuid": new_uuid, "thread_name": req.thread_name,
            "peer_pair": peer_pair, "topic": topic, "session_id": req.session_id,
            "created_at": now, "last_used_at": now, "created": True}


def _parse_thread_name(name: str) -> tuple[str, str]:
    """Split 'lab↔gpu-wsl:phase-7a-shadow' → ('lab↔gpu-wsl', 'phase-7a-shadow').

    Falls back to (name, '') if no ':' separator. Tolerant of various arrows
    (`↔`, `<->`, `--`) — anything before the first ':' is the peer_pair.
    """
    if ":" not in name:
        return (name, "")
    pair, _, topic = name.partition(":")
    return (pair.strip(), topic.strip())


@app.get("/sessions")
async def sessions_list(authorization: Optional[str] = Header(None),
                         limit: int = Query(50, ge=1, le=100)) -> dict:
    """List all unique sessions with aggregate metadata."""
    _authorize(authorization)
    with _conn() as c:
        rows = c.execute("""
            SELECT 
                session_id, 
                MIN(created_at) as first_seen_at, 
                MAX(last_used_at) as last_active_at,
                COUNT(thread_uuid) as thread_count
            FROM claude_threads
            WHERE session_id IS NOT NULL
            GROUP BY session_id
            ORDER BY last_active_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return {"sessions": [dict(r) for r in rows]}


@app.get("/sessions/{session_id}")
async def session_show(session_id: str,
                        authorization: Optional[str] = Header(None),
                        limit: int = Query(50, ge=1, le=100)) -> dict:
    """Show details for a specific session, including its threads."""
    _authorize(authorization)
    with _conn() as c:
        # Aggregate stats for the session
        stats = c.execute("""
            SELECT 
                session_id, 
                MIN(created_at) as first_seen_at, 
                MAX(last_used_at) as last_active_at,
                COUNT(thread_uuid) as thread_count
            FROM claude_threads
            WHERE session_id = ?
        """, (session_id,)).fetchone()
        
        if not stats or stats["session_id"] is None:
             raise HTTPException(404, "session not found")
             
        # List threads in this session
        threads = c.execute(
            "SELECT * FROM claude_threads WHERE session_id = ? ORDER BY last_used_at DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
        
    return {
        "session": dict(stats),
        "threads": [dict(r) for r in threads]
    }


@app.get("/threads")
async def threads_list(authorization: Optional[str] = Header(None),
                        peer: Optional[str] = Query(None),
                        topic: Optional[str] = Query(None),
                        session_id: Optional[str] = Query(None),
                        limit: int = Query(50, ge=1, le=100)) -> dict:
    """List all threads with optional filtering."""
    _authorize(authorization)
    where_clauses = []
    params = []
    if peer:
        where_clauses.append("peer_pair LIKE ?")
        params.append(f"%{peer}%")
    if topic:
        where_clauses.append("topic LIKE ?")
        params.append(f"%{topic}%")
    if session_id:
        where_clauses.append("session_id = ?")
        params.append(session_id)
    
    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM claude_threads {where} ORDER BY last_used_at DESC LIMIT ?",
            (*params, limit)
        ).fetchall()
    return {"threads": [dict(r) for r in rows]}


@app.get("/threads/{thread_uuid}")
async def thread_show(thread_uuid: str,
                       authorization: Optional[str] = Header(None),
                       limit: int = Query(100, ge=1, le=1000)) -> dict:
    _authorize(authorization)
    with _conn() as c:
        meta = c.execute(
            "SELECT * FROM claude_threads WHERE thread_uuid = ?", (thread_uuid,)
        ).fetchone()
        if not meta:
            raise HTTPException(404, "thread not found")
        # `AND channel IS NULL`: the thread view is a DM-thread view. Channel posts
        # can carry a thread_id (#45 made them thread-taggable), but they are
        # readable ONLY via the membership-gated `?channel=` path — never through
        # this _authorize-only thread endpoint (drop seat-A #45: same read-scope
        # invariant as message_list; this completes it across the gateway, the only
        # other content-returning path).
        msgs = c.execute(
            "SELECT id, from_node, to_node, kind, content, related_task_id, created_at "
            "FROM claude_messages WHERE thread_id = ? AND channel IS NULL "
            "ORDER BY created_at ASC LIMIT ?",
            (thread_uuid, limit),
        ).fetchall()
    return {
        "thread": dict(meta),
        "messages": [dict(m) for m in msgs],
    }


# =====================================================================
# MESSAGES — chat tier
# =====================================================================

class MessagePostRequest(BaseModel):
    from_node: str = Field(..., min_length=1, max_length=64)
    # EXACTLY ONE of {to_node, channel} is required (validated in the handler, 400
    # if both/neither). to_node = a DM (today's path, byte-identical). channel = a
    # channel post (to_node is server-set to CHANNEL_POST_SENTINEL).
    to_node: Optional[str] = Field(None, min_length=1, max_length=64)
    channel: Optional[str] = Field(None, min_length=1, max_length=64,
                                   description="channel post target; mutually "
                                   "exclusive with to_node")
    kind: str = Field(..., description="status|question|answer|unblock|fyi")
    content: str = Field(..., min_length=1, max_length=200_000)
    thread_id: Optional[str] = Field(None, description="UUID; nullable for ad-hoc DMs")
    related_task_id: Optional[int] = None
    # B2: a client MAY send a `mentions` array but the server DISCARDS it and
    # re-derives the mention set from `content` (so a poster can't @-wake a peer by
    # asserting a mention the content doesn't contain). Accepted-and-ignored, not
    # rejected, so existing/future clients that send it don't 422.
    mentions: Optional[list] = Field(None, description="IGNORED — server re-derives "
                                     "from content (B2). Accepted for compat.")


@app.post("/messages")
async def message_post(req: MessagePostRequest,
                        authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    # Caller-binding on from_node stays as before. ASPIRATIONAL-UNTIL-C5: with the
    # shared token (today's 100% of traffic) auth.peer is None → this is a no-op,
    # exactly like the channels create/join gates. Real closure is C5 (per-peer
    # tokens the norm). The B1 membership gate below is wired the same way.
    _check_caller_binding(auth, req.from_node, field="from_node", site="message_post")
    if req.kind not in VALID_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(VALID_KINDS)}")
    # EXACTLY ONE of {to_node, channel}. The `channel` column is the SOLE
    # discriminator between a DM and a channel post (see CHANNEL_POST_SENTINEL).
    if bool(req.to_node) == bool(req.channel):
        raise HTTPException(400, "exactly one of {to_node, channel} is required")

    if req.channel is not None:
        return _message_post_channel(req, auth)

    # ── DM path — BYTE-IDENTICAL to pre-channel behavior ─────────────────────
    now = _utcnow_iso()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO claude_messages (thread_id, from_node, to_node, kind, content, "
            "related_task_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (req.thread_id, req.from_node, req.to_node, req.kind,
             req.content, req.related_task_id, now),
        )
        msg_id = cur.lastrowid
        # Bump from_node's last_seen — proves the peer was active enough to
        # write a DM at this timestamp. No-op for unknown peers (the UPDATE
        # filter never matches). Pre-existing field on claude_peers was only
        # updated at register/health time, leaving it stale despite continuous
        # activity. Without this bump, last_seen can read stale despite a peer
        # writing many DMs since its last register/health call.
        c.execute("UPDATE claude_peers SET last_seen = ? WHERE name = ?",
                  (now, req.from_node))
    log.info("message id=%d %s→%s kind=%s len=%d",
             msg_id, req.from_node, req.to_node, req.kind, len(req.content))
    return {"id": msg_id, "from_node": req.from_node, "to_node": req.to_node,
            "kind": req.kind, "thread_id": req.thread_id, "created_at": now}


def _message_post_channel(req: MessagePostRequest, auth: AuthContext) -> dict:
    """Channel-post path. INTERIM design (see CHANNEL_POST_SENTINEL):
      - channel must exist (404 otherwise).
      - B1 membership gate: from_node MUST be a member (403 otherwise). [The
        caller-binding on from_node is already applied in message_post —
        ASPIRATIONAL-UNTIL-C5, like the channels gates.]
      - B2: derive @mentions SERVER-SIDE from content (client array discarded);
        non-member mentions are inert; NFC-normalized; capped at _MENTIONS_CAP.
      - INSERT with to_node=CHANNEL_POST_SENTINEL, channel=<channel>. The `channel`
        column is the sole discriminator; the sentinel is a non-resolvable
        placeholder (never used for routing/recipient/wake logic).
    """
    now = _utcnow_iso()
    with _conn() as c:
        if c.execute("SELECT 1 FROM channels WHERE name = ?",
                     (req.channel,)).fetchone() is None:
            raise HTTPException(404, f"channel {req.channel!r} not found")
        # B1 membership-write gate: only a member may post.
        if c.execute(
                "SELECT 1 FROM channel_members WHERE channel = ? AND peer = ?",
                (req.channel, req.from_node)).fetchone() is None:
            raise HTTPException(
                403, f"{req.from_node!r} is not a member of channel {req.channel!r}")
        # B2: server-derive mentions; client `mentions` is discarded. Validate each
        # against channel membership (a mention of a non-member is inert/dropped).
        members = {r["peer"] for r in c.execute(
            "SELECT peer FROM channel_members WHERE channel = ?",
            (req.channel,)).fetchall()}
        mentions = _derive_channel_mentions(req.content, members)
        cur = c.execute(
            "INSERT INTO claude_messages (thread_id, from_node, to_node, channel, "
            "kind, content, mentions, related_task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req.thread_id, req.from_node, CHANNEL_POST_SENTINEL, req.channel,
             req.kind, req.content, json.dumps(mentions), req.related_task_id, now),
        )
        msg_id = cur.lastrowid
        c.execute("UPDATE claude_peers SET last_seen = ? WHERE name = ?",
                  (now, req.from_node))
    log.info("channel-post id=%d %s→#%s kind=%s len=%d mentions=%d",
             msg_id, req.from_node, req.channel, req.kind, len(req.content),
             len(mentions))
    return {"id": msg_id, "from_node": req.from_node, "channel": req.channel,
            "kind": req.kind, "mentions": mentions, "created_at": now}


@app.get("/messages")
async def message_list(authorization: Optional[str] = Header(None),
                        to: Optional[str] = Query(None),
                        to_node: Optional[str] = Query(None,
                            description="Alias for `to`. The POST body field is "
                            "`to_node`, so peers naturally query ?to_node=; FastAPI "
                            "silently ignores unknown params, so a ?to_node= query "
                            "would otherwise return the UNFILTERED all-peers firehose "
                            "instead of one inbox — a silent monitor-blindness bug. "
                            "Accept it as an alias."),
                        from_: Optional[str] = Query(None, alias="from"),
                        from_node: Optional[str] = Query(None,
                            description="Alias for `from` (same to_node-mismatch class)."),
                        channel: Optional[str] = Query(None,
                            description="if set, return this channel's posts (member-"
                            "or-operator gated). Additive: does NOT affect DM queries."),
                        thread_id: Optional[str] = Query(None),
                        kind: Optional[str] = Query(None),
                        unread_only: bool = Query(False),
                        since: Optional[str] = Query(None,
                            description="ISO timestamp; messages strictly after"),
                        limit: int = Query(100, ge=1, le=1000)) -> dict:
    auth = _authorize(authorization)
    # Coalesce the body-field-name aliases onto the canonical filter vars so a
    # ?to_node= / ?from_node= query filters identically to ?to= / ?from=.
    to = to or to_node
    from_ = from_ or from_node
    clauses, params = [], []
    if channel:
        # CHANNEL READ (subscriber path). GATE: operator (shared token) OR a member
        # of the channel — else 403. ASPIRATIONAL-UNTIL-C5: today most traffic is
        # the shared token → _is_operator(auth) is True → reads any channel; the
        # per-peer membership check is the real read-scope once per-peer tokens are
        # the norm (mirrors the channels members-list read-scope posture).
        with _conn() as c:
            if c.execute("SELECT 1 FROM channels WHERE name = ?",
                         (channel,)).fetchone() is None:
                raise HTTPException(404, f"channel {channel!r} not found")
            if not _is_operator(auth):
                if c.execute(
                        "SELECT 1 FROM channel_members WHERE channel = ? AND peer = ?",
                        (channel, auth.peer)).fetchone() is None:
                    raise HTTPException(
                        403, f"not a member of channel {channel!r}")
        clauses.append("channel = ?");                     params.append(channel)
    else:
        # Channel posts are readable ONLY through the membership-gated `?channel=`
        # path above. Every OTHER read (generic firehose, ?to=, ?from=, incl.
        # ?to=<sentinel>) is restricted to DMs — channel posts never leak into an
        # unscoped/peer-inbox query (drop seat-A #45: the read-scope gate must not
        # be bypassable via the to/from/generic branch). DM reads are channel-NULL
        # by construction, so this is behavior-neutral for DMs.
        clauses.append("channel IS NULL")
    if to:           clauses.append("to_node = ?");        params.append(to)
    if from_:        clauses.append("from_node = ?");      params.append(from_)
    if thread_id:    clauses.append("thread_id = ?");      params.append(thread_id)
    if kind:         clauses.append("kind = ?");           params.append(kind)
    if unread_only:  clauses.append("read_at IS NULL")
    if since:        clauses.append("created_at > ?");     params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _conn() as c:
        rows = c.execute(
            f"SELECT id, thread_id, from_node, to_node, channel, mentions, kind, "
            f"content, related_task_id, created_at, read_at "
            f"FROM claude_messages {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        # Bump last_seen for the peer polling their inbox — `to=X` filter is
        # the canonical drain shape, so X is observably active here. Skip when
        # `to` is absent (generic / cross-peer queries don't identify a single
        # active peer). No-op for unknown peers.
        if to:
            c.execute("UPDATE claude_peers SET last_seen = ? WHERE name = ?",
                      (_utcnow_iso(), to))
    return {"messages": [dict(r) for r in rows], "n": len(rows)}


@app.post("/messages/{msg_id}/read")
async def message_mark_read(msg_id: int,
                             authorization: Optional[str] = Header(None)) -> dict:
    """Mark a message as read. Caller is on the honor system about which
    messages it actually consumed (4-node mesh, low trust requirement)."""
    _authorize(authorization)
    now = _utcnow_iso()
    with _conn() as c:
        cur = c.execute(
            "UPDATE claude_messages SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (now, msg_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "message not found or already read")
    return {"id": msg_id, "read_at": now}


# =====================================================================
# CHANNELS — pub/sub membership (mesh-channels B1 membership-write + B5
# create-auth). THIS layer is lifecycle only (create/join/leave/list/members);
# it opens NO wake surface — posting to a channel + the @mention/wake machinery
# (B2/B3/B4) land in later steps.
# =====================================================================

VALID_CHANNEL_KINDS = frozenset({"announce", "topic", "group"})
VALID_CHANNEL_VISIBILITY = frozenset({"open", "invite"})
VALID_WAKE_POLICIES = frozenset(
    {"mentions_only", "here_and_mentions", "all", "muted"})
# Pre-seeded canonical names (seeded for real at build step 7). Reserved so a
# hostile cell can't pre-register #announcements (name is PK) and own the
# canonical broadcast channel — B5 anti-squat. CREATE of a reserved name is
# operator-only (the operator seeds them); a non-operator create → 403.
RESERVED_CHANNEL_NAMES = frozenset({"announcements", "grok", "rho", "watchtower"})
_CHANNEL_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")

# Canonical mesh peer-name regex — MIRRORS swarph_shared.peer_registry.
# NAMING_CONVENTION_REGEX (the single source of truth; not importable here because
# swarph_shared is not a gateway runtime dependency). kebab-case, must start with a
# letter and end with a letter/digit. Used to PROVE the channel-post sentinel below
# is structurally un-resolvable as a peer. Keep in lockstep with swarph_shared.
PEER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")

# CHANNEL-POST to_node SENTINEL — drop-endorsed INTERIM stopgap (NOT the rejected
# channel-name-in-to_node convention). claude_messages.to_node is NOT NULL, but a
# channel post has no single recipient. Rather than recreate the table to drop the
# NOT NULL (the §2 logical model: channel posts at to_node NULL), this increment is
# ADDITIVE: a channel post INSERTs this RESERVED sentinel as to_node. It is OUTSIDE
# PEER_NAME_RE (leading '__') → no valid or FUTURE peer can ever be named it → it is
# structurally un-resolvable as a peer. That gives it the SAME fail-safe property as
# NULL: no ?to=<peer> filter, peer registry lookup, or caller-binding check can EVER
# match it, so channel posts can never pollute a peer's DM inbox/unread scan.
#   - the `channel` column is the SOLE discriminator: channel IS NOT NULL ⟺ channel
#     post. to_node=SENTINEL is a non-resolvable PLACEHOLDER, never used for routing,
#     recipient resolution, or wake logic.
#   - CONDITION 1 (locked by test_cond1_sentinel_outside_peer_name_re):
#     `not PEER_NAME_RE.fullmatch(CHANNEL_POST_SENTINEL)` — the fail-safe property.
#   - CONDITION 4 (DEFERRED clean-up): the §2 to_node-NULL table-recreate is the real
#     fix; it is OFF the Sunday clock and gets its OWN drop seat-A. This sentinel is
#     the interim that ships the post-path now without a destructive migration.
CHANNEL_POST_SENTINEL = "__channel__"

# Channel-post @mention parsing (B2): handles are kebab-case peer names following an
# '@'. Cap the derived set so a hostile post can't store an unbounded mentions array.
_MENTION_RE = re.compile(r"(?<![\w@])@([a-z][a-z0-9-]{0,62}[a-z0-9])")
_MENTIONS_CAP = 20


def _derive_channel_mentions(content: str, members: set) -> list:
    """B2: derive the mention set SERVER-SIDE from `content` for a channel post.

    Any client-supplied `mentions` array is DISCARDED by the caller; this is the
    sole source of truth. NFC-normalize the content first (so a composed/decomposed
    lookalike can't smuggle a different handle than it renders), parse `@handle`
    tokens, then KEEP only handles that are members of the channel (a mention of a
    non-member is inert/dropped — it can't be used to wake a non-member later).
    De-dup preserving first-seen order; cap at _MENTIONS_CAP so a hostile post can't
    store an unbounded array. Returns a JSON-serializable list (possibly empty)."""
    normalized = unicodedata.normalize("NFC", content)
    out, seen = [], set()
    for m in _MENTION_RE.finditer(normalized):
        handle = m.group(1)
        if handle in members and handle not in seen:
            seen.add(handle)
            out.append(handle)
            if len(out) >= _MENTIONS_CAP:
                break
    return out


def _is_operator(auth: AuthContext) -> bool:
    """Operator = a shared/commander token (no bound peer identity). Per-peer
    tokens are cells (non-operator). Mirrors the caller-binding None-semantics:
    a shared token is unbound/admin. B5 create-auth gates announce + reserved
    names to operators only.

    ASPIRATIONAL-UNTIL-C5 (drop seat-A, PR #43) — read this before trusting the
    gate in production. Today 100% of live traffic uses the SHARED token, so a
    normally-authenticating cell gets auth.peer=None → is treated as OPERATOR
    here. So B5 create-auth (announce/reserved → operator-only) is BYPASSABLE by
    any cell right now, exactly like the C2 caller-binding no-op documented at
    the MESH_CALLER_BINDING_ENFORCE block. The 403 is correctly WIRED (auth.peer
    is strictly token-derived, never from a body/header — server.py:586/627), so
    it closes the moment per-peer tokens are the norm and the shared token is
    retired (C5). Until C5 this is a trusted-sandbox gate, NOT a production
    boundary — do NOT read "operator-gated" as "closed in production." The
    membership/broadcast authority of the FUTURE POST surface is likewise REAL
    only post-C5 (a cell can already create an announce channel + own
    allow_broadcast=1 on it). Mitigation now: pre-seed the reserved names via the
    operator at deploy so the PK-409 protects the canonical names meanwhile."""
    return auth.peer is None


class ChannelCreateRequest(BaseModel):
    name: str = Field(..., description="1-64 chars of [a-z0-9-_]")
    kind: str = Field(..., description="announce|topic|group")
    visibility: Optional[str] = Field(None, description="open|invite (default open)")
    description: Optional[str] = Field(None, max_length=2000)
    created_by: str = Field(..., min_length=1, max_length=64)


class ChannelJoinRequest(BaseModel):
    peer: str = Field(..., min_length=1, max_length=64)
    wake_policy: Optional[str] = Field(
        None, description="mentions_only|here_and_mentions|all|muted")


class ChannelLeaveRequest(BaseModel):
    peer: str = Field(..., min_length=1, max_length=64)


@app.post("/channels")
async def channel_create(req: ChannelCreateRequest,
                         authorization: Optional[str] = Header(None)) -> dict:
    """Create a channel; the creator auto-joins. B5: kind='announce' OR a
    reserved name → operator-only (403 otherwise). `allow_broadcast` is seeded
    for the announce OWNER only (decoupled from created_by — topic/group owners
    get 0); never granted on a plain self-join."""
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.created_by, field="created_by",
                          site="channel_create")
    name = req.name
    if not (1 <= len(name) <= 64) or any(ch not in _CHANNEL_NAME_CHARS for ch in name):
        raise HTTPException(400, "channel name must be 1-64 chars of [a-z0-9-_]")
    if req.kind not in VALID_CHANNEL_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(VALID_CHANNEL_KINDS)}")
    visibility = req.visibility or "open"
    if visibility not in VALID_CHANNEL_VISIBILITY:
        raise HTTPException(
            400, f"visibility must be one of {sorted(VALID_CHANNEL_VISIBILITY)}")
    if (req.kind == "announce" or name in RESERVED_CHANNEL_NAMES) and not _is_operator(auth):
        raise HTTPException(
            403, "announce channels and reserved names are operator-gated (B5)")
    now = _utcnow_iso()
    owner_broadcast = 1 if req.kind == "announce" else 0
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "INSERT INTO channels (name, kind, visibility, description, "
                "created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, req.kind, visibility, req.description, req.created_by, now),
            )
            c.execute(
                "INSERT INTO channel_members (channel, peer, wake_policy, "
                "allow_broadcast, joined_at) VALUES (?, ?, 'mentions_only', ?, ?)",
                (name, req.created_by, owner_broadcast, now),
            )
            c.execute("COMMIT")
        except sqlite3.IntegrityError:
            c.execute("ROLLBACK")
            raise HTTPException(409, f"channel {name!r} already exists")
    log.info("channel created: %s kind=%s vis=%s by=%s",
             name, req.kind, visibility, req.created_by)
    return {"name": name, "kind": req.kind, "visibility": visibility,
            "description": req.description, "created_by": req.created_by,
            "created_at": now}


@app.post("/channels/{name}/join")
async def channel_join(name: str, req: ChannelJoinRequest,
                       authorization: Optional[str] = Header(None)) -> dict:
    """Self-join an OPEN channel (B1 self-membership). Invite channels are NOT
    self-joinable (H3 — owner-add only); a non-member self-join → 403. Idempotent:
    re-join updates wake_policy in place. A self-join NEVER grants broadcast."""
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.peer, field="peer", site="channel_join")
    wake_policy = req.wake_policy or "mentions_only"
    if wake_policy not in VALID_WAKE_POLICIES:
        raise HTTPException(
            400, f"wake_policy must be one of {sorted(VALID_WAKE_POLICIES)}")
    now = _utcnow_iso()
    with _conn() as c:
        ch = c.execute("SELECT visibility FROM channels WHERE name = ?",
                       (name,)).fetchone()
        if ch is None:
            raise HTTPException(404, f"channel {name!r} not found")
        if ch["visibility"] == "invite":
            already = c.execute(
                "SELECT 1 FROM channel_members WHERE channel = ? AND peer = ?",
                (name, req.peer)).fetchone()
            if not already:
                raise HTTPException(
                    403, "invite channels are not self-joinable (owner-add only, H3)")
        # Upsert: never touch allow_broadcast on self-join (preserve an
        # owner-granted value for an invite member updating policy; default 0).
        c.execute(
            "INSERT INTO channel_members (channel, peer, wake_policy, "
            "allow_broadcast, joined_at) VALUES (?, ?, ?, 0, ?) "
            "ON CONFLICT(channel, peer) DO UPDATE SET wake_policy = excluded.wake_policy",
            (name, req.peer, wake_policy, now),
        )
        row = c.execute(
            "SELECT wake_policy, allow_broadcast, joined_at FROM channel_members "
            "WHERE channel = ? AND peer = ?", (name, req.peer)).fetchone()
    return {"channel": name, "peer": req.peer, "wake_policy": row["wake_policy"],
            "allow_broadcast": row["allow_broadcast"], "joined_at": row["joined_at"]}


@app.post("/channels/{name}/leave")
async def channel_leave(name: str, req: ChannelLeaveRequest,
                        authorization: Optional[str] = Header(None)) -> dict:
    """Self-leave (idempotent — leaving a channel you're not in is a no-op)."""
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.peer, field="peer", site="channel_leave")
    with _conn() as c:
        c.execute("DELETE FROM channel_members WHERE channel = ? AND peer = ?",
                  (name, req.peer))
    return {"channel": name, "peer": req.peer, "left": True}


@app.get("/channels")
async def channel_list(authorization: Optional[str] = Header(None),
                       peer: Optional[str] = Query(
                           None, description="if set, each channel gets an "
                           "is_member flag for this peer")) -> dict:
    """List channels (+ member_count). Channel existence is enumerable by design
    (the threat model assumes it); post READ-confidentiality rides GET /messages
    (a later step), not this listing."""
    _authorize(authorization)
    with _conn() as c:
        rows = c.execute(
            "SELECT c.name, c.kind, c.visibility, c.description, c.created_by, "
            "c.created_at, "
            "(SELECT COUNT(*) FROM channel_members m WHERE m.channel = c.name) "
            "AS member_count "
            "FROM channels c ORDER BY c.name").fetchall()
        member_of = set()
        if peer:
            member_of = {r["channel"] for r in c.execute(
                "SELECT channel FROM channel_members WHERE peer = ?", (peer,)).fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        if peer:
            d["is_member"] = r["name"] in member_of
        out.append(d)
    return {"channels": out, "n": len(out)}


@app.get("/channels/{name}/members")
async def channel_member_list(name: str,
                              authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)
    with _conn() as c:
        if c.execute("SELECT 1 FROM channels WHERE name = ?", (name,)).fetchone() is None:
            raise HTTPException(404, f"channel {name!r} not found")
        rows = c.execute(
            "SELECT peer, wake_policy, allow_broadcast, joined_at "
            "FROM channel_members WHERE channel = ? ORDER BY joined_at",
            (name,)).fetchall()
    return {"channel": name, "members": [dict(r) for r in rows], "n": len(rows)}


# =====================================================================
# SCHEDULED EVENTS — automation control plane (spec §2/§3/§5)
# Registry + control surface. The orchestrator FIRES; this layer is
# registry + lifecycle + validation only. No wake logic here.
# =====================================================================

VALID_TRIGGER_TYPES = frozenset({"time", "event"})
# v1 starter predicate vocabulary (spec §5) — grows additively per kind.
VALID_PREDICATE_KINDS = frozenset(
    {"on_channel_post", "on_pr_merged", "on_event_complete", "on_peer_stale"})
# context_ref anchor kinds that are DURABLE (survive compaction). A /tmp path
# or a session-id is the opposite of durable → rejected (spec §4).
_DURABLE_ANCHOR_KEYS = frozenset({"repo", "memory", "channel", "feature", "file"})

# Injection-safe charset: reject C0 control chars minus \t \n \r (benign —
# cell_wake collapses them to a space at send), plus DEL (0x7f) and C1 (0x80-0x9f).
# Applied to task + every string value in context_ref at create-time so dangerous
# bytes never persist in the registry (seat-A security gate).
_SCHED_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _scan_ctrl(obj) -> bool:
    """True iff any string anywhere in obj (dict keys AND values, nested lists/
    dicts) contains a dangerous control char. Walks the LIVE structure — a
    json.dumps scan is INVALID (dumps escapes \\x1b to literal chars, search misses)."""
    if isinstance(obj, str):
        return bool(_SCHED_CTRL.search(obj))
    if isinstance(obj, dict):
        return any(_scan_ctrl(k) or _scan_ctrl(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_scan_ctrl(x) for x in obj)
    return False


# Co-located cell allowlist — ops sets SCHEDULER_LOCAL_CELLS="cell-a,cell-b" in
# the live service env. send-keys wake is host-local; a target on another host
# can't be reached. If empty, the check is skipped (backward-compatible, v1).
# IMPORTANT: empty = DISABLED gate — the deploy MUST set SCHEDULER_LOCAL_CELLS in
# production. The firing engine also enforces locality independently. (seat-A HIGH)
SCHEDULER_LOCAL_CELLS = frozenset(
    c.strip() for c in os.environ.get("SCHEDULER_LOCAL_CELLS", "").split(",")
    if c.strip()
)
# Sentinel so the "allowlist is DISABLED" warning fires once per process, not
# per-request (avoids log spam while staying visible to ops).
_sched_local_cells_warned: list = []  # non-empty = already warned


def _validate_cron(expr: str) -> bool:
    """Return True iff expr is a valid, exactly 5-field UTC cron expression.
    croniter.is_valid accepts 6-field (seconds-prefix) cron; the spec mandates
    5-field UTC only, so we add a whitespace-field-count guard after croniter.
    """
    # Import OUTSIDE the try so a MISSING/broken croniter surfaces as a LOUD
    # ImportError (mis-configured dep) rather than being swallowed into "every cron
    # invalid" — which silently rejects ALL schedules with zero signal pointing at the
    # dep. Drop #3265: that masking is what made 26 test failures read as a cron bug.
    from croniter import croniter
    try:
        if not croniter.is_valid(expr):
            return False
        # Spec §2 / seat-A LOW: require exactly 5 whitespace-separated fields.
        if len(expr.split()) != 5:
            return False
        return True
    except Exception:
        return False


def _anchor_is_durable(anchor: dict) -> bool:
    """True iff the anchor names a recurring artifact. Rejects /tmp + session-ids
    on a `file` anchor (the non-recurring things that vanish on compaction).
    A non-string `file` value (e.g. an int) is treated as invalid → returns False
    so the caller raises 400 rather than crashing with AttributeError (seat-A LOW)."""
    if not isinstance(anchor, dict) or not (set(anchor) & _DURABLE_ANCHOR_KEYS):
        return False
    f = anchor.get("file")
    if f is not None:
        if not isinstance(f, str):
            # Non-string file values can't be validated — treat as invalid anchor.
            return False
        if f.startswith("/tmp/") or f.startswith("/var/tmp/"):
            return False
        if f.startswith("session-") or "/sessions/" in f:
            return False
    return True


class ScheduledEventCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    trigger_type: str = Field(..., description="time|event")
    cron: Optional[str] = None
    predicate: Optional[dict] = None
    target_cell: str = Field(..., min_length=1, max_length=64)
    task: str = Field(..., min_length=1, max_length=8000)
    context_ref: list = Field(..., description="list of durable anchors (spec §4)")
    out_channel: Optional[str] = None
    created_by: str = Field(..., min_length=1, max_length=64)
    min_interval_sec: Optional[int] = Field(None, ge=0,
                                            description="minimum seconds between fires (optional)")


@app.post("/scheduled-events")
async def scheduled_event_create(req: ScheduledEventCreateRequest,
                                 authorization: Optional[str] = Header(None)) -> dict:
    """Create a scheduled event.

    OPERATOR-GATED (seat-A B2, pre-C5) — A scheduled fire is ACTIVE SESSION-
    INJECTION (the orchestrator sends a task payload into a live cell via tmux
    send-keys). Blast radius is HIGH — higher than a channel post, which is
    passive. The 403 is correctly wired but, like the channels gate, is a
    TRUSTED-SANDBOX gate until C5 retires the shared token. Do NOT read
    'operator-gated' as 'closed in production' until C5.
    """
    auth = _authorize(authorization)
    # Addition 3 — operator-gate (seat-A B2, active-injection, pre-C5).
    if not _is_operator(auth):
        raise HTTPException(
            403,
            "scheduled events are operator-gated (active session-injection; per-peer cells are denied)",
        )
    _check_caller_binding(auth, req.created_by, field="created_by",
                          site="scheduled_event_create")
    if req.trigger_type not in VALID_TRIGGER_TYPES:
        raise HTTPException(400, f"trigger_type must be one of {sorted(VALID_TRIGGER_TYPES)}")
    if req.trigger_type == "time":
        if not req.cron or not _validate_cron(req.cron):
            raise HTTPException(400, "time trigger requires a valid 5-field cron")
        predicate_json = None
    else:  # event
        p = req.predicate or {}
        if p.get("kind") not in VALID_PREDICATE_KINDS:
            raise HTTPException(
                400, f"event predicate kind must be one of {sorted(VALID_PREDICATE_KINDS)}")
        predicate_json = json.dumps(p)
    if not req.context_ref:
        raise HTTPException(400, "context_ref is required and must be non-empty (spec §4)")
    if not all(_anchor_is_durable(a) for a in req.context_ref):
        raise HTTPException(
            400, "every context_ref anchor must be a DURABLE artifact "
                 "(repo/memory/channel/feature/non-tmp file) — spec §4")
    # Addition 2 — injection-safe charset validation (seat-A), recursive.
    # Reject C0 minus \t\n\r, plus DEL and C1 — the dangerous control set.
    # \t\n\r are allowed (benign; cell_wake collapses them to a space at send).
    # Scan the WHOLE validated request (every field, recursively) — NOT just
    # task+context_ref. A control byte in any sibling string field (target_cell,
    # out_channel, name, created_by, predicate.args, …) must not persist or be
    # echoed raw by GET/LIST/fire-now into a consumer that renders it
    # (consumer-display injection; the persistence-guarantee at _scan_ctrl).
    # model_dump() walks every field uniformly, so a future new field is covered
    # by default — field-complete, not field-narrow (drop seat-A #44 round 3).
    # (model_dump, NOT json.dumps — dumps escapes \x1b to literal chars.)
    _MSG = "request contains a forbidden control character (injection-safe charset)"
    if _scan_ctrl(req.model_dump()):
        raise HTTPException(400, _MSG)
    # Addition 4 — host-local target reject (seat-A HIGH).
    # send-keys is host-local; if ops has set SCHEDULER_LOCAL_CELLS the target
    # must be listed. If the env is unset/empty the check is skipped (v1,
    # backward-compatible) BUT a one-time warning tells ops the gate is DISABLED.
    if not SCHEDULER_LOCAL_CELLS:
        if not _sched_local_cells_warned:
            log.warning(
                "SCHEDULER_LOCAL_CELLS is empty — host-local create-gate DISABLED. "
                "Set SCHEDULER_LOCAL_CELLS in the service env before production use."
            )
            _sched_local_cells_warned.append(True)
    elif req.target_cell not in SCHEDULER_LOCAL_CELLS:
        raise HTTPException(
            400,
            "target_cell must be host-local / co-located with the orchestrator "
            "(not reachable for a send-keys wake)",
        )
    now = _utcnow_iso()
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO scheduled_events (name, trigger_type, cron, predicate, "
                "target_cell, task, context_ref, out_channel, enabled, created_by, "
                "created_at, fire_count, min_interval_sec) "
                "VALUES (?,?,?,?,?,?,?,?,1,?,?,0,?)",
                (req.name, req.trigger_type, req.cron, predicate_json,
                 req.target_cell, req.task, json.dumps(req.context_ref),
                 req.out_channel, req.created_by, now, req.min_interval_sec),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"scheduled event {req.name!r} already exists")
    log.info("scheduled event created: %s trigger=%s target=%s by=%s",
             req.name, req.trigger_type, req.target_cell, req.created_by)
    return {"name": req.name, "trigger_type": req.trigger_type, "enabled": 1,
            "fire_count": 0, "created_at": now}


def _sched_row(c, name):
    return c.execute("SELECT * FROM scheduled_events WHERE name = ?", (name,)).fetchone()


@app.get("/scheduled-events")
async def scheduled_event_list(authorization: Optional[str] = Header(None),
                               enabled: Optional[int] = Query(None),
                               trigger_type: Optional[str] = Query(None)) -> dict:
    _authorize(authorization)
    clauses, params = [], []
    if enabled is not None:      clauses.append("enabled = ?");      params.append(enabled)
    if trigger_type:             clauses.append("trigger_type = ?"); params.append(trigger_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM scheduled_events {where} ORDER BY name", params).fetchall()
    return {"events": [dict(r) for r in rows], "n": len(rows)}


@app.get("/scheduled-events/{name}")
async def scheduled_event_get(name: str,
                              authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)
    with _conn() as c:
        row = _sched_row(c, name)
    if row is None:
        raise HTTPException(404, f"scheduled event {name!r} not found")
    return dict(row)


def _set_enabled(name: str, value: int) -> dict:
    with _conn() as c:
        cur = c.execute("UPDATE scheduled_events SET enabled = ? WHERE name = ?",
                        (value, name))
        if cur.rowcount == 0:
            raise HTTPException(404, f"scheduled event {name!r} not found")
    return {"name": name, "enabled": value}


@app.post("/scheduled-events/{name}/enable")
async def scheduled_event_enable(name: str,
                                 authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)
    return _set_enabled(name, 1)


@app.post("/scheduled-events/{name}/disable")
async def scheduled_event_disable(name: str,
                                  authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)
    return _set_enabled(name, 0)


@app.delete("/scheduled-events/{name}")
async def scheduled_event_delete(name: str,
                                 authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)
    with _conn() as c:
        cur = c.execute("DELETE FROM scheduled_events WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"scheduled event {name!r} not found")
    log.info("scheduled event deleted: %s", name)
    return {"name": name, "deleted": True}


@app.post("/scheduled-events/{name}/fire-now")
async def scheduled_event_fire_now(name: str,
                                   authorization: Optional[str] = Header(None)) -> dict:
    """Manual/on-demand fire MARK. The gateway records the fire (watermark +
    count + status); the orchestrator's tick performs the actual send-keys wake
    on its next pass (or the caller drives it). Keeps wake logic out of the
    gateway (spec §3.2/§7).

    OPERATOR-GATED (seat-A B2, pre-C5) — fire-now is active session-injection;
    blast-radius is HIGH. Gate mirrors the create gate. ASPIRATIONAL-UNTIL-C5:
    same caveat as the create endpoint — a shared-token cell bypasses today.
    """
    auth = _authorize(authorization)
    # Addition 3 — operator-gate (seat-A B2, active-injection, pre-C5).
    if not _is_operator(auth):
        raise HTTPException(
            403,
            "scheduled events are operator-gated (active session-injection; per-peer cells are denied)",
        )
    now = _utcnow_iso()
    with _conn() as c:
        row = _sched_row(c, name)
        if row is None:
            raise HTTPException(404, f"scheduled event {name!r} not found")
        c.execute(
            "UPDATE scheduled_events SET last_fired_at = ?, last_status = 'fired', "
            "fire_count = fire_count + 1 WHERE name = ?", (now, name))
    return {"name": name, "fired": True, "fired_at": now,
            "target_cell": row["target_cell"], "task": row["task"],
            "context_ref": json.loads(row["context_ref"])}


# =====================================================================
# LLM FLEET PANEL — observe + control the 4 provider lanes
# =====================================================================
# GET  /services                 — read scope (any authed caller via _authorize)
# POST /services/{lane}/action   — control; OPERATOR-GATED via _is_operator(auth),
#   the SAME gate the scheduled-events fire-now route uses (active blast-radius:
#   stop/restart disrupts a serving lane; set-model rewrites a lane .env). The
#   allowlist (services_control.LANES/ACTIONS) is the fail-closed security
#   boundary — unknown lane -> 403, unknown action -> 400 — enforced in
#   services_control before any privileged call into the fleetctl sudo wrapper.
#   ASPIRATIONAL-UNTIL-C5: same caveat as fire-now/create — a shared-token cell
#   is treated as operator today; the gate closes when per-peer tokens are the
#   norm and the shared token is retired (C5).
from . import services_control as _sc  # noqa: E402


class ServiceActionReq(BaseModel):
    action: str
    model: Optional[str] = None


@app.get("/services")
async def list_services_endpoint(authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)               # read scope OK
    # list_services() is blocking (4 lanes × systemctl + /health probes); offload
    # to a worker thread so the privileged probes never stall the asyncio loop.
    return {"services": await asyncio.to_thread(_sc.list_services)}


@app.post("/services/{lane}/action")
async def service_action_endpoint(lane: str, req: ServiceActionReq,
                                  authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    if not _is_operator(auth):              # SAME gate the fire-now control uses
        raise HTTPException(
            403, "fleet control is operator-gated (active lane disruption; per-peer cells are denied)")
    try:
        return _sc.run_action(lane, req.action, req.model, actor="commander")
    except _sc.FleetError as e:
        raise HTTPException(e.status, e.detail)


# =====================================================================
# LANES — launched LLM provider lanes (registry + scale + enqueue)
# =====================================================================
# GET  /lanes                 — read scope (any authed caller)
# POST /lanes                 — operator-gated create + scale-to-n
# POST /lanes/{name}/scale    — operator-gated
# DELETE /lanes/{name}        — operator-gated (scale-0-then-delete)
# POST /lanes/{name}/enqueue  — any authed caller; lane must exist
#
# The mutating routes reuse the SAME operator gate the /services/{lane}/action
# route uses (_is_operator(auth) → auth.peer is None). The provider + NAME_RE
# allowlists in lanes_control are the fail-closed security boundary (unknown
# provider/name/n → 4xx BEFORE any DB write or fleetctl call). LaneError → HTTP.
from . import lanes_control as _lc  # noqa: E402


class CreateLaneReq(BaseModel):
    name: str
    provider: str
    model: str
    n: int = 0
    context_text: str = ""
    context_files: list[str] = []


class ScaleReq(BaseModel):
    n: int


class SetContextReq(BaseModel):
    context_text: str = ""
    context_files: list[str] = []


class EnqueueReq(BaseModel):
    prompt: str
    context_text: str = ""
    context_files: list[str] = []


@app.get("/lanes")
async def list_lanes_endpoint(authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)               # read scope OK (any authed caller)
    return {"lanes": _lc.list_lanes()}


@app.post("/lanes")
async def create_lane_endpoint(req: CreateLaneReq,
                               authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    if not _is_operator(auth):              # SAME gate the fleet-control route uses
        raise HTTPException(
            403, "lane control is operator-gated (mints + scales workers; per-peer cells are denied)")
    try:
        # create-at-0 then scale (I2): the row is persisted at n=0, then workers
        # are brought up by scale_lane. A scale failure leaves a clean
        # n=<last-applied> lane (per scale_lane's record-what-happened), never an
        # "n=8 but 0 workers" row. Both blocking calls are offloaded off the event
        # loop (I1) so a MAX_N×30s scale loop never stalls the asyncio loop.
        lane = await asyncio.to_thread(_lc.create_lane, req.name, req.provider, req.model, 0)
        if req.context_text or req.context_files:
            await asyncio.to_thread(_lc.set_context, req.name, req.context_text, req.context_files)
        if req.n:
            lane = await asyncio.to_thread(_lc.scale_lane, req.name, req.n)
        return lane
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


@app.post("/lanes/{name}/scale")
async def scale_lane_endpoint(name: str, req: ScaleReq,
                              authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    if not _is_operator(auth):
        raise HTTPException(
            403, "lane control is operator-gated (scales workers; per-peer cells are denied)")
    try:
        return await asyncio.to_thread(_lc.scale_lane, name, req.n)  # offload blocking loop (I1)
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


@app.delete("/lanes/{name}")
async def delete_lane_endpoint(name: str,
                               authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    if not _is_operator(auth):
        raise HTTPException(
            403, "lane control is operator-gated (stops + deletes workers; per-peer cells are denied)")
    try:
        await asyncio.to_thread(_lc.scale_lane, name, 0)   # stop all its workers first (offload I1)
        await asyncio.to_thread(_lc.delete_lane, name)
        return {"ok": True}
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


@app.post("/lanes/{name}/context")
async def set_lane_context_endpoint(name: str, req: SetContextReq,
                                    authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    if not _is_operator(auth):
        raise HTTPException(403, "lane context is operator-gated (trusted-source only; per-peer cells are denied)")
    try:
        return await asyncio.to_thread(_lc.set_context, name, req.context_text, req.context_files)
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/lanes/{name}/context")
async def get_lane_context_endpoint(name: str,
                                    authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)               # read scope OK (any authed caller)
    try:
        return _lc.get_context(name)
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/lanes/{name}/context/files")
async def list_lane_context_files_endpoint(name: str,
                                           authorization: Optional[str] = Header(None)) -> dict:
    _authorize(authorization)
    try:
        return {"files": _lc.list_context_files(name)}
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


@app.post("/lanes/{name}/enqueue")
async def enqueue_lane_endpoint(name: str, req: EnqueueReq,
                                authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)        # bare-prompt enqueue: any authed caller
    # Per-job CONTEXT, however, is operator-only (same trust gate as lane context).
    if (req.context_text or req.context_files) and not _is_operator(auth):
        raise HTTPException(403, "per-job context is operator-gated (trusted-source only; per-peer cells are denied)")
    try:
        return {"job_id": _lc.enqueue(name, req.prompt,
                                      job_context_text=req.context_text,
                                      job_context_files=req.context_files)}
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


@app.get("/lanes/{name}/jobs/{job_id}")
async def get_lane_job_endpoint(name: str, job_id: int,
                                authorization: Optional[str] = Header(None)) -> dict:
    """Read a lane job back by its enqueue job_id (closes the result-delivery
    seam: the enqueuer had no way to read the worker's verdict). Any authed
    caller, mirroring enqueue. get_job does status + (done→output_path content,
    traversal-guarded / failed→error / else None); the route stays thin."""
    _authorize(authorization)
    try:
        return _lc.get_job(name, job_id)
    except _lc.LaneError as e:
        raise HTTPException(e.status, e.detail)


# =====================================================================
# TASKS — work queue
# =====================================================================

class TaskCreateRequest(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=128)
    category: str = Field(..., description="research|audit|survey|doc|debug|commander_approved")
    spec_json: dict = Field(..., description="prompt, target_node, scope, constraints")
    priority: int = Field(5, ge=0, le=10)
    cost_cap_usd: Optional[float] = Field(None, ge=0.0)
    not_before: Optional[str] = Field(None, description="ISO timestamp; gate")
    added_by: str = Field(..., min_length=1, max_length=64)
    parent_task_id: Optional[int] = None
    thread_id: Optional[str] = None


def _insert_task(category: str, spec_json: str, *, task_id: Optional[str] = None,
                 priority: int = 5, cost_cap_usd: Optional[float] = None,
                 not_before: Optional[str] = None, added_by: str = "lanes_control",
                 parent_task_id: Optional[int] = None,
                 thread_id: Optional[str] = None) -> int:
    """Shared row-insert for the generic task queue. Inserts a pending
    claude_tasks row and returns its integer pk. Used by BOTH POST /tasks and
    lanes_control._create_task (lane-tagged enqueue) — the single insert path, so
    there is exactly one task queue. spec_json is a pre-serialized JSON string.

    Raises sqlite3.IntegrityError on a duplicate task_id (the caller maps it to a
    409). task_id defaults to a generated unique id (the lane enqueue path has no
    natural task_id; POST /tasks passes the caller-supplied one)."""
    if task_id is None:
        task_id = f"task-{uuid.uuid4().hex}"
    now = _utcnow_iso()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO claude_tasks (task_id, category, status, priority, cost_cap_usd, "
            "not_before, added_by, added_at, spec_json, parent_task_id, thread_id) "
            "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, category, priority, cost_cap_usd, not_before, added_by, now,
             spec_json, parent_task_id, thread_id),
        )
        return cur.lastrowid


@app.post("/tasks")
async def task_create(req: TaskCreateRequest,
                       authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.added_by, field="added_by", site="task_create")
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(VALID_CATEGORIES)}")
    # Loop-detection: refuse if parent chain already exceeds depth 3
    if req.parent_task_id is not None:
        depth = _chain_depth(req.parent_task_id)
        if depth >= 3:
            raise HTTPException(400, f"parent_task_id chain depth {depth} exceeds limit (3) — loop suspected")

    now = _utcnow_iso()
    try:
        task_pk = _insert_task(
            req.category, json.dumps(req.spec_json), task_id=req.task_id,
            priority=req.priority, cost_cap_usd=req.cost_cap_usd,
            not_before=req.not_before, added_by=req.added_by,
            parent_task_id=req.parent_task_id, thread_id=req.thread_id)
    except sqlite3.IntegrityError as e:
        raise HTTPException(409, f"task_id {req.task_id!r} already exists") from e
    log.info("task created: id=%s task_id=%s category=%s priority=%d",
             task_pk, req.task_id, req.category, req.priority)
    return {"id": task_pk, "task_id": req.task_id, "status": "pending",
            "added_at": now}


def _chain_depth(parent_id: int, max_depth: int = 10) -> int:
    """Walk parent_task_id back to root; return chain length."""
    depth = 0
    current = parent_id
    with _conn() as c:
        while current is not None and depth < max_depth:
            r = c.execute(
                "SELECT parent_task_id FROM claude_tasks WHERE id = ?", (current,)
            ).fetchone()
            if r is None:
                break
            depth += 1
            current = r["parent_task_id"]
    return depth


@app.get("/tasks")
async def task_list(authorization: Optional[str] = Header(None),
                     status: Optional[str] = Query(None),
                     category: Optional[str] = Query(None),
                     claimed_by: Optional[str] = Query(None),
                     limit: int = Query(100, ge=1, le=1000)) -> dict:
    _authorize(authorization)
    clauses, params = [], []
    if status:      clauses.append("status = ?");      params.append(status)
    if category:    clauses.append("category = ?");    params.append(category)
    if claimed_by:  clauses.append("claimed_by = ?");  params.append(claimed_by)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM claude_tasks {where} "
            f"ORDER BY priority DESC, added_at ASC LIMIT ?",
            params,
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["spec_json"] = json.loads(d["spec_json"])
        except Exception:
            pass
        out.append(d)
    return {"tasks": out, "n": len(out)}


class TaskClaimRequest(BaseModel):
    claimed_by: str = Field(..., min_length=1, max_length=64)
    category: Optional[str] = Field(None, description="optional filter")


@app.post("/tasks/claim")
async def task_claim(req: TaskClaimRequest,
                      authorization: Optional[str] = Header(None)) -> dict:
    """Atomic claim of the next pending task.

    Race-safe: the UPDATE...RETURNING locks the row; concurrent claimers see
    different rows or get empty results. SQLite's lock ordering gives FIFO
    fairness within priority bucket.

    Capability gate (v0.1.1): if the claimer's advertised capabilities set
    `can_claim_tasks: false` (per claude-service ≥0.3.1), the request is
    refused with 403. Lets nodes opt out of claim-routing while keeping
    /messages, /tasks/add, and /peers/register paths fully open. Backward-
    compatible: peers without the flag (or missing from registry) are
    permitted as before.
    """
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.claimed_by, field="claimed_by", site="task_claim")
    if not _peer_can_claim_tasks(req.claimed_by):
        raise HTTPException(
            403,
            f"peer {req.claimed_by!r} advertises can_claim_tasks=false — "
            f"task claims refused. The peer can still emit tasks/messages."
        )
    now = _utcnow_iso()
    cat_clause = "AND category = ?" if req.category else ""
    cat_param = (req.category,) if req.category else ()

    with _conn() as c:
        # SQLite ≥3.35 supports UPDATE...RETURNING. Verify available.
        c.execute("BEGIN IMMEDIATE")  # write-lock
        try:
            row = c.execute(
                f"""SELECT id FROM claude_tasks
                    WHERE status='pending'
                      AND (not_before IS NULL OR not_before <= ?)
                      {cat_clause}
                    ORDER BY priority DESC, added_at ASC LIMIT 1""",
                (now, *cat_param),
            ).fetchone()
            if not row:
                c.execute("COMMIT")
                return {"claimed": None, "reason": "no_pending_tasks"}

            task_pk = row["id"]
            c.execute(
                "UPDATE claude_tasks SET status='in_progress', claimed_by=?, "
                "claimed_at=?, attempts=attempts+1 WHERE id=?",
                (req.claimed_by, now, task_pk),
            )
            full = c.execute(
                "SELECT * FROM claude_tasks WHERE id = ?", (task_pk,)
            ).fetchone()
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    d = dict(full)
    try:
        d["spec_json"] = json.loads(d["spec_json"])
    except Exception:
        pass
    log.info("task claimed: id=%s task_id=%s by=%s", d["id"], d["task_id"], req.claimed_by)
    return {"claimed": d}


class TaskCompleteRequest(BaseModel):
    output_path: Optional[str] = None
    cost_actual_usd: Optional[float] = Field(None, ge=0.0)


@app.post("/tasks/{task_pk}/complete")
async def task_complete(task_pk: int, req: TaskCompleteRequest,
                         authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    now = _utcnow_iso()
    # Ownership gate (drop ultra #1669 gap #3): the caller must be the worker
    # that CLAIMED this task (owner = claude_tasks.claimed_by from DB, NOT a
    # request body field — that's why this is an ownership gate, not an actor
    # binder). Else a peer could complete another's task + misattribute its
    # cost_actual_usd. Read the owner first (own conn, released), bind before the
    # mutation txn (separate-conn ordering preserved). None claimed_by → no owner
    # to bind (the UPDATE's status='in_progress' guard 404s a non-claimed task).
    with _conn() as c:
        owner_row = c.execute(
            "SELECT claimed_by FROM claude_tasks WHERE id=?", (task_pk,)
        ).fetchone()
    if owner_row is not None and owner_row["claimed_by"] is not None:
        _check_caller_binding(auth, owner_row["claimed_by"],
                              field="task_owner", site="task_complete")
    with _conn() as c:
        cur = c.execute(
            "UPDATE claude_tasks SET status='done', completed_at=?, "
            "output_path=?, cost_actual_usd=? WHERE id=? AND status='in_progress'",
            (now, req.output_path, req.cost_actual_usd, task_pk),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "task not found or not in_progress")
    log.info("task completed: id=%s output=%s cost=%s", task_pk,
             req.output_path, req.cost_actual_usd)
    return {"id": task_pk, "status": "done", "completed_at": now}


class TaskFailRequest(BaseModel):
    error: str = Field(..., min_length=1, max_length=4000)
    retryable: bool = Field(True, description="If True, status→pending; else 'failed'")


@app.post("/tasks/{task_pk}/fail")
async def task_fail(task_pk: int, req: TaskFailRequest,
                     authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)
    now = _utcnow_iso()
    # Ownership gate (drop ultra #1669 gap #3): caller must be the worker that
    # CLAIMED this task (owner = claude_tasks.claimed_by from DB). Read first,
    # bind before the mutation txn. NOTE this read must precede the UPDATE which
    # sets claimed_by=NULL — so capture the owner while it's still present.
    with _conn() as c:
        owner_row = c.execute(
            "SELECT claimed_by FROM claude_tasks WHERE id=?", (task_pk,)
        ).fetchone()
    if owner_row is not None and owner_row["claimed_by"] is not None:
        _check_caller_binding(auth, owner_row["claimed_by"],
                              field="task_owner", site="task_fail")
    new_status = "pending" if req.retryable else "failed"
    with _conn() as c:
        cur = c.execute(
            "UPDATE claude_tasks SET status=?, last_error=?, claimed_by=NULL, "
            "claimed_at=NULL WHERE id=? AND status='in_progress'",
            (new_status, req.error, task_pk),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "task not found or not in_progress")
    log.warning("task failed: id=%s retryable=%s error=%s", task_pk,
                req.retryable, req.error[:200])
    return {"id": task_pk, "status": new_status, "failed_at": now}


class TaskUnblockRequest(BaseModel):
    evidence: str = Field(..., min_length=1, max_length=2000,
                           description="Why is this task now runnable?")
    posted_by: str = Field(..., min_length=1, max_length=64)


@app.post("/tasks/{task_pk}/unblock")
async def task_unblock(task_pk: int, req: TaskUnblockRequest,
                        authorization: Optional[str] = Header(None)) -> dict:
    """Clear `not_before` gate + ensure status is pending. Drops a
    `claude_messages` row as audit trail."""
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.posted_by, field="posted_by", site="task_unblock")
    now = _utcnow_iso()
    with _conn() as c:
        r = c.execute(
            "SELECT task_id, status, added_by FROM claude_tasks WHERE id=?", (task_pk,)
        ).fetchone()
        if not r:
            raise HTTPException(404, "task not found")
        if r["status"] not in ("pending", "stale"):
            raise HTTPException(409, f"cannot unblock task in status {r['status']!r}")
        c.execute(
            "UPDATE claude_tasks SET not_before=NULL, status='pending' WHERE id=?",
            (task_pk,),
        )
        # Audit log
        c.execute(
            "INSERT INTO claude_messages (from_node, to_node, kind, content, "
            "related_task_id, created_at) VALUES (?, ?, 'unblock', ?, ?, ?)",
            (req.posted_by, r["added_by"], req.evidence, task_pk, now),
        )
    log.info("task unblocked: id=%s by=%s evidence=%s",
             task_pk, req.posted_by, req.evidence[:100])
    return {"id": task_pk, "status": "pending", "unblocked_at": now}


# =====================================================================
# PEER HEALTH EVENTS — 6th-state observability (RFC PR #159 + Path B)
# =====================================================================
# Cross-vertex aggregation of peer-health events that
# don't fire Stop/StopFailure/PreCompact/PostCompact hooks (the
# 6th-state class — rate_limit_429 / classifier_unavailable_bash_denied
# / usage_limit_reset). Replaces an earlier flat-file log with a queryable
# substrate primitive.
#
# event_type is INTENTIONALLY un-constrained — RFC §1 designed for free
# enumeration extension (new event types slot in without schema change).
# Validation lives at the application layer per VALID_PEER_HEALTH_EVENT
# TYPES (kept for documentation; not enforced server-side).


# Known event types (informational; not enforced — RFC §1 extension-friendly):
KNOWN_PEER_HEALTH_EVENT_TYPES = {
    # 5-state §6.4a R9/R10 taxonomy (hook-fired sentinels)
    "Active",
    "Stop",
    "StopFailure",
    "Compacting",
    "Quota-exhausted",
    # 6th-state Path A discoveries (JSONL-parsed)
    "rate_limit_429",
    "classifier_unavailable_bash_denied",
    "usage_limit_reset",
}


class PeerHealthEventPost(BaseModel):
    time: str = Field(..., min_length=1)         # ISO-8601 event timestamp
    peer: str = Field(..., min_length=1)         # observed peer name
    event_type: str = Field(..., min_length=1)   # extensible enumeration
    source: Optional[str] = Field(default=None)
    source_session_id: Optional[str] = Field(default=None)
    source_jsonl_path: Optional[str] = Field(default=None)
    metadata: Optional[dict] = Field(default=None)


class PeerHealthEventBatch(BaseModel):
    """Optional batch wrapper — multiple events in one POST for cron efficiency."""
    events: list[PeerHealthEventPost] = Field(..., min_length=1)


@app.post("/peer-health-events")
async def peer_health_event_post(
    req: PeerHealthEventPost,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Append a single peer-health event to the log."""
    auth = _authorize(authorization)
    # The reported `peer` is the observed-peer actor field. Bind it so a caller
    # can't post health events impersonating another peer. NOTE: cross-vertex
    # observers legitimately report ON other peers, but the `peer` field here is
    # the SUBJECT of the event the caller is asserting — binding it to the
    # authenticated caller is the R1 actor-identity rule; if a distinct
    # observer→observed semantic is needed it belongs in the observations table,
    # not this self-report log. (warn-only; seat-B to confirm the semantic.)
    _check_caller_binding(auth, req.peer, field="peer", site="peer_health_event_post")
    now = _utcnow_iso()
    md = json.dumps(req.metadata) if req.metadata else None
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO peer_health_events "
            "(time, peer, event_type, source, source_session_id, "
            " source_jsonl_path, metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (req.time, req.peer, req.event_type, req.source,
             req.source_session_id, req.source_jsonl_path, md, now),
        )
        event_id = cur.lastrowid
    return {"id": event_id, "time": req.time, "peer": req.peer,
            "event_type": req.event_type, "created_at": now}


@app.post("/peer-health-events/batch")
async def peer_health_event_batch_post(
    req: PeerHealthEventBatch,
    authorization: Optional[str] = Header(None),
) -> dict:
    """Append N events in one transaction. Used by cron-callers that
    accumulated events between polls (efficient vs per-event POST)."""
    auth = _authorize(authorization)
    # Per-event binding: each event's `peer` is its own actor field. A batch
    # could otherwise mix the caller's real events with forged-peer ones.
    for ev in req.events:
        _check_caller_binding(auth, ev.peer, field="peer", site="peer_health_event_batch_post")
    now = _utcnow_iso()
    ids: list[int] = []
    with _conn() as c:
        for ev in req.events:
            md = json.dumps(ev.metadata) if ev.metadata else None
            cur = c.execute(
                "INSERT INTO peer_health_events "
                "(time, peer, event_type, source, source_session_id, "
                " source_jsonl_path, metadata, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ev.time, ev.peer, ev.event_type, ev.source,
                 ev.source_session_id, ev.source_jsonl_path, md, now),
            )
            ids.append(cur.lastrowid)
    return {"n": len(ids), "ids": ids, "created_at": now}


@app.get("/peer-health-events")
async def peer_health_event_get(
    authorization: Optional[str] = Header(None),
    peer: Optional[str] = Query(None),
    event_type: Optional[str] = Query(None),
    since: Optional[str] = Query(None, description="ISO timestamp; events strictly after"),
    limit: int = Query(100, ge=1, le=10000),
) -> dict:
    """Query peer-health events. Filterable by peer + event_type + since."""
    _authorize(authorization)
    clauses, params = [], []
    if peer:        clauses.append("peer = ?");       params.append(peer)
    if event_type:  clauses.append("event_type = ?"); params.append(event_type)
    if since:       clauses.append("time > ?");       params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _conn() as c:
        rows = c.execute(
            f"SELECT id, time, peer, event_type, source, source_session_id, "
            f"source_jsonl_path, metadata, created_at "
            f"FROM peer_health_events {where} ORDER BY time DESC LIMIT ?",
            params,
        ).fetchall()
    return {"events": [dict(r) for r in rows], "n": len(rows)}


@app.get("/peer-health-events/stats")
async def peer_health_event_stats(
    authorization: Optional[str] = Header(None),
    since: Optional[str] = Query(None),
) -> dict:
    """Aggregate counts per (peer, event_type) since timestamp. Useful for
    Mercury / dashboard surfaces — answers 'how many rate-limit events
    has drop hit today?' style queries cheaply."""
    _authorize(authorization)
    clause = "WHERE time > ?" if since else ""
    params = [since] if since else []
    with _conn() as c:
        rows = c.execute(
            f"SELECT peer, event_type, COUNT(*) AS n "
            f"FROM peer_health_events {clause} "
            f"GROUP BY peer, event_type ORDER BY n DESC",
            params,
        ).fetchall()
    return {"stats": [dict(r) for r in rows], "since": since}


# =====================================================================
# COUNCIL WORKER POOL — subscription-path queue (RFC v1, PR #164)
# =====================================================================
# A producer POSTs /council-batch with N debate jobs. Workers claim jobs
# atomically, spawn a `claude -p` subprocess via subscription credentials,
# write result back via /council-complete or /council-fail. The producer
# polls /council-batch/{debate_id} for results.


class CouncilJobSpec(BaseModel):
    role: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    model: str = Field(default="claude-sonnet-4-6")
    # subscription_chat-only (ignored for council debate roles):
    purpose: Optional[str] = Field(default=None, max_length=128,
        description="Required for role=subscription_chat — human-readable caller "
        "intent (e.g. 'skunkworks-architect'); makes the generic queue auditable.")
    response_schema: Optional[str] = Field(default=None,
        description="Optional JSON-schema string; worker post-parses + retries once.")
    system_prompt: Optional[str] = Field(default=None,
        description="Optional system prompt for subscription_chat; `prompt` is the user turn.")


class CouncilBatchRequest(BaseModel):
    debate_id: str = Field(..., min_length=1)
    jobs: list[CouncilJobSpec] = Field(..., min_length=1)
    # C2 caller-binding (sweep #1897 MED): optional claimed enqueuer identity. When
    # present, the gateway binds it to the authenticated per-peer token (warn-only
    # until enforce), so council/subscription-spend enqueues become attributable +
    # bindable like every other actor source. Optional + backward-compatible:
    # existing shared-token callers (the council producer path) omit it and hit the
    # documented no-op. Real closure is C5 (shared-token retirement).
    from_node: Optional[str] = Field(default=None, max_length=64,
        description="claimed enqueuer peer name; C2-bound to the caller's token when set")


class CouncilClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1)


class CouncilCompleteRequest(BaseModel):
    job_id: int
    worker_id: str = Field(..., min_length=1, max_length=64)
    result_json: str = Field(..., min_length=1)


class CouncilFailRequest(BaseModel):
    job_id: int
    worker_id: str = Field(..., min_length=1, max_length=64)
    error: str = Field(..., min_length=1)
    should_retry: bool = Field(default=True)


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 chat-sync endpoint (2026-05-18) — synchronous chat through worker
# pool. Wraps council_jobs lifecycle: INSERT one job with role=r0_public_chat,
# poll status until done/failed/timeout, return result_json. Audit log in
# public_chat_log table.
#
# Caller pattern (PHP proxy on brainsurfing.tech):
#   POST /api/chat-sync
#     headers: Authorization: Bearer <mesh-token>
#     body: {prompt: str, timeout_sec: int (default 30), source: str (optional)}
#   returns: {ok: bool, text: str, latency_ms: int, job_id: int}  on success
#            {ok: false, error: str, latency_ms: int}              on failure
#
# Security layers (defense-in-depth, top of CLAUDE.md billing-path rule):
#   - nginx edge: 5 req/min per IP rate limit, path allowlist only /api/chat-sync
#   - PHP proxy: per-session rate limit, origin/referer validation, input sanity
#   - mesh-gateway (this endpoint): kill-switch file flag, daily global cap,
#     per-IP-hash recent count check, bearer auth, role gate (r0_public_chat only)
#   - council_worker: _scrubbed_env strips ANTHROPIC_API_KEY (subscription path)
#
# Failure-mode semantics:
#   - kill-switch active     → 503 with response_status='kill_switch'
#   - daily cap exceeded     → 429 with response_status='cap_exceeded'
#   - bearer auth fail       → 401 (existing _authorize)
#   - worker timeout         → 504 with response_status='timeout'
#   - LLM/worker failure     → 502 with response_status='failed'
#   - success                → 200 with response_status='done'
# Every outcome lands a row in public_chat_log for abuse investigation.
# ─────────────────────────────────────────────────────────────────────────

_PUBLIC_CHAT_KILLSWITCH_PATH = "/var/lib/mesh-gateway/public_chat_killswitch"
_PUBLIC_CHAT_DAILY_CAP = int(os.environ.get("PUBLIC_CHAT_DAILY_CAP", "200"))
_PUBLIC_CHAT_PER_IP_HOURLY_CAP = int(os.environ.get("PUBLIC_CHAT_PER_IP_HOURLY_CAP", "10"))
_PUBLIC_CHAT_DEFAULT_MODEL = os.environ.get(
    "PUBLIC_CHAT_DEFAULT_MODEL", "claude-sonnet-4-6"
)
_PUBLIC_CHAT_POLL_INTERVAL_SEC = 0.2


class ChatSyncRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    timeout_sec: int = Field(default=30, ge=5, le=60)
    source: Optional[str] = Field(default="brainsurfing", max_length=64)


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _client_ip(request: Request) -> str:
    """Extract the client IP for per-IP rate limiting, resistant to header spoofing.

    SECURITY (sweep #1897 MED): the LEFTMOST X-Forwarded-For entry is
    CLIENT-CONTROLLED — a client can send `X-Forwarded-For: 1.2.3.4` and our nginx
    appends the real peer, making the chain `1.2.3.4, <real>`. Trusting the leftmost
    (the old behavior) let a client rotate a fake IP to evade the per-IP cap. We now
    trust only what OUR edge sets:
      1. X-Real-IP — nginx OVERWRITES this with $remote_addr (the real TCP peer); a
         client-supplied value is discarded by the proxy, so it's authoritative.
      2. else the RIGHTMOST X-Forwarded-For hop — the entry our nginx APPENDED
         ($proxy_add_x_forwarded_for); rightmost = last proxy = real peer (assumes a
         single trusted proxy in front, which is our topology). The leftmost (client-
         supplied) entries are intentionally discarded.
      3. else request.client.host (direct connection / no proxy).
    """
    real = request.headers.get("x-real-ip")
    if real and real.strip():
        return real.strip()
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        hops = [h.strip() for h in fwd.split(",") if h.strip()]
        if hops:
            return hops[-1]  # rightmost = nginx-appended real peer (not the spoofable leftmost)
    if request.client:
        return request.client.host
    return "unknown"


def _log_public_chat(
    ip_hash: str,
    prompt_hash: str,
    prompt_length: int,
    response_status: str,
    latency_ms: Optional[int],
    response_length: Optional[int],
    job_id: Optional[int],
    source: Optional[str],
    error: Optional[str],
) -> None:
    """Write one row to public_chat_log. Never raises — audit MUST NOT
    break the request path. Logs to module logger on failure for ops."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO public_chat_log "
                "(time, ip_hash, prompt_hash, prompt_length, response_status, "
                " latency_ms, response_length, job_id, source, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _utcnow_iso(), ip_hash, prompt_hash, prompt_length,
                    response_status, latency_ms, response_length,
                    job_id, source, error,
                ),
            )
    except Exception as exc:  # noqa: BLE001 — audit must not fail the request
        log.error("public_chat_log insert failed: %s", exc)


@app.post("/api/chat-sync")
async def chat_sync(req: ChatSyncRequest,
                    request: Request,
                    authorization: Optional[str] = Header(None)) -> dict:
    """Synchronous chat through council worker pool.

    Inserts a council_jobs row with role=r0_public_chat, polls until
    done/failed/timeout, returns result_json. Wraps existing worker
    lifecycle (claim → claude -p → complete) so no new worker plumbing
    is needed beyond extending _CLAUDE_ROLES with r0_public_chat
    (worker side).

    Audit log row written in EVERY outcome path (success/failure/timeout/
    kill-switch/cap-exceeded). GDPR-clean: ip + prompt hashed, never raw.
    """
    _authorize(authorization)
    t_start = time.time()
    ip = _client_ip(request)
    ip_hash = _sha256(ip)
    prompt_hash = _sha256(req.prompt)
    prompt_length = len(req.prompt)
    source = req.source or "unknown"

    # Layer 1: kill-switch file flag (operator emergency disable, no restart)
    if os.path.exists(_PUBLIC_CHAT_KILLSWITCH_PATH):
        _log_public_chat(
            ip_hash, prompt_hash, prompt_length, "kill_switch",
            int((time.time() - t_start) * 1000), None, None, source,
            "kill-switch file present",
        )
        raise HTTPException(503, "chat endpoint temporarily disabled")

    # Layers 2-4: cap-check + enqueue, ATOMIC under a write lock.
    #
    # R1 fix (chat-sync cap-race, 2026-06-02 — lab whole-substrate sweep #1897).
    # This is the last server-side defense above nginx/PHP on a PUBLIC,
    # subscription-billed lane, so it must hold under burst. Two compounding
    # bugs were here; both are closed below:
    #   (a) RACE: the old block read the cap then INSERTed with no write lock
    #       (the per-request conn is isolation_level=None / autocommit), so N
    #       concurrent requests all read daily_count<cap before any insert landed
    #       -> all enqueued. BEGIN IMMEDIATE takes the write lock up front, so the
    #       count->insert boundary serializes; contenders WAIT (busy_timeout),
    #       they don't all slip through.
    #   (b) BLIND CAP: the cap counted only public_chat_log, which is written at
    #       job COMPLETION (_log_public_chat). Enqueue writes council_jobs. So
    #       enqueued-but-still-running jobs were invisible to the cap for the
    #       WHOLE worker duration — a burst inside that window all saw the same
    #       stale low count and the daily subscription-spend cap was bypassable.
    #       The daily count is now completed(public_chat_log) + in-flight
    #       (council_jobs not yet terminal). No double-count: a terminal job has a
    #       log row and is excluded from the in-flight term; an in-flight job has
    #       no log row yet, so it's counted exactly once.
    # _log_public_chat opens its OWN connection, so EVERY logging call happens
    # AFTER the COMMIT releases the write lock — calling it while holding
    # BEGIN IMMEDIATE would self-deadlock (the new conn blocks on the lock we hold).
    # SCOPE: the per-IP hourly cap still counts only public_chat_log here.
    # Counting in-flight per-IP needs an ip_hash column on council_jobs — batched
    # into the per-IP PR with the X-Forwarded-For trust fix (sweep #1897 MEDs).
    # BEGIN IMMEDIATE already removes the concurrent-instant race for it too.
    now_iso = _utcnow_iso()
    day_ago_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    hour_ago_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    debate_id = f"chat-{uuid.uuid4()}"
    job_id = None
    daily_exceeded = False
    daily_seen = 0
    ip_exceeded = False
    with _conn() as c:
        c.execute("PRAGMA busy_timeout = 5000")  # contenders WAIT for the lock, don't error
        c.execute("BEGIN IMMEDIATE")             # serialize the count->insert boundary
        try:
            daily_done = c.execute(
                "SELECT COUNT(*) AS n FROM public_chat_log "
                "WHERE time > ? AND response_status NOT IN ('cap_exceeded','kill_switch')",
                (day_ago_iso,),
            ).fetchone()["n"]
            daily_inflight = c.execute(
                "SELECT COUNT(*) AS n FROM council_jobs "
                "WHERE role='r0_public_chat' AND created_at > ? "
                "AND status NOT IN ('done','failed','timeout','cancelled')",
                (day_ago_iso,),
            ).fetchone()["n"]
            daily_seen = daily_done + daily_inflight
            if daily_seen >= _PUBLIC_CHAT_DAILY_CAP:
                daily_exceeded = True
            else:
                ip_count = c.execute(
                    "SELECT COUNT(*) AS n FROM public_chat_log "
                    "WHERE ip_hash=? AND time > ? AND response_status NOT IN "
                    "('cap_exceeded','kill_switch','rate_limited')",
                    (ip_hash, hour_ago_iso),
                ).fetchone()["n"]
                if ip_count >= _PUBLIC_CHAT_PER_IP_HOURLY_CAP:
                    ip_exceeded = True
                else:
                    c.execute(
                        "INSERT INTO council_jobs "
                        "(debate_id, role, prompt, model, status, created_at) "
                        "VALUES (?, 'r0_public_chat', ?, ?, 'pending', ?)",
                        (debate_id, req.prompt, _PUBLIC_CHAT_DEFAULT_MODEL, now_iso),
                    )
                    job_id = c.execute(
                        "SELECT id FROM council_jobs WHERE debate_id=?", (debate_id,),
                    ).fetchone()["id"]
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    # Write lock released — now safe to open new connections for logging + raise.
    if daily_exceeded:
        _log_public_chat(
            ip_hash, prompt_hash, prompt_length, "cap_exceeded",
            int((time.time() - t_start) * 1000), None, None, source,
            f"daily cap {_PUBLIC_CHAT_DAILY_CAP} exceeded ({daily_seen})",
        )
        raise HTTPException(429, "daily chat capacity exceeded")
    if ip_exceeded:
        _log_public_chat(
            ip_hash, prompt_hash, prompt_length, "rate_limited",
            int((time.time() - t_start) * 1000), None, None, source,
            f"per-IP hourly cap {_PUBLIC_CHAT_PER_IP_HOURLY_CAP} exceeded",
        )
        raise HTTPException(429, "rate limit exceeded for client")

    # Layer 5: poll for completion. await asyncio.sleep between checks so
    # we don't hog the FastAPI event loop. Each poll opens a fresh _conn().
    deadline = t_start + req.timeout_sec
    while time.time() < deadline:
        await asyncio.sleep(_PUBLIC_CHAT_POLL_INTERVAL_SEC)
        with _conn() as c:
            r = c.execute(
                "SELECT status, result_json, error FROM council_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
        if not r:
            # Shouldn't happen — we just inserted it
            _log_public_chat(
                ip_hash, prompt_hash, prompt_length, "failed",
                int((time.time() - t_start) * 1000), None, job_id, source,
                "council_jobs row vanished mid-poll",
            )
            raise HTTPException(500, "internal: job row missing")
        if r["status"] == "done":
            # Parse worker result. council_worker writes a JSON envelope
            # with {"text": "...", "stderr_tail": "..."}; we want the text.
            try:
                parsed = json.loads(r["result_json"]) if r["result_json"] else {}
            except json.JSONDecodeError:
                parsed = {"text": r["result_json"] or ""}
            text = parsed.get("text", "") if isinstance(parsed, dict) else str(parsed)
            latency_ms = int((time.time() - t_start) * 1000)
            _log_public_chat(
                ip_hash, prompt_hash, prompt_length, "done",
                latency_ms, len(text), job_id, source, None,
            )
            return {
                "ok": True, "text": text, "latency_ms": latency_ms,
                "job_id": job_id,
            }
        if r["status"] == "failed":
            err = r["error"] or "worker reported failure"
            latency_ms = int((time.time() - t_start) * 1000)
            _log_public_chat(
                ip_hash, prompt_hash, prompt_length, "failed",
                latency_ms, None, job_id, source, err[:500],
            )
            raise HTTPException(502, f"worker failed: {err[:200]}")

    # Timeout reached without completion
    latency_ms = int((time.time() - t_start) * 1000)
    _log_public_chat(
        ip_hash, prompt_hash, prompt_length, "timeout",
        latency_ms, None, job_id, source, f"timeout after {req.timeout_sec}s",
    )
    raise HTTPException(504, f"chat timeout after {req.timeout_sec}s")


@app.post("/council-batch")
async def council_batch_post(req: CouncilBatchRequest,
                              authorization: Optional[str] = Header(None)) -> dict:
    """Insert N jobs. Idempotent via UNIQUE(debate_id,role).

    subscription_chat jobs (substrate sprint Item 1): REQUIRE `purpose`, are
    subject to a per-day cap, and have their {system_prompt,user_prompt,
    response_schema,purpose} packed as JSON into the stored `prompt` column so
    the worker can unpack without new schema columns."""
    auth = _authorize(authorization)
    # C2 caller-binding (sweep #1897 MED): bind the claimed enqueuer to the
    # authenticated token when from_node is supplied (warn-only until enforce).
    # Closes the "any authenticated peer can enqueue subscription-spend jobs"
    # gap — council-batch was the one actor source the C2 wiring missed. No-op
    # for shared-token callers (the current council producer path) by design.
    if req.from_node:
        _check_caller_binding(auth, req.from_node, field="from_node", site="council_batch")
    for j in req.jobs:
        if j.role not in VALID_COUNCIL_ROLES:
            raise HTTPException(400,
                f"role must be one of {sorted(VALID_COUNCIL_ROLES)}; got {j.role!r}")
        if j.role == "subscription_chat" and not (j.purpose and j.purpose.strip()):
            raise HTTPException(400,
                "role=subscription_chat requires a non-empty `purpose` (auditable caller intent)")
    now = _utcnow_iso()
    # guard (b): per-day cap on subscription_chat (count today's rows)
    n_sub = sum(1 for j in req.jobs if j.role == "subscription_chat")
    if n_sub:
        with _conn() as c:
            today = now[:10]
            used = c.execute(
                "SELECT COUNT(*) AS n FROM council_jobs WHERE role='subscription_chat' "
                "AND created_at >= ?", (today + "T00:00:00",),
            ).fetchone()["n"]
        if used + n_sub > SUBSCRIPTION_CHAT_DEFAULT_MAX_PER_DAY:
            raise HTTPException(429,
                f"subscription_chat daily cap {SUBSCRIPTION_CHAT_DEFAULT_MAX_PER_DAY} "
                f"would be exceeded (used={used}, requested={n_sub})")
    inserted: list[dict] = []
    with _conn() as c:
        for j in req.jobs:
            if j.role == "subscription_chat":
                stored_prompt = json.dumps({
                    "user_prompt": j.prompt,
                    "system_prompt": j.system_prompt,
                    "response_schema": j.response_schema,
                    "purpose": j.purpose,
                })
            else:
                stored_prompt = j.prompt
            cur = c.execute(
                "INSERT OR IGNORE INTO council_jobs "
                "(debate_id, role, prompt, model, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (req.debate_id, j.role, stored_prompt, j.model, now),
            )
            # sweep #1897 LOW: INSERT OR IGNORE silently no-ops when (debate_id,role)
            # already exists, and the SELECT-back then returns the PRE-EXISTING row —
            # previously indistinguishable from a fresh insert. `created` (rowcount==1)
            # tells the caller whether THIS request created the row or echoed a row it
            # didn't create, so a collision can't masquerade as a successful enqueue.
            # (Full owner-scoping — never echoing a foreign row's id — needs an owner
            # column on council_jobs; deferred. debate_ids are unguessable UUIDs/ULIDs,
            # so the practical leak surface is small.)
            created = cur.rowcount == 1
            row = c.execute(
                "SELECT id, status FROM council_jobs WHERE debate_id=? AND role=?",
                (req.debate_id, j.role),
            ).fetchone()
            inserted.append({"role": j.role, "id": row["id"], "status": row["status"],
                             "created": created})
    log.info("council-batch debate=%s n_jobs=%d (subscription_chat=%d)",
             req.debate_id, len(req.jobs), n_sub)
    return {"debate_id": req.debate_id, "jobs": inserted, "n": len(inserted)}


@app.post("/council-claim")
async def council_claim_post(req: CouncilClaimRequest,
                              authorization: Optional[str] = Header(None)) -> dict:
    """Atomic claim of next pending job. Returns the job or 204 if empty.

    Uses BEGIN IMMEDIATE + UPDATE pattern (same as claude_tasks.claim) for
    race-safe single-claim. Worker calls this in a poll loop. Increments
    attempts counter."""
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.worker_id, field="worker_id", site="council_claim", enforce_identity=False)
    now = _utcnow_iso()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT id, debate_id, role, prompt, model, attempts, max_attempts "
                "FROM council_jobs WHERE status='pending' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                c.execute("COMMIT")
                # sweep #1897 LOW: 204 No Content MUST NOT carry a body. The old
                # JSONResponse(content=None) serialized the literal `null` (a 4-byte
                # body) → a protocol-invalid 204-with-body that strict HTTP clients/
                # proxies reject. A bodiless Response(204) is correct; the worker
                # branches on status_code==204 (empty queue), not on body shape.
                return Response(status_code=204)
            c.execute(
                "UPDATE council_jobs SET status='claimed', claimed_by=?, "
                "claimed_at=?, attempts=attempts+1 WHERE id=?",
                (req.worker_id, now, row["id"]),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    log.info("council-claim job=%d debate=%s role=%s worker=%s",
             row["id"], row["debate_id"], row["role"], req.worker_id)
    return {
        "id": row["id"],
        "debate_id": row["debate_id"],
        "role": row["role"],
        "prompt": row["prompt"],
        "model": row["model"],
        "attempt": row["attempts"] + 1,
        "max_attempts": row["max_attempts"],
    }


@app.post("/council-complete")
async def council_complete_post(req: CouncilCompleteRequest,
                                  authorization: Optional[str] = Header(None)) -> dict:
    """Worker reports job completed successfully. Stores result_json + flips
    status to 'done'.

    Phase 1.2 #1: requires ``worker_id`` matching ``council_jobs.claimed_by``.
    Refuses 403 if a different worker tries to mark another's job done,
    or 409 if the job isn't currently in 'claimed' state (already done /
    failed / pending). Prevents one badly-scheduled or misconfigured worker
    from clobbering another's in-flight result.
    """
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.worker_id, field="worker_id", site="council_complete", enforce_identity=False)
    now = _utcnow_iso()
    with _conn() as c:
        r = c.execute(
            "SELECT status, claimed_by FROM council_jobs WHERE id=?", (req.job_id,)
        ).fetchone()
        if not r:
            raise HTTPException(404, f"council_job id={req.job_id} not found")
        if r["status"] != "claimed":
            raise HTTPException(
                409,
                f"council_job id={req.job_id} status={r['status']!r} "
                f"not 'claimed' — cannot complete",
            )
        if r["claimed_by"] != req.worker_id:
            raise HTTPException(
                403,
                f"council_job id={req.job_id} claimed by "
                f"{r['claimed_by']!r}, not {req.worker_id!r}",
            )
        c.execute(
            "UPDATE council_jobs SET status='done', completed_at=?, "
            "result_json=? WHERE id=?",
            (now, req.result_json, req.job_id),
        )
    log.info("council-complete job=%d worker=%s", req.job_id, req.worker_id)
    return {"id": req.job_id, "status": "done", "completed_at": now}


@app.post("/council-fail")
async def council_fail_post(req: CouncilFailRequest,
                             authorization: Optional[str] = Header(None)) -> dict:
    """Worker reports job failed. If should_retry=true AND attempts<max_attempts,
    flips back to 'pending' for another claim. Else flips to 'failed'.

    Phase 1.2 #1: requires ``worker_id`` matching ``council_jobs.claimed_by``.
    Refuses 403 if a different worker tries to mark another's job failed,
    or 409 if the job isn't in 'claimed' state.
    """
    auth = _authorize(authorization)
    _check_caller_binding(auth, req.worker_id, field="worker_id", site="council_fail", enforce_identity=False)
    now = _utcnow_iso()
    with _conn() as c:
        r = c.execute(
            "SELECT attempts, max_attempts, status, claimed_by "
            "FROM council_jobs WHERE id=?",
            (req.job_id,),
        ).fetchone()
        if not r:
            raise HTTPException(404, f"council_job id={req.job_id} not found")
        if r["status"] != "claimed":
            raise HTTPException(
                409,
                f"council_job id={req.job_id} status={r['status']!r} "
                f"not 'claimed' — cannot fail",
            )
        if r["claimed_by"] != req.worker_id:
            raise HTTPException(
                403,
                f"council_job id={req.job_id} claimed by "
                f"{r['claimed_by']!r}, not {req.worker_id!r}",
            )
        if req.should_retry and r["attempts"] < r["max_attempts"]:
            # Reset to pending; claimed_by stays for audit-trail; another
            # worker can re-claim on next poll
            c.execute(
                "UPDATE council_jobs SET status='pending', error=? WHERE id=?",
                (req.error, req.job_id),
            )
            new_status = "pending"
        else:
            c.execute(
                "UPDATE council_jobs SET status='failed', completed_at=?, "
                "error=? WHERE id=?",
                (now, req.error, req.job_id),
            )
            new_status = "failed"
    log.info("council-fail job=%d new_status=%s err=%.100s",
             req.job_id, new_status, req.error)
    return {"id": req.job_id, "status": new_status, "error": req.error}


@app.get("/council-batch/{debate_id}")
async def council_batch_get(debate_id: str,
                             authorization: Optional[str] = Header(None)) -> dict:
    """Return all jobs for a debate + their statuses. The producer polls this
    to assemble the debate result after POST /council-batch."""
    _authorize(authorization)
    with _conn() as c:
        rows = c.execute(
            "SELECT id, role, status, claimed_by, attempts, created_at, "
            "claimed_at, completed_at, result_json, error "
            "FROM council_jobs WHERE debate_id=? ORDER BY id ASC",
            (debate_id,),
        ).fetchall()
    if not rows:
        raise HTTPException(404, f"no jobs for debate_id={debate_id!r}")
    all_done = all(r["status"] == "done" for r in rows)
    any_failed = any(r["status"] == "failed" for r in rows)
    return {
        "debate_id": debate_id,
        "n": len(rows),
        "all_done": all_done,
        "any_failed": any_failed,
        "jobs": [dict(r) for r in rows],
    }


class CouncilReclaimStuckRequest(BaseModel):
    older_than_min: int = Field(default=5, ge=1, le=1440)
    dry_run: bool = Field(default=False)


@app.post("/council-reclaim-stuck")
async def council_reclaim_stuck(req: CouncilReclaimStuckRequest,
                                  authorization: Optional[str] = Header(None)) -> dict:
    """Reset claimed jobs whose worker presumably died (claimed_at older
    than `older_than_min` minutes) back to status='pending' so they can
    be re-claimed by another worker.

    Closes Phase 1.1 stuck-claim recovery gap per drop-mother seat-A
    DM #1261 point B. RFC §5 acknowledged the need; this endpoint ships
    the discipline. Ops cron fires this every 5min; manual ops trigger
    also acceptable.

    dry_run=true returns the IDs that WOULD be reclaimed without modifying.
    Useful for ops investigation before pulling the trigger.

    SAFETY MARGIN: older_than_min=5 is safe while council_worker
    CLAUDE_TIMEOUT_SEC ≤ 90s (3.3× margin between max-legitimate-claim-
    duration and reclamation cutoff). If subprocess timeout is ever
    bumped (Anthropic API spike, longer prompts, etc.), the race-window
    closes — a still-running worker could have its job reclaimed
    mid-flight + completion conflict with the re-claimer. Phase 1.2
    candidate per drop-mother seat-A DM #1282: /council-complete should
    verify claimed_by matches caller's worker_id and 409 on mismatch
    ("the worker who claimed owns the completion lane"). Until that
    lands, keep older_than_min ≥ 5 AND keep subprocess timeout ≤ 90s.

    Returns:
      {
        "reclaimed": [ids...],
        "n_reclaimed": int,
        "older_than_min": int,
        "dry_run": bool,
      }
    """
    _authorize(authorization)
    cutoff_iso = (datetime.now(timezone.utc)
                  - timedelta(minutes=req.older_than_min)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT id, debate_id, role, claimed_by, claimed_at, attempts "
            "FROM council_jobs WHERE status='claimed' AND claimed_at < ?",
            (cutoff_iso,),
        ).fetchall()
        stuck = [dict(r) for r in rows]
        ids = [r["id"] for r in stuck]
        if ids and not req.dry_run:
            placeholders = ",".join("?" * len(ids))
            # Reset to pending; preserve attempts counter + claimed_by for
            # audit-trail. Next /council-claim will increment attempts +
            # set new claimed_by.
            c.execute(
                f"UPDATE council_jobs SET status='pending' "
                f"WHERE id IN ({placeholders})",
                ids,
            )
    if ids:
        log.warning("council-reclaim-stuck reclaimed=%d older_than=%dmin dry_run=%s",
                    len(ids), req.older_than_min, req.dry_run)
        for s in stuck:
            log.warning("  reclaim id=%d debate=%s role=%s was_claimed_by=%s "
                        "claimed_at=%s attempts=%d",
                        s["id"], s["debate_id"], s["role"],
                        s["claimed_by"], s["claimed_at"], s["attempts"])
    return {
        "reclaimed": ids,
        "stuck": stuck if req.dry_run else None,
        "n_reclaimed": len(ids),
        "older_than_min": req.older_than_min,
        "dry_run": req.dry_run,
    }


# =====================================================================
# BRAIN PROXY (POST /brain/query)
# =====================================================================

def _brain_query_upstream(question: str, limit: int) -> list:
    """Proxy a READ-ONLY gbrain query. Builds the MCP `query` call itself (never
    write/admin), POSTs to GATEWAY_GBRAIN_URL with the gateway-held token, returns
    the chunk array. Raises on any upstream/parse failure (caller maps to 502)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "query",
                       "arguments": {"query": question, "limit": limit, "expand": False}}}
    req = urllib.request.Request(
        GATEWAY_GBRAIN_URL, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", f"Bearer {GATEWAY_GBRAIN_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed tailnet URL
        raw = resp.read().decode("utf-8")
    payload = raw
    if "data:" in raw:
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("data:"):
                payload = s[len("data:"):].strip()
                break
    doc = json.loads(payload)
    parsed = json.loads(doc["result"]["content"][0]["text"])
    return parsed if isinstance(parsed, list) else []


class BrainQueryRequest(BaseModel):
    query: str
    limit: int = 8


@app.post("/brain/query")
async def brain_query(req: BrainQueryRequest,
                      authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)                      # 401 on bad mesh token
    if not GATEWAY_GBRAIN_TOKEN:
        raise HTTPException(503, "brain proxy not configured")
    if not req.query.strip():
        raise HTTPException(400, "query is required")
    try:
        chunks = await run_in_threadpool(_brain_query_upstream, req.query, req.limit)
    except Exception as e:  # fail loud — never a swallowed empty result
        log.warning("brain proxy upstream error for peer=%s: %s", auth.peer, e)
        raise HTTPException(502, "brain upstream error")
    log.info("brain query peer=%s regime=%s limit=%d chunks=%d",
             auth.peer, auth.regime, req.limit, len(chunks))
    return {"chunks": chunks}


# =====================================================================
# STARTUP
# =====================================================================

_init_db()
_lc.init_db()   # launched-lane registry (separate lanes.db; mirrors the table inits above)
log.info("mesh-gateway ready: port=%d db=%s", PORT, DB_PATH)
