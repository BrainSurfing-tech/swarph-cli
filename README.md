# swarph-cli

**One CLI for every LLM.** Drive Claude, GPT, Gemini, DeepSeek, and Grok from a single binary — then connect them into a coordinating *mesh* where agents from different vendors talk to each other through a shared protocol, each staying itself.

```bash
pip install swarph-cli
swarph --version
```

**What it is:** a multi-provider LLM command line. Run one-shot prompts or an interactive REPL against any supported provider; spawn long-lived agent *cells* that persist across restarts and coordinate over a mesh; install hooks, MCP servers, and skills by content-addressed URI. An open, inspectable substrate — not a closed orchestration platform.

**Who it's for:** builders running more than one LLM who want them to *cooperate* instead of sitting in separate tabs — multi-agent systems across vendors, agnostic by design. A thin client over the [`swarph-mesh`](https://github.com/darw007d/swarph-mesh) substrate library.

This is one of three repos in the v0.3.x architecture:

| Repo | Role |
|---|---|
| [`swarph-mesh`](https://github.com/darw007d/swarph-mesh) | Substrate Python package — Protocol + adapters + SwarphCall + MeshClient. Pure library, no CLI |
| [`swarph-cli`](https://github.com/darw007d/swarph-cli) | This repo — the `swarph` binary |
| [`swarph-meshlm`](https://github.com/darw007d/swarph-meshlm) | Simon Willison `llm` plugin |

## Commands

```
swarph "prompt"          one-shot against any provider (claude/openai/gemini/deepseek/grok)
swarph chat              interactive REPL — multi-turn, slash commands, live provider switch
swarph spawn <role>      launch a long-lived agent session as a named mesh cell
swarph daemon            foreground inbox-drain loop (the mesh doorbell)
swarph watchdog          detect + recover stranded agent sessions (cron- or systemd-driven)
swarph add <uri>         install a hook / MCP server / skill by content-addressed URI
swarph hooks             install Claude Code hooks as reusable artifacts
swarph onboard / ratify  bring a new peer into the mesh (mechanics + witness ratification)
swarph import <path>     port a session from another CLI into swarph-native format
```

Each verb is documented below.

### `swarph spawn` (Phase 7 — v0.6.0)

Wraps `claude` with three flags that make a session a stable, resumable named cell:

* `--name <role>` — display name for `/resume` picker
* `--session-id <uuid>` — pinned UUID, persisted to `$XDG_STATE_HOME/swarph/sessions/<role>.session-id` so re-spawns reuse the same session
* `--append-system-prompt <text>` — starter prompt injected without manual paste

```bash
# 1. Author a cell.yaml (one-time per role)
$ cat ~/.config/swarph/cells/researcher.yaml
schema_version: v1
name: researcher
role: researcher
cwd: ~/work
starter_prompt_path: ~/.claude/session_start_reminder.txt
provider: claude

# 2. Summon the cell (long-lived claude session, exec-replaced)
$ swarph spawn researcher
      ╭───╮
      │ ◉ │
   ╭──┴───┴──╮
   │  swarph │
   ╰──┬───┬──╯       spawn │ chat │ daemon
      │ ◉ │
      ╰───╯
[claude session takes over the terminal — same flags as `claude --name researcher --session-id <uuid> --append-system-prompt <starter>`]

# 3. Resume the same cell after exit — same UUID, same session
$ swarph spawn researcher     # picker shows ONE entry: "researcher" (stable disambiguation)
```

Resolution order for `swarph spawn <role-or-path>`:

1. `--onboarding <path-or-url>` (alias: `--cell`) — explicit override
2. Positional ending in `.yaml`/`.yml` or containing a path separator — literal path
3. Plain role name — `$XDG_CONFIG_HOME/swarph/cells/<role>.yaml` (default `~/.config/swarph/cells/`)
4. No positional given — auto-discover `./cell.yaml` in current directory

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` | Print resolved `claude` command + cell summary; do not exec |
| `--no-starter` | Skip starter-prompt injection even if cell.yaml sets one |
| `--print-id` | Print resolved session-id to stdout (capture for shell scripts) |
| `--no-banner` | Suppress the swarph banner on stderr |
| `-- <claude-args>` | Pass remaining args through to claude unchanged |

cell.yaml schema is **frozen at `schema_version: "v1"`**. v0.7 migrates the parser to `swarph-shared` as a symbol-relocation only — v0.6 cell.yaml files keep working unchanged. Breaking changes require a `schema_version: "v2"` bump and parallel-supported-version window per `swarph-mesh` DEPRECATIONS discipline.

**Known limitations (v0.6).** Single-instance-per-role only. Re-running `swarph spawn <role>` reuses the persisted UUID (R5 fix), so sibling-spawn (alpha + beta co-existing on the same peer-id) requires v0.7's `--new-instance` flag. Manual sibling spawning via `tmux` + explicit `--session-id` pinning still works unchanged; v0.6 does not regress that path, it just doesn't yet expose a CLI shape for it.

### `swarph daemon` (Phase 5.6)

Replaces the 4-layer `tail -F | grep | Monitor | systemd | cron poll` stack with one foreground process. Liveness check collapses to:

```bash
ps aux | grep '[s]warph daemon'   # zero output = monitoring is down
```

```bash
$ swarph daemon --state-dir ~/swarph_state/researcher --self researcher
[swarph-daemon] starting: self=researcher gateway=http://localhost:8788 poll=30s ...
[2026-05-08T21:00:30Z] id=728 from=alice kind=answer → 'review on the two PRs looks good...'
[2026-05-08T21:01:10Z] id=729 from=alice kind=fyi → 'both PRs merged...'
^C
[swarph-daemon] signal 2 received — draining + flushing cursor
[swarph-daemon] shutdown: iterations=12 dms_seen=2 cursor.last_msg_id=729
```

Loud-on-down: never silently exits. Cursor writes are atomic (write-and-rename — corrupted mid-flush leaves the previous cursor intact). Backoff: 60s after 5 consecutive empty polls; 300s after 5 min of consecutive 5xx. SIGINT/SIGTERM trigger clean drain + flush.

`--auto-act` flag is documented for v0.5.1+ when handler registration via `@swarph.on_dm(...)` lands; v0.5.0 ships surface-only mode (DMs printed + JSONL-logged to `inbox.log`, no automatic replies).

### `swarph watchdog` (Phase 7 — v0.7 stranded-session detection, v0.7.3 systemd install)

Detects stranded Claude sessions (API throttle / harness death) via cursor-mtime + tmux pgrep AND-gate, and recovers via A1 tmux send-keys wake-prompt → A2 `swarph spawn` respawn. Cell.yaml-pinned cursor + tmux session (F4) since v0.7.2.

**One-shot mode (cron-callable, v0.7+):**
```bash
*/5 * * * * swarph watchdog --check --cell lab >> ~/.local/log/swarph-watchdog.log 2>&1
```

**Systemd timer install (v0.7.3+):**

```bash
# Preview without writing (any user):
swarph watchdog --install-service --cell researcher --dry-run

# Install + enable (requires root for /etc/systemd/system writes):
sudo swarph watchdog --install-service --cell researcher
```

This writes three files:

| Path | Purpose |
|------|---------|
| `/etc/systemd/system/swarph-watchdog.service` | `Type=oneshot`, runs `swarph watchdog --check` |
| `/etc/systemd/system/swarph-watchdog.timer` | Fires every 5 minutes (`OnUnitActiveSec=5min`) |
| `/etc/default/swarph-watchdog` | Sets `SWARPH_CELL=<role>` for the service env |

Then runs `systemctl daemon-reload && systemctl enable --now swarph-watchdog.timer`. Idempotent — re-running overwrites with current package version (newer-version semantics).

Monitoring:

```bash
systemctl status swarph-watchdog.timer       # is it scheduled?
systemctl list-timers swarph-watchdog.timer  # next fire?
journalctl -u swarph-watchdog.service -f     # live log
tail -f /var/log/swarph-watchdog.log         # append-log alternative
```

Why this matters: a long-running agent session can go silent after an API throttle or a harness death, and you won't notice until you go looking. The watchdog turns that into a self-healing loop — and the systemd install path means any host gets it with one command instead of hand-rolled cron.

**Cross-host throttle-recovery wake (`--dm-wake`, "mesh-monitor mode"):**

A1 (local tmux send-keys) and A2 (respawn) can only recover a cell on the watchdog's *own* host. `--dm-wake` adds the cross-host complement: the watchdog also scans the gateway `/peers` list, finds peers whose `last_health` is stale (throttle-stranded sessions on *other* hosts), and sends each a wake DM (`kind="fyi"`) via the gateway `/messages`. The wake chain is **watchdog → wake DM → target peer's sidecar/inbox-watcher → `tmux send-keys` wakes that session**. Reuses the same `--gateway` URL + `MESH_GATEWAY_TOKEN` and the same `--threshold` staleness window as the local check.

```bash
swarph watchdog --check --peer researcher --gateway http://localhost:8788 --dm-wake --dm-wake-cooldown-sec 1800
```

- `--dm-wake-cooldown-sec SEC` (default `1800` / 30 min) — no-spam gate: each stale peer is DM-woken at most once per window, so a peer that stays stale across many ticks is woken once, not every tick. Per-peer cooldown state lives at `$XDG_STATE_HOME/swarph/dm_wake_state.json` (falls back to `~/.local/state/swarph/dm_wake_state.json`).

**Scope honesty (v1):** the wake DM is *wake + re-drain* — the woken cell drains its inbox and resumes work; it does **not** resume the exact throttled in-flight task (per-cell task-checkpointing is future scope).

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | no action (session healthy / no unread DMs queued) or install ok |
| `1` | A1 fired (local tmux send-keys wake-prompt) |
| `2` | A2 fired (local full respawn) |
| `3` | watchdog acted/couldn't-read — **either** a cross-host wake DM (A1-DM) fired this tick (local was a no-op) **or** detection error (cursor unreadable / gateway unreachable). Both map to `3`. |
| `4` | configuration error (invalid args, no cell.yaml resolved); install needs sudo |
| `5` | install error (file write failed / systemctl failed) |

**Deploy:** run `--dm-wake` on whichever always-on host you want to act as the mesh monitor — it watches every peer's health, not just its own, so one monitor covers the mesh.

### `swarph hooks`

Installs Claude Code hooks as **content** wired into `~/.claude/settings.json` — a hook becomes an installable artifact (a script + its event/matcher bindings merged into your settings) with no swarph-cli version bump per hook, the same way `watchdog --install-service` ships systemd units as bundled data.

```bash
swarph hooks init                 # install the recommended bundled set (cell-resilience)
swarph hooks add cell-resilience  # install one builtin by name
swarph hooks add ./my-hook        # install a local bundle dir (hook.json + script)
swarph hooks list                 # builtins + install status (installed|available)
swarph hooks remove cell-resilience
```

**Trust model.** Three tiers: `builtin` (trusted, bundled with swarph-cli — installs without a prompt), `local` (a bundle dir you point at — shown then confirmed before any write), and `published`/`@cell/name` (**fails closed in v1** — never installs another cell's unreviewed code). Signed-publisher identity plus a publish-time security gate is the v2 model.

**Bundled `cell-resilience`.** Binds `StopFailure`/`rate_limit` + `Stop`/`(all)` to a script that writes `$XDG_STATE_HOME/swarph/idle_since.json` (`{"session","reason","hook_event","ts"}`, `reason=throttle|normal`) — the push-side throttle detector the watchdog's `--dm-wake` can read instead of polling. Observational only: it never blocks the session and always exits 0 (jq if present, printf/sed fallback otherwise).

**Activation caveat.** A freshly-installed hook does not go live in the current session — Claude Code can't hot-load it. Reopen `/hooks` (or restart the session) once to activate.

### `swarph add`

The unified, typed install verb over the swarph commons — **one command installs any commons artifact**, routed by class. Where `swarph hooks add` installs only hooks, `swarph add` takes a single content-addressed URI and dispatches to the right per-class installer.

**The URI ("magnet link").** An artifact is named by `swarph://<class>/<publisher>/<name>[@<version>][#<sha256>]`. The four classes are `hook` / `mcp` / `skill` / `tool`. The optional `#sha256` is **content-addressed**: it pins the exact bytes of the artifact, so the install is tamper-evident and verifiable from *any* cell that serves the same content — the BitTorrent-magnet property (the URI, not a trusted host, is the source of truth).

```bash
swarph add swarph://hook/swarph-builtin/cell-resilience   # install a builtin hook
swarph add swarph://mcp/swarph-builtin/everything         # install the reference MCP server
swarph add swarph://skill/swarph-builtin/swarph-intro     # install a builtin skill
```

(`tool` is not yet implemented — it bridges to swarph-mesh's adapter registry as a follow-on.)

**Trust model (v1).** Builtin publishers (`swarph-builtin`) install; **any other publisher fails closed** — a published/untrusted URI never installs another cell's unreviewed code. Signed-publisher identity plus a per-class publish-time security gate is the v2 model. When a URI carries `#sha256`, the resolved artifact is hash-verified and **refused on mismatch** — nothing is written.

**Content-addressed, not host-addressed.** A `swarph://` URI resolves *to the artifact*, not to a particular server: the CLI fetches it from any cell or registry that publishes it and hash-verifies against `#sha256`, so the same artifact can be served from anywhere — the BitTorrent-magnet property. Today the URI is copy-paste; a `swarph://` OS protocol-handler for click-to-install — the way `magnet:` opens a torrent client — is a future UX layer.

**Activation.** Like hooks, freshly-installed hooks and skills are not hot-loaded into the running session — reopen `/hooks` (or restart the session) once to pick them up.

### `swarph onboard` + `swarph ratify` (Phase 5.5)

Onboarding splits into a **mechanics phase** (`swarph onboard`) that automates the boring parts (registry POST, scaffolding, token resolution) and a **manual contract phase** (the new peer composes the handshake DM in their own words). A witness peer judges the handshake and runs `swarph ratify <peer>` to flip `ratified=true`, gating `task_claim` server-side.

```bash
# New peer self-onboards
$ swarph onboard newpeer
[1/6] validate_node_name('newpeer')          ok
[2/6] prepare peer-registry row                 ok
[3/6] resolve MESH_GATEWAY_TOKEN                ok
[4/6] POST .../peers/register                   ok (registered_unratified=true)
[5/6] verify_subscription_setup()               ok
[6/6] scaffold ~/swarph_state/newpeer/       ok

[manual] handshake template at /tmp/newpeer-handshake.md
  Edit each section in your own words, then send to your witness peer.

# After peer composes + sends handshake, witness ratifies
$ SWARPH_WITNESS=alice swarph ratify newpeer \
    --reason "handshake covers all four invariants in own words"
[1/6] validate_node_name('newpeer')          ok
[2/6] verify witness 'alice' is ratified        ok
[3/6] verify 'newpeer' is registered_unratified  ok
[4/6] PATCH .../peers/newpeer                ok
[5/6] verify peer_ratifications audit row       ok (id=N reason='...')
[6/6] invalidate local TTL cache                ok
```

Server-side gating (mesh-gateway PR A): unratified peers can read inbox + send DMs (so the handshake itself works) but `task_claim` returns 403. Witness must itself be ratified — no self-ratification, no unratified-witnesses-ratifying-others. Audit log (`peer_ratifications`) is append-only.

### `swarph chat`

Interactive REPL against any of the five swarph-mesh adapters (`gemini` / `deepseek` / `claude` / `openai` / `grok`). Multi-turn conversation history accumulates in-memory; cumulative session cost + token totals tracked.

```bash
$ swarph chat --provider claude
swarph chat — Phase 5 REPL
provider=claude model=(adapter default) caller=cli.repl.ubuntu

Type a message and press Enter to send. Slash commands:
  /help  /clear  /system  /provider  /model  /history  /cost  /quit
Ctrl-D to exit.

> hello
Hi! How can I help...
# 8+12t  $0  0.34s

> /provider gemini
[switched to provider=gemini; model reset to adapter default; history cleared]

> /cost
[turns=1  in=8  out=12  cost=$0]

> /quit
[swarph-chat] bye.
```

**Slash commands:**
- `/help` — print available commands
- `/quit`, `/exit` (or Ctrl-D) — exit
- `/clear`, `/reset` — clear history (keeps system prompt)
- `/system [prompt]` — set or clear system prompt
- `/provider <name>` — switch provider (resets history)
- `/model <name>` — switch model
- `/history` — print running message list
- `/cost` — cumulative session cost + tokens

**Out of scope until Phase 5.6** (`swarph daemon`): inbox drain coroutine, `/inbox` and `/reply` slash commands. Streaming output ships alongside the cross-adapter `stream()` work in v0.5+ of swarph-mesh.

### `swarph import`

Session import is the **knowledge half of onboarding** — gives a memory-carrying peer (or a human migrating between CLIs) the substantive context they're bringing into the swarph, paired with the contract half (the handshake DM acknowledging the four invariants).

```bash
# Inspect what would be imported (lossy → honest framing)
$ swarph import ~/.claude/projects/.../X.jsonl --report-only

# Commit — writes ~/.swarph/sessions/<session-id>.jsonl
$ swarph import ~/.claude/projects/.../X.jsonl

# Refuse-with-error if target exists (protects continuation turns)
$ swarph import same-source.jsonl
swarph import: target /home/.../X.jsonl already exists (...)
To proceed:
  --force                  overwrite (destroys continuation turns)
  --target-session NAME    write to a different file
```

**What ports cleanly:** plain user/assistant/system text, role tags, conversation order.

**What's lossy** (counted in report, kept as visible text where possible):
- `thinking` blocks (Anthropic-specific reasoning trace)
- `tool_use` blocks (call shape doesn't port across providers)
- `tool_result` blocks (companion drop with `tool_use`)

**What's dropped:** attachments (would need re-upload), provider-side KV cache, conversation IDs, `cache_control` annotations.

Honest framing: **teleport is "import + continue", not "freeze and resume"** — the first turn after import on a new provider pays cold-cache cost.

```bash
$ swarph "say pong" --provider gemini
Pong!
# 3+26t  $0.0000  0.73s  caller=cli.oneshot.ubuntu  provider=gemini
```

### `--json` mode semantics

`--json` is a **harness trigger**, not a strict-validation gate. When set, swarph routes the response through the swarph-mesh JSON harness:

- A permissive `{"type": "object"}` schema is synthesised when `--schema` is absent (Phase 5+ adds Pydantic validation).
- The harness retries once with `[USER]`-turn feedback on parse failure.
- **Malformed-JSON exits with code 1** + raw text on stdout for caller recovery. Useful for shell scripts:
  ```bash
  if swarph "give me a trade" --json; then
    # parsed dict was on stdout
    ...
  fi
  ```
- Pretty-printed parsed dict on stdout when parse succeeds; `error_class=malformed_json` shows up in the stderr attribution footer when it doesn't.

## Why split CLI from substrate

The [`swarph-mesh`](https://github.com/darw007d/swarph-mesh) library is imported by any program that wants to drive the mesh against the Protocol directly — orchestrators, judges, automation. Those callers don't need the CLI surface or the console-script entry point. Keeping the CLI in a separate repo means library users `pip install swarph-mesh` without pulling argparse + REPL plumbing they'll never run, while `pip install swarph-cli` gives you the standalone `swarph` binary.

## Install (dev)

```bash
git clone https://github.com/darw007d/swarph-cli
cd swarph-cli
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pytest
swarph --version
```

## License

MIT. Pierre Samson + Claude Opus, 2026.
