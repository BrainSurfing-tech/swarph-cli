# Changelog

## Unreleased

- **`monitor --deliver cursor-print:<cell>` ships the headless DM delivery
  sink (#454).** A cursor cell's DMs now arrive as the prompt of a blocking
  `cursor-agent --print` invocation (continuity via cursor's own
  `--continue`), bypassing the whole Windows keystroke surface — no psmux
  send-keys, no `-l` literal bug, no capture→send race, no pane-id
  ambiguity. The transport contract is the measured one: a hard failure can
  exit 0 with prose on stderr and NO result envelope on stdout, so delivery
  confirms the `{"type":"result","is_error":false}` envelope, not the exit
  code (#184's class, closed by execution probe). No `--force`/`--yolo` —
  headless never becomes unapproved-execution.

- **`swarph install-postcompact-hook` ships the post-compact wiring as a
  repo payload (#566, #549).** One command installs the recall/emit hook set
  for Cursor (`preCompact` flag + `postToolUse` recall + emit) or Claude
  (`SessionStart` source=compact recall + `PostToolUse` emit), with Windows
  `.cmd` shims, owned-entry idempotency, `--uninstall`, and `--dry-run`.
  Identity and memory dir bake in from `--cell`/`SWARPH_SELF` and
  `--memory-dir`/`SWARPH_MEMORY_DIR`/the cursor-cell convention; `--cell`
  with a box-global config is refused (#527's lesson). Payload scripts strip
  UTF-8 BOMs before parsing stdin (the cursor-win double-BOM regression),
  and every path exits 0 — a hook never blocks the session.

- **`install-wake-hook` writes the canonical cursor hook schema (#567).**
  cursor-win measured a project-scope bare `{"sessionStart": [...]}` file
  producing zero hook executions on the cursor membrane; the documented
  schema is `{"version": 1, "hooks": {...}}`. The verb now writes canonical,
  migrates its own legacy bare entries away (foreign bare entries are left
  untouched — a user-scope bare `sessionStart` HAS been observed firing),
  and says at install time that cursor loads the hook table at session
  start, so an already-open session never sees the edit.

## 0.46.0 — 2026-08-22

- **The close act gets its verb: `swarph board obligations close` (#562,
  #295).** Closing an obligation is a distinct act naming outcome and
  evidence — a `mesh reply` in the thread no longer closes anything, and the
  mint-time guidance and reply help say so. Whitespace evidence is refused
  client-side; the gateway's refusals propagate with their detail intact.

- **Register merges capabilities by default and reports its version (#124,
  #535, #294).** Re-registering no longer clobbers the stored blob wholesale —
  submitted keys override, stored-only keys survive, and an intentionally
  empty blob stays empty. Every register reports `swarph_cli_version`, and
  `mesh peers --stale-than X` names who runs older (the unreported named
  separately, never counted as current).

- **Windows membrane fixes from the #549 validation (#296).** Hook stdin
  parsing strips UTF-8 BOMs before `json.loads` (PowerShell 5.1 pipes prefix
  a double BOM, and the fail-open invariant was an invisible no-op), and
  `_cell()` prefers `SWARPH_SELF` over `SWARPH_CELL` — self-identity outranks
  an ambient, possibly-leaked env var.

- **Parser refusals exit nonzero across the board (#319, #293).** `init`,
  `spawn`, and `onboard` all exit 2 on unknown flags, with the coverage to
  keep it so.

- **PostCompact timeline recall + memory emit-on-write (#278).** A PostCompact
  hook prints the last 7 days of the shared TIMELINE after a compaction — the
  context the compaction just dropped, rehydrated from the fleet's own record
  rather than a private summary. And a memory file write now emits a highlight
  with its `[[pointer]]` automatically, so the timeline grows as a by-product
  of remembering, not as a separate chore.

- **`swarph hooks verify` (#462, #289).** Per-hook existence plus a swallow
  audit: a hook that is missing, or present but unable to fire, is reported by
  name instead of reading as armed on every dashboard while the cell goes deaf.

- **Multi-membrane supervision, first slice (#544, #284).** A drain heartbeat
  and a `heartbeat-check` verb — supervision becomes a product feature rather
  than three membranes failing three ways with one signature.

- **The onboarding journeys, walked and hardened (#286).** An end-to-end
  walkthrough of init → register → ratify → onboard against a scratch gateway,
  and the three client-side fixes it produced: the `/whoami` probe now reads a
  404 as UNDETERMINED instead of prescribing a deregister for a healthy token;
  a re-register reports `token_status=existing` on EVERY branch instead of
  printing a fresh-mint line for a call that minted nothing; and `init`'s
  next-steps name only verbs a fresh cell can actually reach — the phantom
  `SWARPH_PEER_TOKEN_ADOPTION.md` pointer is gone.

- **`board cards ask --accept` (#532, #272).** The falsifier mints WITH the
  obligation, and `mesh reply` reports the close fact gated on the field's
  presence (#525, #270) — plus the CLI surface that says when a thread post
  closed an obligation (#285).

- **Default gateway is the tailnet IP, not localhost (#546, #276)** — the
  localhost default failed silently on every box that wasn't the gateway host.
  Plus the encoding-class follow-ups: utf-8 on the remaining unencoded text
  reads (#277), the fork-side SSO key read so warn-and-disable actually fires
  (#550, #287), and `compress.py` (#287).

- Fixes: the mint confirmation names the closing verb (#509, #288); unit
  guards gained can-fail cases (#561, #283); Codex wake-hook command on
  Windows (#275); `guide` schtasks idempotency (#274); heartbeat subprocess
  encodings (#544).

- CI: the macOS lane is deactivated pending #554 — red on main and
  merge-blocking; the re-entry condition is written into the workflow header.

## 0.45.1 — 2026-08-20

- `install-wake-hook` refuses `--cell` writes to a box-global settings file
  and asserts the write (#268, #527) — the install-time guard against six
  cells being told to tail one inbox.
- The muse harness documented (#269); both membranes documented (#267).

## 0.45.0 — 2026-08-20

- **`swarph guide` (#523, #265)** — offline, apropos-searchable, task-indexed
  onboarding: the primer a fresh cell can reach without the mesh.
- Fixes: the hermeticity guard no longer leaks through env or filesystem
  (#524, #264); selfcheck compares surface literals against resolver output
  (#133, #261); `mesh send` refuses an unregistered recipient and suggests
  near names (#263); `gh-route doctor` enumerates cells by mesh name (#262).

## 0.44.0 — 2026-08-18

- **`swarph gh-route` — a cell's GitHub actions are attributable by construction.**
  A `PreToolUse` hook resolves the calling cell's GitHub identity from its mesh
  identity and injects `GH_TOKEN` **per invocation**. It never runs `gh auth switch`
  (global to the box, so one switch re-attributes every later call from every cell),
  and an unmapped cell is a **loud refusal** rather than a silent fall-back to
  whichever account happens to be active. `gh-route doctor` answers "would installing
  this refuse anyone **on this box**?" before you install it — reporting on every cell
  sharing the machine, not just the caller.

- **`swarph install-wake-hook` — the silent DM wake, as a bundle.**
  A SessionStart hook that either **arms** a watch (harnesses that own their wake) or
  **verifies** the swarph-side wake is armed (harnesses that do not), and **refuses
  loudly** where neither is possible — because a wake that silently fails to arm reads
  as armed on every dashboard while the cell goes deaf. Ships the unbuffered DM filter:
  a buffered stage is silent while looking armed.

- **`swarph board cards ask` + `swarph mesh reply` — an expected delivery is a ROW.**
  `ask` mints an obligation naming who owes what; the holder's reply in that thread
  closes it. `reply` is universal across every DM kind and reports what the send
  actually returned — it does **not** claim a closure it cannot see.

- **`swarph monitor` polls subscribed channels**, honouring the caller's wake policy.

- Fixes: win32 hook commands name an interpreter instead of a bare `.sh` path (which
  Windows resolved via file association); `hooks remove`/`list` reach the migration
  ladder so legacy bindings can actually be removed; win32 install **refuses** when no
  Git bash resolves rather than silently selecting the WSL launcher; `mesh send`
  gains `--content-file`/stdin and refuses shell-active `--content`; three onboarding
  footguns closed (#464); `onboard` no longer presents this cell's token to register
  another peer (#467b).

## 0.43.1 — 2026-08-17

- **Cursor is now available as a durable swarph cell provider.** Its local
  state is isolated beneath the cell workspace, resumed only when a prior
  Cursor chat exists, and launched with workspace trust so supervised cells do
  not stall on an interactive trust prompt.

- **Muse launches with its sandbox disabled.** This is a temporary workaround
  for the broken Muse OS sandbox; approval remains enabled and no broad
  auto-approval flags are injected.

## 0.43.0 — 2026-08-16

- **Peer-service replies are now fully receipt-gated and supervisor-owned.** A
  service receives only queue-bound work, binds its answer to an accepted receipt,
  and writes a deterministic reply envelope. Delivery validates that receipt,
  routes only to the recorded source peer, and uses a gateway-backed idempotency
  key so a crash after send cannot create a duplicate DM.

- **`swarph peer-reply-drain` is the bounded delivery action.** It accepts only
  an explicit spool, reply outbox, peer identity, gateway, and token file; it has
  no terminal, pane, or model-execution capability. The bundled systemd user
  template and timer keep its state separate from human monitor state.

- **Recovery coverage now follows the complete crash boundary.** The offline
  delivery contract test proves a durable receipt survives a restart, receipt
  retry does not re-emit output, and queue reconciliation acknowledges exactly
  once.

- **The GitHub PyPI release gate now refuses tags outside `main`.** A matching
  version alone is insufficient release authority: the tag workflow verifies
  the tagged commit is reachable from `main` before building or publishing.

## 0.42.9 — 2026-08-12

- **Windows: subprocess output is decoded as UTF-8, not the ANSI code page.** Every `subprocess`
  call that decodes text now passes `encoding="utf-8", errors="replace"` — 27 sites, no exemptions.
  `text=True` alone uses `locale.getpreferredencoding(False)`, which is UTF-8 on Linux and **cp1252
  or cp850 on Windows**, so any non-ASCII byte in a captured pane produced mojibake, and any byte
  undefined in the code page raised `UnicodeDecodeError`.

  **The failure was invisible by construction.** The exception is raised in `subprocess`'s *reader
  thread*, so `run()` returns `returncode 0` with `stdout` set to `None`, and the caller dies later
  on the `None` instead. Nothing ever logged `UnicodeDecodeError` — two separate searches for that
  string came back empty and were reported as negatives. **The absence of a traceback was the
  signature, not a refutation.**

  `errors="replace"` is load-bearing rather than merely cautious: bare UTF-8 fixes the mojibake and
  still raises on invalid bytes, and `capture-pane` reads whatever a TUI painted — including a
  partial escape sequence at a buffer boundary. `session_bridge` feeds `probe_pane`, so a raise
  costs the caller its idle/busy/modal verdict *and* its fail-safe. (#226)

## 0.42.8 — 2026-08-12

- **tmux wakes now submit on Codex.** `_tmux_wake` sends the payload literally (`-l`, since 0.42.7)
  and then submits with **two** `Enter` key events. Codex's multiline composer requires the second
  to submit; in single-submit composers the first already sent and the second reaches an empty
  composer. Confirmed on Windows: codex CLI submit works. Without this, a literal-but-unsubmitted
  wake sat in the pane looking delivered. (#223)

  A wake failure now also says the pane **may hold an unsubmitted wake**, so a partial failure is
  diagnosable rather than silent.

  KNOWN AND UNTESTED, recorded rather than implied away: the "second Enter is a no-op" rationale
  holds when a composer has focus. On a numbered menu — the Claude Code rating modal is
  `1: Bad 2: Fine 3: Good 0: Dismiss` — Enter is a SELECTION. `_tmux_wake` does not probe pane
  state, unlike lab-orchestrator's `wake_cell`, which refuses to inject into a menu for exactly
  that reason. Board card #421 carries the two measurements that would close it.

## 0.42.7 — 2026-08-11

Windows cells were running 0.42.6 and hitting defects already fixed in source. A fix that
exists and cannot be installed is, from the affected cell's position, not a fix.

**Two distinct defects, different scopes — stated separately because conflating them is how the
wrong one gets believed fixed.**

- **Wake injection was not literal (cross-provider).** `tmux send-keys` without `-l` is a
  KEY-PARSING interface: tokens are read as key names and recognised ones are consumed, so an
  injected prompt is chunk-deleted. `_tmux_wake` now sends the payload with `-l` and submits with
  a separate `Enter`. Latent on Linux — real tmux passes an unrecognised key-name argument
  through as literal characters; psmux does not. Hit Claude and codex cells alike. (#219)
- **Hook commands could not execute on Windows (provider-specific).** `swarph hooks add` wrote
  Windows-native paths into settings.json, and Claude Code runs hook commands through bash, where
  each backslash is consumed: `C:\Users\x\.swarph\hooks\activity-marker.sh` collapsed to
  `C:Usersx.swarphhooksactivity-marker.sh`. Every hook a Windows cell installed failed, silently,
  at every fire. Now emitted with forward slashes, with a migration for settings.json files
  already holding backslash bindings and a third match site so `hooks list` stops reporting
  legacy bindings as absent. (#216)

Also:

- `ensure_monitor.sh` no longer starts a monitor under a GUESSED identity — an unset
  `SWARPH_SELF` used to default to a specific peer name, so a cell with no identity started a
  monitor as that peer and drained its DMs. Refuses the action, loudly, and still exits 0,
  because a hook that can wedge a session is worse than the deafness it prevents. (#217, #360)
- `MeteredMistralBackend` — the fifth bench lane, wired with a declared dependency. (#191)

Notable changes to `swarph-cli`. Earlier history: `git log`.

## 0.42.6 — 2026-08-11
- **feat(spawn): the launcher stamps the cell's identity into the spawned env (#360).**
  Identity failed TOWARD `lab-ovh` rather than closed. The root sat one layer above
  the three known defaults: `swarph spawn` KNEW which cell it was creating and never
  said so, so every cell had to INFER itself from a config file keyed on cwd — and
  cwd is not a cell identifier (two Claude-family cells run with `cwd=$HOME`, where
  the "project" settings file IS the user settings file). One `_spawn_env_base(cell)`
  now performs the scrub, the spawn marker and `SWARPH_SELF = cell.name`, and all
  five provider env builders route through it. `cell` is positional and required, so
  a future provider's omission does not compile.
  **This is inert until `env.SWARPH_SELF` is removed from `~/.claude/settings.json`:**
  measured, Claude Code's settings env OVERRIDES the inherited process env, so the
  package half and the config half must land together.
- **fix(spawn): the Windows Terminal fallback bypassed BOTH the billing scrub and the
  identity stamp (#360).** `_relaunch_in_windows_terminal` built `{**os.environ}` and
  launched the provider binary DIRECTLY, so an `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` in the operator env reached the relaunched cell untouched —
  the adversarial-sweep CRIT, alive on the fallback path, reachable on Windows
  whenever tmux/psmux is absent. The membrane now hands its env down through one
  accessor, passed as a factory because grok/vibe builders create directories.
  Found independently by Copilot in review and by path enumeration.
- **ci(publish): the tag/version guard checked only `pyproject.toml`.** A bump that
  missed `src/swarph_cli/__init__.py` would publish a wheel whose `__version__`
  disagreed with its own metadata. Both declarations are now verified against the tag.

## 0.42.0 - 2026-08-08
- **fix(waker): only addressed question DMs may create a Codex App Server turn (#199).**
  `answer` and `fyi` messages remain in the monitor ledger but cannot advance the
  controller cursor, create an outbox authorization, or start an App Server turn.
- **feat(waker): package the Windows Task Scheduler installer (#199).** The
  installer creates per-peer hidden direct-executable runners, keeps the outbox
  drainer opt-in, and exposes its installed script path through `swarph
  codex-waker --windows-installer-path`.

## 0.41.9 — 2026-08-07
- **fix(tokens): a NAMED cell's credential outranks the ambient one (#190).** On a
  multi-cell host a process-global `MESH_GATEWAY_TOKEN` cannot be the credential for
  more than one cell, so naming a cell selected an IDENTITY WITHOUT CARRYING ITS
  CREDENTIAL. Deliberately narrow: the flip applies only when the identity is
  EXPLICIT — a defaulted name keeps the old env-first order, so nothing changes for
  an operator who names no cell. A named cell whose credential file exists but
  yields no usable token now REFUSES rather than falling back to the ambient one:
  naming a cell is a decision, not a hint.
  - The refusal names the file it tells you to fix. It previously rendered the path
    as the literal `(None)` — `peer_src` is assigned only on a successful read, and
    that branch is reachable only when every read failed. The behaviour was correct
    and the diagnostic was useless, which is the worse of the two to ship.
  - Known gap, carded not fixed (#374): presence is tested with `exists()`, so a
    credential that is a BROKEN SYMLINK still falls through to the ambient token
    silently. A dead symlink is a provisioning error, not an absence.
- **feat(waker): portable Codex App Server controller + Linux supervisor (#193, #195).**
  Merged after 0.41.8 was cut, so this is their first release.

### Changelog gap, recorded rather than quietly closed
0.41.7 and 0.41.8 shipped to PyPI with **no entry here**. What they contained:
- **0.41.7** — `fix: an explicit SCOPED --token-file must beat an ambient SHARED-ROOT
  token` (#332), a silent privilege escalation. Same family as #190 above; #190
  generalises it from `--token-file` to peer identity.
- **0.41.8** — `fix(tokens): skip the POSIX mode warning on Windows` (#192).

## 0.41.6 — 2026-08-05
- **feat(board): `cards thread` + `cards say`** — the CLI half of card↔DM fusion.
  The gateway had carried `GET /board/cards/{id}/thread` and the card-gated attach
  path since earlier the same day and **nothing could reach them**: no CLI verb
  existed, so the fusion lived in the database and the OpenAPI schema — shipped,
  deployed, migrated across ~300 cards, invisible to every human and cell. Found
  by trying to write a finding onto a card and having to send a DM instead.
  `say` refuses when a card has no assignee and no `--to` rather than inventing a
  placeholder recipient: the gateway accepts an unregistered `to_node` with a 200,
  so a placeholder is byte-identical to delivery while addressed to nobody.
- **fix(spawn): the Windows Terminal rescue is provider-generic.** It lived in
  `ClaudeMembrane.pre_launch` — nothing in its body was claude-specific, only its
  **call site** was — so codex and antigravity never inherited it. Those were
  exactly the two cells the operator had been launching by hand for months.
  `ClaudeMembrane.pre_launch` is deleted; it collapsed into the base.
- **fix(spawn): codex + antigravity now `chdir`** like claude/grok/vibe, and pass
  `-C .` / `--add-dir .` instead of the absolute cwd. Measured failure:
  `swarph spawn` with a cwd of `C:/…/synced workspace/project folder`
  died with an unexpected-argument error — the path re-split on
  spaces crossing the exec boundary. Codex itself parses that path correctly;
  the mangling was ours. Fixing by removing the dependency on quoting rather than
  out-guessing which Windows layer re-tokenises.
  >>> **VERIFICATION LIMIT, STATED:** CI is green on `windows-latest`, but the
  suite mocks `_launch_via_tmux`, `_relaunch_in_windows_terminal` and `os.execve`
  — the exact boundary this fixes. Windows CI proves the code imports and the
  logic holds; it does **not** prove argv survives `execve`. A broken fix returns
  the same green. Metal verification is outstanding. <<<
- **fix(cli):** bound the shared pin (`swarph-shared>=0.4.0,!=0.6.0,<0.7`) so a
  transitive break becomes an install-time resolver error, and `--help` shows the
  real product surface (#308, #301).
- **fix(monitor):** land `TmuxNotifySink`, which was **uncommitted and
  load-bearing** on the shared editable clone (#309) — one `git checkout` from
  deletion.
- **note:** the three fixes above had been running in production on the lab-ovh
  box for days via an editable clone parked on a WIP branch, and had never reached
  `main` or PyPI. No version string showed it: `swarph --version` reported 0.41.5
  (a claim the source makes about itself) while the installed dist-info said
  0.41.4 and the executing code was neither.

## 0.39.4 — 2026-07-27
- **fix(packaging):** ship `scripts/ensure_monitor.sh`. 0.39.3 shipped a README
  telling operators to run it while the wheel **did not contain it** — non-`.py`
  files must be declared in `[tool.setuptools.package-data]` and it was not.
  twine, CI and the build all reported success; a clean-room
  `pip install --no-cache-dir` from PyPI is what caught it.
- **test:** `test_packaged_artifacts_exist.py` — a file the docs tell operators
  to run must exist in the package tree **and** be covered by a package-data
  pattern. Existing in `src/` is not enough; the wheel omits it silently.
  Verified to FAIL when the declaration is removed.

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
