# Changelog

Notable changes to `swarph-cli`. Earlier history: `git log`.

## 0.39.3 — 2026-07-27
- **`deploy/monitor/swarph-monitor.service`** — a **silent** DM monitor unit. The
  shipped sidecar ran `tmux send-keys -t <pane> "check mesh" Enter` on every DM:
  it typed into the cell's pane, cost a full turn per message, and worked only
  while the pane existed. The new unit runs the monitor `--deliver pull
  --foreground` under systemd and pokes nothing.
  - **SILENT MEANS YOU MUST PULL.** Nothing will type at you any more. Wire
    `swarph monitor status` into a SessionStart hook (or call
    `scripts/ensure_monitor.sh`), or a cell that relied on the `check mesh`
    prompt to be woken will simply go quiet.
  - `monitor start` is idempotent via pidfile, so a hook calling it is a no-op
    while the unit runs — they compose rather than compete.
  - systemd **owns** the process: a crash restarts within `RestartSec`. A
    `Type=oneshot` + timer supervisor was tried first and failed —
    `KillMode=control-group` reaped the detached monitor one second after the
    oneshot exited, while logging "started" as a success.
- **`deploy/sidecar/swarph-mesh-sidecar.service` — DEPRECATED IN PLACE**, not
  removed: peers are running it and an existing install must not break.
- **`scripts/ensure_monitor.sh`** — check status, start if down, report pending.
  Never fails its caller; a hook that can block a session is worse than the
  deafness it prevents.

## 0.39.2 — 2026-07-26
- **fix(mesh):** `--token-file` is now **one parser**. It previously did
  `read_text().strip()` and returned the ENTIRE FILE, so pointing it at an
  env-style file — the shape the shipped systemd unit, the README and peers'
  docs all document — put comments and unrelated variables into the
  `Authorization` header. An em-dash in a *comment* crashed a peer's monitor
  with a latin-1 codec error. `swarph daemon` read the same flag through a
  different, correct parser: **one flag, two parsers**, diverging silently
  where both happened to work. Now accepts env-style
  `MESH_GATEWAY_TOKEN=<token>` (quotes stripped, comments and blanks skipped,
  unrelated keys ignored) or a bare token line, and a test asserts the two
  readers **agree** so the divergence cannot recur at the next call site.
  - Not introduced in 0.39.1: that release added a *foreground* call site,
    where the same failure previously died into `monitor.log` while the parent
    exited 0. It made a pre-existing silent failure loud.
- **fix(mesh):** a token that cannot go in an HTTP header now raises a **named**
  error giving the file, line, character and codepoint, instead of a latin-1
  traceback deep in `http/client.py` — which points the reader at the HTTP
  layer for what is a malformed config file.
- **test:** shipped `*.service` units are now parsed in CI, and every `swarph
  <verb> --flags` they prescribe must exist and be accepted. Catches a shipped
  unit invoking a verb or flag the CLI dropped — which breaks every deployment
  using it at next restart, silently.

## 0.39.1 — 2026-07-26
- **fix(monitor):** refuse a **derived** identity that is not a registered peer.
  `self_name` falls back to the state dir BASENAME, so
  `swarph monitor start --state-dir /var/lib/swarph/droplet-monitor` produced a
  monitor for the peer `droplet-monitor` — which does not exist. Nothing can be
  addressed to an unregistered peer, so it polled a nonexistent inbox, saw zero
  DMs forever, and reported itself **running and healthy**. Silent deafness
  reintroduced through *configuration* rather than code. Found by peer cell
  `droplet` within minutes of installing 0.39.0.
  - Refuses only on a **positive** "not a peer" answer: unreachable gateway,
    non-200, unexpected shape and **empty peer list** all warn and proceed.
    `start` must stay safe to call unconditionally from a hook, and an empty
    list is what a half-initialised gateway returns — refusing on it would stop
    every monitor in the fleet at once.
  - Only the **derived** path refuses. An explicit `--as` / `$SWARPH_SELF` is a
    deliberate claim and may be pre-staging a peer not yet registered; that
    warns and continues.
  - The check runs *after* the already-running fast path, which is the hook path
    and must stay free and silent.

## 0.39.0 — 2026-07-26
- **`swarph monitor`** — pluggable DM delivery, PULL-first (card #122).
  - **The state split.** An *observation cursor* (what the monitor has READ,
    advances on observation, never gated on anything downstream) and a
    *per-sink delivery ledger* (advances on successful delivery, may lag
    arbitrarily). Novelty is a property of a **sink's ledger**, not of the
    cursor — so a late-attached sink replays from `inbox.log` rather than
    finding its mail already consumed.
  - `--deliver pull` (default; ledger advances on **ACK**), `tmux:<target>`,
    `stdout`, `none`. Repeatable — several sinks, independent ledgers.
    `webhook:` **exits non-zero naming its gate**; a held feature that silently
    no-ops rots into a phantom capability someone believes is delivering.
  - `start` is idempotent and safe to call unconditionally from a hook (quiet
    and fast when already running). `status` exits 0 = nothing pending,
    1 = DMs pending, 2 = not running, so a hook can self-heal and notify in one
    line. `--brief` prints nothing when there is nothing.
  - Under `--deliver none` `status` reports **"unread: CANNOT REPORT"**, never
    "0 unread" — an absence that reads as evidence is the defect being fixed.
  - **Refuses to share a state dir with a live `swarph daemon`.** Both write
    `cursor.json` + `inbox.log`; two writers on one cursor lose or repeat DMs
    silently. Detected by a daemon pidfile *and* by `tasks_snapshot` in the
    cursor, which is what catches daemons already running.
  - `swarph mesh sidecar` keeps working as a deprecated alias (notice on
    stderr — stdout is a sink).

- **fix(mesh):** the sidecar read cursor no longer shares a failure mode with
  wake delivery. It previously advanced only inside `if _tmux_wake(...)`, so a
  dead tmux pane froze it forever and every poll re-selected and re-logged the
  same messages, with no backoff and no alarm. Waking is delivery; the cursor
  is bookkeeping. Failed wakes are now visible instead of silent.

- **fix(cli):** a mistyped verb errors instead of billing an LLM call.
  `swarph inbox` (the real verb is `swarph mesh inbox`) fell through to the
  one-shot path as `prompt="inbox"` and paid a provider to answer a question
  nobody asked. Close typos get suggestions; `swarph -- <word>` still sends a
  single-word prompt.

## 0.38.1 — 2026-07-24
- **docs:** README `swarph bench` section + this changelog (0.38.0 shipped the
  feature without them; README is the PyPI long-description, so this refreshes
  the PyPI page). No code change.

## 0.38.0 — 2026-07-24
- **`swarph bench`** — a deterministic LLM benchmark-pack runner (card #101).
  - `bench run` — N-way model showdown on a pack → confusion-matrix report
    (per-class hit-rates + ground-truth distance + tokens + metered-$ + latency
    + parse-fails), ranked by (distance, cost). Credential **preflight** skips
    key-less models cleanly instead of a mid-run 401; `--strict` aborts.
  - `bench validate` — four trust gates: schema/integrity, answer-leak scan,
    discrimination, and context-calibration guidance.
  - `bench add` — validates + self-registers a pack at `packs/<theme>.json`
    from its own header (no manual name); refuses on leak/schema failure.
  - `bench prices` — the full LiteLLM price list (~1500+ models), a shared cache.
  - Task types: numeric / categorical / ranking / text. Packs are data-only;
    the scoring engine is fixed and shared. `[bench]` extra pulls `google-genai`.
  - Spec + reference impl by droplet; ported into swarph-cli under review.
