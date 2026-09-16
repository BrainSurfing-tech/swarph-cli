# OpenCode membrane — design

**Date:** 2026-09-11
**Status:** spec, for peer review before plan
**Owner:** commander (author), for handoff to the membrane reviewer
**Board:** opencode-membrane, card to be filed
**Builds on:** the cursor membrane (`test_cursor_membrane.py`, the newest template) and the
`#247`/muse release-ordering rule (`spawn.py`'s `VALID_PROVIDERS ⊆ MEMBRANES` subset guard).

## Goal

Make [OpenCode](https://opencode.ai) (`~/.opencode/bin/opencode`, v1.18.30 on this box) a first-class
swarph **membrane** — the 8th, alongside `claude` / `codex` / `cursor` / `grok` / `muse` / `vibe` /
`antigravity`. Concretely: `swarph init --provider opencode` scaffolds a cell, `swarph spawn <cell>`
execs the opencode TUI in a named tmux session with an isolated + identity-stamped env, and the same
four hook products the other agentic CLIs get (SessionStart starter injection, DM wake, post-compact
recall, emit-on-write) fire via an opencode plugin.

## What "wrap" means here, and why opencode is not like the other seven

swarph does not shim a CLI on `$PATH`. Each provider is a `ProviderMembrane` subclass in
`src/swarph_cli/commands/spawn.py` that answers three questions:

1. **argv** — how to *invoke* the TUI, including the resume-vs-fresh decision.
2. **env** — the billing-redirect scrub + `SWARPH_SPAWN=1` + `SWARPH_SELF`, plus a provider-specific
   state-isolation story so the cell's sessions/credentials never mix with the operator's.
3. **hooks** — how the CLI's own lifecycle events reach back into swarph (`hook-output`,
   `wake-hook-output`, post-compact recall, memory-emit).

For claude/codex/cursor the hook answers are a JSON `{"type":"command","command":"swarph ..."}` entry in
that CLI's config, with a `commandWindows` twin. **opencode has no command-hook surface.** Its hook
surface is a **JavaScript/TypeScript plugin** — a JS module placed in
`~/.config/opencode/plugins/` (global) or `.opencode/plugins/` (project), or named in
`opencode.json` via `"plugin": [...]`. Every plugin exports a factory returning a `Hooks` object keyed
by event. So two of the three questions stay identical to the other membranes and one is structurally
new; the design below splits them.

## Grounded opencode facts (read from SDK types + docs, 2026-09-11)

- **Config** — global `~/.config/opencode/opencode.json` (+ `.jsonc`), project `opencode.json`,
  merged (not replaced), with `OPENCODE_CONFIG` / `OPENCODE_CONFIG_DIR` / `OPENCODE_CONFIG_CONTENT`
  env overrides. `.opencode/` and `~/.config/opencode/` use **plural** subdirs: `agents/`, `commands/`,
  `plugins/`, `skills/`, `tools/`, `themes/`, `modes/`.
- **Sessions** — stored in a sqlite DB at `~/.local/share/opencode/opencode.db` (`opencode db path`);
  auth at `~/.local/share/opencode/auth.json`; runtime state at `~/.local/state/opencode`
  (locks, model.json). `opencode session list` / `delete` / `export` exist. This is the
  resume-by-discovery family, **not** claude's pinned UUID.
- **CLI** — `opencode` (TUI, defaults), `opencode run "<prompt>"` (one-shot), `opencode serve` /
  `web` / `acp`. Flags: `--continue`/`-c` (continue last), `--session`/`-s <id>`, `--fork`,
  `--prompt`, `--model`/`-m provider/model`, `--agent`, `--auto` (auto-approve). No `-C`/`--cwd`
  flag; working dir is positional/traversed.
- **Plugin hooks** (the `Hooks` type, `@opencode-ai/plugin`): `event` (receives the whole event union),
  plus typed hooks `chat.message`, `chat.params`, `chat.headers`, `permission.ask`,
  `command.execute.before`, `tool.execute.before`, `tool.execute.after`, `shell.env`,
  `experimental.chat.system.transform`, `experimental.chat.messages.transform`,
  `experimental.session.compacting`, `experimental.compaction.autocontinue`,
  `experimental.text.complete`, `tool.definition`, `config`, `tool` (custom), `auth`, `provider`.
- **Event union** (`@opencode-ai/sdk`): `session.created`, `session.updated`, `session.deleted`,
  `session.idle`, `session.compacted`, `session.status`, `session.error`, `session.diff`,
  `message.created`/`updated`/`removed`, `message.part.*`, `tool.execute.before`/`after`,
  `permission.asked`/`replied`, `file.edited`, `command.executed`, `todo.updated`, `shell.env`, `busy`/`idle`.
- **Instructions** — opencode reads `AGENTS.md` and, by default, `~/.claude/CLAUDE.md`
  (`OPENCODE_DISABLE_CLAUDE_CODE=1` + `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT=1` disable the latter).
  Config key `instructions: ["...", ".cursor/rules/*.md"]` adds more.

## The three structural differences from existing membranes

1. **No command hooks → a shipped JS plugin.** All four hook products route through one opencode
   plugin that shells out to the existing swarph verbs. The plugin is a *package payload*
   (`importlib.resources`), not a source-tree path, exactly like the postcompact `.sh` payloads
   (`install_postcompact_hook.py`, the 0.39.3 undeclared-package-data lesson).
2. **No `--cwd` flag → chdir is load-bearing, paths stay relative.** opencode's TUI and `run` resolve
   the workspace from the current directory, so `launch()`'s existing `os.chdir(cell.cwd)` is the
   mechanism and **no path-shaped string crosses the exec boundary** (#314's lesson). If the session
   must be continued explicitly, `--session <id>` is passed — never a path.
3. **Resume-by-discovery, and the discovery source is a sqlite DB.** No pinned UUID. Continuity is
   opencode's own `--continue`/`--session`, discovered by reading `opencode session list --format json`,
   guarded the way `_cursor_has_prior_chat` guards `--continue` against an empty store.

## Design — Part 1: the membrane (`spawn.py`)

`OpencodeMembrane(ProviderMembrane)` with:

- `name = "opencode"`.
- `uses_pinned_session() -> False` (resume-by-discovery family, like codex/cursor/muse/grok/vibe).
- `build_argv()` — a module-level `_build_opencode_argv(cell, no_starter, passthrough)`:
  - base `["opencode"]`;
  - `--continue` **only when** a prior session exists for the cell's workspace (probe #P1 below);
    otherwise a bare `opencode` (fresh TUI, no flag — the `--continue`-on-empty trap is what killed
    cursor's first spawn);
  - `--auto` is the autonomy posture but **not a membrane default** — it widens tool approval and
    belongs in `cell.yaml` (grosk's `always_approve`, vibe/cursor's "membrane must not widen approval")
    → omit from argv; operator passes it via `-- --auto` passthrough;
  - starter prompt: opencode's TUI has no `--append-system-prompt`/`--system-prompt-override`; the
    starter reaches the cell through **configuration** (the `experimental.chat.system.transform`
    plugin hook below), not argv. `build_argv` therefore appends nothing for the starter, matching
    codex (which delivers via AGENTS.md rather than a flag);
  - `argv.extend(passthrough)`.
- `resolve_binary()` — `shutil.which("opencode")`, then the standard install locations
  `~/.opencode/bin/opencode` and `~/.local/bin/opencode` (the curl installer's default; npm/bun/brew
  installs resolve via PATH). `binary_not_found_message()` points at `curl -fsSL https://opencode.ai/install | bash`.
- `env_builder` — `_opencode_env(cell)`:
  - `scrub_env_for_subprocess()` base (already strips `OPENAI_API_KEY`, `ANTHROPIC_*`, `*_API_KEY`,
    `*_BASE_URL` — covering the billing-redirect class for opencode's providers too);
  - deny-by-default the `OPENCODE_*` namespace, then set the cell's own
    `OPENCODE_CONFIG_DIR`/`OPENCODE_CONFIG`/`OPENCODE_CONFIG_CONTENT` after (order load-bearing, the
    grok/vibe scrub pattern). **Do this once we know which knob the cell actually reads** — see
    probe #P2, the isolation story is the largest open question;
  - keep `HOME`/`XDG_CONFIG_HOME` intact by default (the vibe/cursor lesson: relocating `$HOME` costs
    the cell `~/.config/swarph/<self>.peer_token`, `~/.swarph/secrets.toml`, brain_ask, the codegraph
    hook), unless probe #P2 proves the data dir can't be isolated any other way;
  - `SWARPH_SPAWN=1` + `SWARPH_SELF=cell.name` come from `_spawn_env_base` (positional, non-optional
    cell — already enforced).
- `apply_task_injection()` — opencode has no flag; the CURRENT_TASK.md restore goes through the plugin's
  system transform (same file-truth as codex's AGENTS.md, but via config instead of a file at spawn).
  Default no-op otherwise.
- `memory_sync_files()` — `AGENTS.md` + the workspace `opencode.json`-declared instruction/rule files;
  **never** `~/.local/share/opencode/auth.json` (credential) or a live sqlite DB (torn-artifact, the
  vibe cursor store exclusion). `memory_guard_file()` → `cell.cwd / "AGENTS.md"`.
- `pane_busy_markers` / `pane_composer_prefixes` / `pane_empty_placeholders` — **left unimplemented
  (base `()`) until probe #P3**; unknown pane state defers as busy, fail-closed, never defaulted to
  another TUI's markers.

Registration, in the correct order:
- `MEMBRANES["opencode"] = OpencodeMembrane()` in `spawn.py`.
- `CLI_ENABLED_PROVIDERS = VALID_PROVIDERS | {"muse", "cursor", "opencode"}` in `cell.py`.
- `_validate_routing`'s `provider_native` map gains `"opencode": "opencode"`.
- `init.py` `_LLM_BLURBS["opencode"] = "OpenCode — opencode membrane (plugins/events)"`.
- `pane_probe.py` `PANE_PREDICATES["opencode"]` (after #P3).

## Design — Part 2: the hook products via one plugin

Clone `install_postcompact_hook.py`'s shape into a new verb `install-opencode-plugin` (name TBD —
`install-opencode-hooks` collides with nothing; the codex precedent is `install-codex-hooks`):

- A package payload `swarph_cli/payloads/opencode/plugin.js` — a plain ESM module (no build step) that
  loads in opencode's plugin runner. It reads env baked at install time (`SWARPH_SELF`,
  `SWARPH_MEMORY_DIR`, the pinned interpreter path) and shells to `python -m swarph_cli <verb>`,
  capturing stdout and returning it in the shape each hook expects.
- An installer with the usual contract: `--scope user|project`, `--cell` (refused with `--scope user`
  if global — card #527's shape), `--uninstall`, `--dry-run`, write-landed assert (#527 task 3).
  It writes the plugin file into `<dir>/plugins/` (or the cell's `OPENCODE_CONFIG_DIR/plugins/`) and
  registers it in `opencode.json` via `"plugin": ["./plugins/swarph-opencode.js"]` — **or**, simpler,
  relies on "local files auto-load at startup" (plugins in the dir need no config entry) and only
  ships the file. Probe #P4 decides.

Hook product → opencode event mapping:

| swarph product | opencode event | shape |
|---|---|---|
| `hook-output` (starter, SessionStart) | `experimental.chat.system.transform` | append to `output.system[]` — the direct "session context" analog |
| `wake-hook-output` (DM wake arm) | `experimental.chat.system.transform` (guarded: once per session) | append the arm-instruction once; the unknown-harness refusal reuses `_emit`'s dual-envelope |
| post-compact recall | `experimental.session.compacting` | `output.context.push(<7-day timeline>)` — the literal "inject context at compaction" hook |
| `memory-emit-hook` (emit-on-write) | `tool.execute.after` (matcher: `edit`/`write`/`bash`, etc.) | call emit-on-write on writes; the camelCase→`tool_input` adapter mirrors grok's |
| `hooks touch-activity` | `tool.execute.before` or the `busy`/`idle` event | touch the activity marker |

`wake_hook_output.py` gains `opencode` in the `_ARM_HARNESSES` family (the wake lives in the harness,
emitted as the tail-`dm_notify_filter` instruction), and `install_wake_hook.py`/`wake_hook_output.py`/
`install_postcompact_hook.py` gain an `opencode` branch in `_KNOWN_HARNESSES`, `_config_path`,
`_event_key`, `_new_entry`/`_registration`. Whether the wake and postcompact routes share the one
plugin or use separate plugin files should mirror the existing per-product split, not a merge.

## Non-goals

- No `opencode` entry in `swarph_shared.VALID_PROVIDERS` yet (release ordering — see below).
- No `opencode run`/`--print` DM-delivery lane (cursor's #454 analogue). The membrane is the TUI cell;
  a headless delivery seam is a **separate later card**, prompted explicitly by "what wakes the cell".
- No `opencode serve`/ACP cell shape; the durable session is the tmux pane like every other membrane.

## Release ordering (the #247 rule, non-negotiable)

The membrane ships in a **released swarph-cli** (registered in `MEMBRANES` + `CLI_ENABLED_PROVIDERS`)
**before** `opencode` enters `swarph_shared.VALID_PROVIDERS`. `spawn.py`'s guard is a subset check, so
this direction is inert; reversed, it raises at import and kills `swarph spawn` for every fresh
install (2026-08-05, ~5h). Same discipline the cursor membrane documents verbatim.

## Open questions / probes required (ground by execution, never assume)

- **#P1 — session discovery.** Does `opencode session list --format json` expose the most-recent
  session for a given workspace directory, and is that the right predicate for `--continue`? What
  does a bare `opencode --continue` do on an empty DB — fresh start or error? Determines the
  `_opencode_has_prior_session` guard.
- **#P2 — data isolation.** Does opencode honor `XDG_DATA_HOME` / `XDG_STATE_HOME` for
  `opencode.db` + `auth.json`? If not, is there an `OPENCODE_*` override, or do we isolate via a
  relocated `$HOME` (grok's approach) and accept losing `Path.home()`-relative mesh identity? This is
  the single largest design decision and must be settled by a strace/exec probe, not argument.
- **#P3 — TUI idle markers.** Record the real opencode TUI's busy/composer/empty markers to fill
  `PANE_PREDICATES["opencode"]`. Empty is a valid, defensible result if the TUI's walls are
  indistinguishable — but it must be *measured*, never defaulted.
- **#P4 — plugin registration.** Confirmed: does a file in `~/.config/opencode/plugins/` auto-load with
  no `opencode.json` edit (docs say "loaded automatically at startup")? If so, the installer ships the
  file and skips the config edit; if a `plugin` entry is required, the installer writes it.
- **#P5 — `experimental.chat.system.transform` availability.** It is `experimental.*`; confirm it fires
  in the current 1.18.x and whether `OPENCODE_EXPERIMENTAL` must be set. If it's gated/broken, the
  fallback is `chat.message` (append to `output.message`) or `chat.params`.

## Testing

`tests/test_opencode_membrane.py` mirroring `test_cursor_membrane.py`: argv (no `--auto`, `--continue`
only when guarded, no path-shaped string, passthrough), env (namespace deny + cell overrides after,
`SWARPH_SELF` stamp survives), binary resolution fallbacks, and the empty-DB resume guard. Plus:
`install-opencode-plugin` idempotency/uninstall/dry-run/#527 cell-in-global refusal; `wake_hook_output`
`opencode` arm-family emission; post-compact recall wiring; memory-sync `AGENTS.md` inclusion and
auth.json/DB exclusion. All plugin-side behavior is asserted as *string output of the plugin script*,
not live opencode execution (no opencode binary in CI, same posture as codex/cursor tests).

## Scope / constraints

- Repos: `swarph-cli` only (no `swarph-shared` change this release).
- Files: `spawn.py`, `cell.py`, `init.py`, `pane_probe.py`, `commands/install_opencode_plugin.py` (new),
  `commands/install_wake_hook.py`, `commands/wake_hook_output.py`, `commands/install_postcompact_hook.py`,
  `main.py` (verb entry), `payloads/opencode/*`, plus tests.
- Stdlib-only on the Python side; the opencode payload is vanilla ESM JS (no npm deps at the cell).
- Changes live spawn + hook behavior → reviewed PR, **commander-gated** merge (live-spawn rule).

## Probe findings (2026-09-16, MEASURED against opencode 1.18.30)

Each `#P*` above is resolved where a probe ran; the rest carry their unresolved status.

- **#P1 (resolved).** `opencode --pure session list --format json` rows carry `id` + `directory` +
  `updated`. Residency is per-directory, so `_opencode_prior_session` discovers the newest matching
  session and resumes by `--session <id>` — never global `--continue`. (The empty-store `--continue` rc
  question is moot: `--continue` is not used.)
- **#P2 (resolved).** `XDG_DATA_HOME=/tmp/x opencode --pure db path` → `/tmp/x/opencode/opencode.db`;
  `XDG_CONFIG_HOME=/tmp/x opencode --pure debug config` → `"plugin": []`. Both XDG vars are honored, so
  the cell isolates DB + auth via `XDG_DATA_HOME` and plugins via `XDG_CONFIG_HOME` — no fake `$HOME`,
  and opencode auth (data-dir scoped) is untouched by the config relocation.
- **#P4 (resolved).** A bare JS file in the config dir's plugin tree auto-loads with no `opencode.json`
  `plugin` entry (the operator's own provider plugin loads against a config of only `$schema`, and the
  probe plugin's `init` fired). The installer ships the file only; no config edit.
- **#P5 (resolved for the load-bearing hook).** `experimental.chat.system.transform` FIRES and fires
  WITHOUT `OPENCODE_EXPERIMENTAL=1` (observed 3× across one turn); `tool.execute.before` fires
  (`hook:tool.before:bash`); the `event` hook fires (`session.created` / `session.idle`).
  `experimental.session.compacting` was not triggered (a short turn never compacts) — typed-hook is
  SUBJECT-to-first-compaction, not assumed.
- **#P3 (open).** TUI idle/composer markers remain unrecorded; `PANE_PREDICATES["opencode"]` stays
  empty (fail-closed: unknown ⇒ busy ⇒ defer) until a real TUI capture.
- **Follow-up (deliberately out of scope of this PR):** emit-on-write (`memory-emit-hook`) needs a shape
  adapter (opencode `tool.args` ≠ claude `tool_input.file_path`) and a memory-location decision; the
  plugin ships starter + wake-arm + post-compact recall + touch-activity only.

