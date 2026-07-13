# swairm-Pattern Port: Agent Credential Isolation, Untrusted-Repo Preflight, Orphaned-Claim Recovery — Design

**Status:** approved scope 2026-07-13 (commander, async — Sardinia). Build **#2a**; spec **#2b, #3, #1**.
**Goal:** port three hardening patterns from swairm (github.com/morzu117/swairm) into swarph, each scoped to what swarph's *actual* code needs — not a mechanical copy.
**Source:** [[reference_swairm_repo]]. swairm's implementations are ports-and-adapters, TDD, hardened through their own review rounds (R1/I1–I3).

---

## Grounding — what the swarph code actually is (verified 2026-07-13)

An Explore pass mapped every landing zone. The headline facts that shape this design:

1. **Credential leak is real and proven.** Every agent-spawn site **except grok** inherits the
   operator's real `HOME`. The billing scrubbers (`scrub_env_for_subprocess` /
   `scrub_billing_env` / `_scrubbed_env`) strip only `*_API_KEY` / `*_AUTH_TOKEN` / `*_BASE_URL`.
   **None scrub GitHub creds.** With the inherited HOME, `~/.claude/.credentials.json`,
   `~/.config/gh/hosts.yml`, `~/.git-credentials`, `~/.netrc`, `~/.ssh` are all readable by the
   spawned agent. Proof: `gh auth token` returns a token from the env a spawned cell inherits.
   Impact: a spawned coding cell can act as the operator on GitHub.
2. **grok already solves it in-repo** — the reference prototype:
   - `spawn.py:_grok_env` → disposable HOME under `cell.cwd/.grok-cell`, symlinks **only**
     `~/.grok/auth.json` in, scrubs the whole `GROK_*`/`XAI_*` namespace to block HOME-bypass
     redirects (`GROK_HOME`/`GROK_AUTH_PATH`).
   - `grok-service` → bwrap default-deny jail + `CLEAN_HOME` allowlist built from scratch
     (`PATH HOME USER TERM LANG`).
3. **No automated reviewer exists.** `--setting-sources` appears in **zero** swarph repos; no
   `git config --list` poison probe anywhere. `security.py:llm_review` is scaffolded-not-wired and
   reviews *content text*, not git diffs. So pattern #3 has **no consumer today**.
4. **Durable state is already solid; recovery is the gap.** All control-plane state is one WAL
   SQLite (`/home/ubuntu/research/claude_mesh.db`), atomic `BEGIN IMMEDIATE` claims, unique keys —
   no volatile queue to lose. But: `claude_tasks` has **no orphan reaper**; `gpu_jobs` has a
   `reclaim-stale` endpoint with **no automated caller**; both existing reclaim paths reclaim by
   `claimed_at` **age only** → a still-alive worker's job can be reclaimed and **run twice**.

**Consequence for #1:** swarph does not need "GitHub as sole truth" — it already has a single
durable source of truth. It needs the *other* half swairm has: **orphaned-claim recovery**. So #1
is reframed from an architecture bet to a bounded crash-safety fix.

---

## Pattern #2 — Disposable-HOME credential isolation

### Problem
A spawned agent should never receive credentials it does not need. Today only grok is isolated;
claude/codex/agy and every headless `claude -p` inherit the operator HOME and thus every on-disk
credential. The worst instance: `lab-orchestrator/orchestrator.py` itself runs `gh repo clone` /
`gh pr create` (it *is* the credential holder) and spawns a `claude -p` child that inherits those
creds — exactly swairm's "the drone must not hold the daemon's credential."

