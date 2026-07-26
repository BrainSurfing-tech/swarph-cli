# Changelog

Notable changes to `swarph-cli`. Earlier history: `git log`.

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