### Split (why two deliverables)
The interactive **cell membrane** (`spawn.py` claude/codex/agy) is *not* the same problem as a
**headless one-shot**. An interactive claude cell reads the operator's `~/.claude/settings.json`
(SessionStart hooks, the lab ritual, MCP config, the inbox-watcher) — a naive HOME swap would
strip the very hooks the cell depends on, and it entangles with the shared-settings issue (#20).
A headless `claude -p` needs only *auth + a prompt*. So:

- **#2a (BUILD NOW):** isolate the headless one-shot spawns.
- **#2b (SPEC, later):** isolate the interactive cell membrane, with settings/hooks curation.

### #2a design — headless spawn isolation

**New shared helper** in `swarph-shared` (consumed by swarph-cli and the *-service servers):

```
isolated_agent_home(provider: str, *, root: Path, link_auth: bool = True) -> Path
```
- Creates a disposable HOME at `root/.<provider>-drone-home` (mirrors grok's `.grok-cell`).
- Symlinks **only the provider's own auth** into it (best-effort, idempotent — generalises
  `_link_grok_auth`):
  - `claude`  → `~/.claude/.credentials.json` → `<HOME>/.claude/.credentials.json`
  - `codex`   → `~/.codex/auth.json` → `<HOME>/.codex/auth.json`
  - `gemini`  → `~/.gemini/` auth (link the auth file, not the whole dir)
- Writes a minimal git identity with `[credential]\n\thelper =` to **reset** the credential-helper
  list (swairm's lesson: a system credential helper — e.g. osxkeychain — lives outside the
  blanched HOME and would otherwise leak).
- **Excludes by construction:** `~/.config/gh`, `~/.git-credentials`, `~/.netrc`, `~/.ssh` — never
  linked, so unreachable from the disposable HOME.

```
build_isolated_env(source: Mapping[str,str], home: Path, provider: str) -> dict[str,str]
```
- Pure. Starts from the existing billing scrub, then **forces** `env["HOME"] = str(home)` (never
  from `source` — that is what cuts on-disk cred access), applies the provider namespace scrub
  (generalises `_scrub_grok_namespace` so `*_HOME`/`*_AUTH_PATH` redirects can't bypass the HOME).
- `build_isolated_env` / the path builders are pure & unit-testable; the symlink + subprocess are
  injectable seams (same discipline as grok and swairm).

**Wire-in sites (#2a):** `providers.py:run_provider`, `claude-service` (3 sites),
`gpt-service`, `orchestrator.py:452`, and the `exec_runners/*.sh` shells (which today inherit the
full env + HOME with **no scrub at all** — highest-leverage, lowest-risk fix). grok is already
done; leave it.

### #2a testing (mandatory — this is critical path)
- Unit: `build_isolated_env` forces HOME, drops gh/git keys, keeps only allowlisted env; path
  builders pure; symlink helper idempotent / replaces stale / never clobbers a real file.
- **Real-spawn smoke test** (the load-bearing one): spawn a real `claude -p "print ok"` under an
  isolated HOME and assert **both** (a) it still authenticates and returns output, **and**
  (b) `gh auth token` / reading `~/.config/gh` **fails** from inside that env. Present ≠ isolated —
  prove the negative.

### #2a caveat — blast radius
Spawns are the critical path. #2a is built on a branch, TDD, PR — **not merged/deployed** until the
commander reviews. A disposable-HOME bug that breaks auth would break spawns; the real-spawn smoke
test is the guard, human review is the gate.

### #2b design (SPEC only) — interactive cell membrane
Isolate `spawn.py` claude/codex/agy membranes. Requires the disposable HOME to *also* carry what
an interactive cell needs: `settings.json` (hooks — SessionStart drain, inbox-watcher), MCP config,
`CLAUDE.md`/memory. Options to decide at build time: (i) symlink the operator's `~/.claude/settings.json`
read-only into the isolated HOME (keeps hooks, still drops gh); (ii) generate a per-cell settings
from a template (also resolves #20 shared-settings contamination — a bonus). Open question:
interaction with `~/.claude.json` (project history/config). Delicate → its own spec + plan.

---

## Pattern #3 — Untrusted-repo preflight (SPEC only; no consumer today)

### Problem
When an agent reads a repo/worktree it did **not** author, two hijack surfaces exist:
1. A committed project `.claude/settings.json` can re-authorize tools for the reading agent.
2. A poisoned `.git/config` (`diff.external`, `*.textconv`, `core.hookspath`, `core.fsmonitor`,
   `filter.*.clean/smudge`, `alias.{diff,log,show,status}`) turns a plain `git diff` into code
   execution.
swairm's Queen defends both: run with `--setting-sources user`, and a `git config --list` probe
(executes nothing) matched against a poison regex — poisoned ⇒ abstain, no verdict.

### swarph reality
No reviewer exists; `--setting-sources` is used nowhere. The only agent that reads an un-authored
checkout today is a **spawned cell itself** (Pattern #2), which inherits the checkout's git config
with no guard. So #3 has no reviewer to wrap — it is **net-new infrastructure**.

### Design (reusable, consumer-agnostic)
A pure module `untrusted_repo_preflight` in swarph-shared:
- `git_config_is_poisoned(config_list_output: str) -> str | None` — pure; returns the offending
  key or None. Regex ported verbatim from swairm's `_POISONED_GIT_RE` (battle-tested).
- `preflight(workdir, run_git) -> None` — runs `git config --list` (safe probe), raises on poison;
  optional non-empty-diff-vs-base check (swairm's I3 lesson: a rebuilt worktree at base has an
  empty diff and would be "approved" unread).
- `safe_reader_flags() -> list[str]` — `["--setting-sources", "user", "--disallowedTools",
  "Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch"]` for any `claude -p` that reads an
  untrusted repo.

### Consumers (future)
(a) A `--untrusted` mode on the #2a helper — a spawn told it is reading an un-authored checkout runs
the preflight + `safe_reader_flags`. (b) Any future swarph reviewer/verify flow. Ship the helper +
tests now (spec), wire when a consumer lands. **No silent no-op:** the helper is inert until a
caller opts in — documented as such.

---

## Pattern #1 — Orphaned-claim recovery (SPEC only; reframed)

### Problem (the true gap)
Durable state is solid; **recovery is missing**:
- `claude_tasks`: a task left `in_progress` by a dead claimer has **no reaper** — stalls forever.
- `gpu_jobs`: `POST /gpu/jobs/reclaim-stale` exists but has **no automated caller** (unlike
  `council_jobs`, which a 5-min cron reclaims).
- **Duplicate hazard:** both reclaim paths reset by `claimed_at` age *without confirming the worker
  is dead* → reclaim-while-alive = double execution. (This is the same class as
  [[feedback_broken_channel_cannot_carry_its_stop_signal]] — act on the true signal, not a proxy.)

### Design — swarph-native, better than the reference
swairm reclaims by age only (it has no liveness signal). swarph **does** have one: the watchdog
activity-marker / **peer-health `last_health` (#26)**. So:
- **`claude_tasks` reaper:** a `POST /tasks/reclaim-stale` endpoint (mirror the council reclaimer),
  called by a 5-min cron. Resets `in_progress` → `queued` past a staleness threshold, bumps
  `attempts`, records `last_error="reclaimed: claimer presumed dead"`.
- **`gpu_jobs`:** wire the existing `reclaim-stale` endpoint to the same cron cadence.
- **Liveness-aware reclaim (the upgrade):** before reclaiming a claim owned by a named cell, check
  that cell's `last_health`/`last_seen`. If the worker is **provably alive** (fresh health within
  the window), **do not reclaim** — only age *plus* absent liveness reclaims. Closes the duplicate
  hazard age-only can't.
- Idempotency preserved: reclaim is `BEGIN IMMEDIATE` + guarded `UPDATE … WHERE status='in_progress'
  AND claimed_at < :cutoff RETURNING`, so two reclaimers can't double-reset.

### Scope note
mesh-gateway change (two repos: `mesh-gateway/server.py` deployed + the `swarph-cli/.../gateway`
mirror — keep them in lockstep, per the divergence lesson [[project_two_gateways_diverged]]).
Deploy is commander-gated. Spec now; build on the commander's go.

---

## Testing strategy (all patterns)
TDD throughout (`venv/bin/python -m pytest`). Pure functions (env builders, poison regex, path
builders, reclaim SQL predicate) unit-tested; subprocess/symlink/DB are injectable seams. The
non-negotiable test is #2a's real-spawn smoke test — proving the credential negative, not just
presence.

## Build order
1. **#2a** — credential isolation for headless spawns (this branch: `feat/cell-credential-isolation`).
2. **#1** — orphaned-claim reaper + liveness-aware reclaim (on the commander's go; touches gateway).
3. **#2b** — interactive-cell HOME isolation (delicate; own spec/plan).
4. **#3** — untrusted-repo preflight helper (ship inert; wire when a consumer lands).
