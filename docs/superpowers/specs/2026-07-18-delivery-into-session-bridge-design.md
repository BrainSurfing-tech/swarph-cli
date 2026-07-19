# Delivery-into-session bridge — `swarph daemon` (design)

**Goal:** Stop drain-only cells from being silently DM-blind. When the `swarph daemon` drains a mesh DM, deliver it INTO the cell's live agent session (the resident model's context) — not just to a log no in-session model reads — so the model actually acts on it per CLAUDE.md DM SEMANTICS.

**Origin:** workstation-lc's find — its daemon drains + JSONL-logs DMs, but the running agent session never sees them, so nothing acts. lab-ovh papers over this with a Claude Code Monitor tail (harness-specific); every other daemon-class cell (workstation-lc, droplet, razorpeter, gpu-wsl, gemini-researcher) has no such bridge. This is the next pipeline item after the whatweknow read-side trial.

**Architecture:** `swarph daemon` (swarph-cli) already drains DMs from the mesh gateway to a transactional cursor + `inbox.log`, surface-only — `_route_to_handler` (`commands/daemon.py`) is a v0.5.0 stub that just prints "surfacing only." This feature wires that stub into the unbuilt **REPL-drain path**: after a DM is drained, the daemon **injects it into its own cell's agent pane** via the tmux/psmux multiplexer (`swarph_cli/multiplexer.py`), gated by a *positive*-idle probe ported from lab-orchestrator's proven `workers/cell_wake.py`. Push, not pull — harness-agnostic (claude/codex/grok) and cross-platform (Linux tmux + Windows psmux), which is why it covers workstation-lc.

**Tech Stack:** Python 3 (swarph-cli's floor), stdlib + the existing multiplexer/`tmux`-binary shell-out. No new runtime deps. Lives in `swarph_cli/` behind the existing `swarph daemon --auto-act` opt-in.

## Global Constraints

- **Never crash the daemon.** The daemon's loud-on-down liveness (`ps aux | grep '[s]warph daemon'`) is load-bearing; the bridge's failures are caught + logged, the daemon keeps draining. A bridge exception must never take the drain loop down.
- **Idle-probe is fail-safe toward "busy."** Never inject into an ambiguous/busy/modal pane — defer. This is the modal-stall safety property (never send-keys into a numbered menu; text could read as a selection). Positive-idle confirmation only (footer sentinel present AND no busy-markers), exactly as `cell_wake._probe_idle`.
- **Never lose a DM.** A drained-but-undelivered DM stays queued (persisted beside the cursor) and is retried; the gateway cursor still advances (drain ≠ delivery — the two states are tracked separately) so a re-drain doesn't double-fetch, but an undelivered DM is never dropped.
- **Quiet unless actionable.** Actionable (wake on next idle) = `question`, `unblock`, and any `answer` carrying a **non-null `thread_id`** (a targeted reply — probably to this cell's own question). Ride-along (delivered in the next batch, never triggers its own wake) = `fyi`, `status`, and `answer` with `thread_id=null` (the ~80/day broadcast chatter, e.g. EOD fan-outs). The wake flag is computable from the message alone — no cell-side thread tracking in v1.
- **Opt-in.** Active delivery ships behind the existing `--auto-act` flag; surface-only stays the default for un-opted cells (no behavior change unless a cell is provisioned for delivery).
- **Cross-platform.** All pane I/O goes through the multiplexer's `tmux` binary (tmux on POSIX, psmux on Windows) — no OS-specific pane code.

## Components

Each is a small, independently testable unit.

### 1. `session_target` — resolve the cell's agent pane
`resolve_session_pane(self_name) -> str | None`. Finds the tmux/psmux session/pane hosting THIS cell's resident agent (the session `swarph spawn` launched). Uses the multiplexer's detection + a naming convention (the cell's own session name). Returns the pane target string, or `None` when there is no resident session (a truly headless drain-only cell) → the daemon stays surface-only for that cell and logs `no resident pane` once. No resident pane is a valid state, not an error.

### 2. `idle_probe` — the safety gate
`probe_pane(pane) -> "idle" | "busy" | "modal"`. Ports `cell_wake`'s positive-idle detection: `capture-pane -p` the target, require the idle footer sentinel (`"? for shortcuts"`) AND none of the busy-markers; a numbered menu / dialog → `modal`; anything else → `busy`. Fail-safe: any capture error or ambiguity → `busy` (defer, never inject).

### 3. `pane_inject` — deliver into the pane
`inject(pane, text) -> bool`. `send-keys` the rendered delivery block into the pane, then submit (Enter). Cross-platform via the multiplexer `tmux` binary. Returns success; any failure → `False` (caught by the caller, DM stays queued). Uses literal/`-l` send-keys semantics so DM content can't be misread as tmux key names.

### 4. `delivery_queue` — pending-DM state
Per-cell queue of drained-but-not-yet-delivered DMs, persisted as a single-row JSON file beside the daemon cursor (write-and-rename atomic, same discipline as the cursor) so it survives a daemon restart. Each entry: `{id, from, kind, thread_id, content, wake: bool, deferred_ticks: int}`. `wake` is computed at enqueue by the actionable rule (see Global Constraints). Dedups by message id (a re-drain never re-queues a delivered id).

### 5. `stall_alert` — surface a stuck cell
When a cell never goes idle, DMs defer. After `deferred_ticks` crosses a threshold, send ONE commander DM via the mesh gateway, with **exponential backoff** (alert at 6, 12, 24, 48… — doubling, ~log2 total not linear), resetting the counter on successful delivery. Reuses the exact pattern from the science-claude 63h-stall / 145-DM-flood fix (`feedback_modal_stalls_cell_wake`) so a stall surfaces itself instead of rotting — and never floods.

### 6. Drain-loop wiring
Replace the `_route_to_handler` stub in `commands/daemon.py`. On each drained DM: `queue.enqueue(dm)`. Each tick, after draining: attempt delivery of the queue (below). Guarded so no bridge exception escapes into the drain loop.

## Data Flow

```
gateway DM → daemon drain (cursor advances) → queue.enqueue(dm, wake = kind ∈ {question, unblock} OR (kind==answer AND thread_id != null))
   → each tick, attempt_delivery():
       resolve pane
         none  → surface-only (log once), leave queued as informational (no wake ever) — headless cell
         pane  → probe_pane
             idle  → render batch → pane_inject(send-keys + submit)
                        ok   → dequeue delivered ids, reset stall counter
                        fail → leave queued, log, retry next tick
             busy  → defer (stay queued), deferred_ticks++ → stall_alert at 6,12,24…
             modal → dismiss-safe-modal (cell_wake._try_dismiss_safe_modal), re-probe once; else defer
```

Batch semantics: on an idle wake, deliver ALL currently-queued DMs in one injected block (actionable + any fyi/status riding along), so low-priority chatter never triggers its own wake but is never lost.

**Injected block** the resident model reads:
```
📨 mesh delivery (2 new):
  · from=droplet kind=question: <content>
  · from=drop     kind=unblock:  <content>
(act per DM SEMANTICS — reply AI-to-AI via mesh-gateway; loop human only across a privilege boundary)
```

## Error Handling

- **Pane capture / send-keys failure** → caught, logged, DM stays queued, retry next tick. Daemon unaffected.
- **No resident pane** → surface-only for that cell, logged once (not per-tick spam). Not an error.
- **Modal on the pane** → attempt the scoped safe-modal dismiss + one re-probe; still not idle → defer (never force).
- **Queue-file corruption / unreadable** → treat as empty, log, continue draining (fail-safe; a lost queue file means at worst a re-surface, and the gateway still holds unread state).
- **Stall-alert gateway POST failure** → logged, counter unchanged (retries at the next threshold); never blocks delivery attempts.
- **Any uncaught bridge exception** → caught at the drain-loop boundary, logged loudly to stderr; the daemon keeps running (liveness intact).

## Testing

- **`idle_probe`:** modal-menu fixture → `busy`/`modal` (never idle); clean footer fixture → `idle`; capture error → `busy` (fail-safe).
- **`pane_inject`:** builds the correct `send-keys -l … Enter` argv (mock the `tmux` binary); returns `False` on a non-zero binary exit.
- **`delivery_queue`:** enqueue/dequeue, persists across reload (write-and-rename), dedups by id, `wake` flag set from kind.
- **`stall_alert`:** fires at 6/12/24 (backoff sequence, not linear), resets on delivery, single alert per threshold (no flood).
- **kind policy:** `question`/`unblock` → `wake=true`; `answer` with non-null `thread_id` → `wake=true`; `answer` with `thread_id=null`, `fyi`, `status` → `wake=false` but still delivered in the next batch (ride-along, never dropped).
- **Integration:** spin a real throwaway tmux session, enqueue a DM, run one delivery tick → assert the injected block lands in the pane (`capture-pane`); a "busy" fixture pane → asserts defer + stall counter increments + backoff alert.
- **No-pane path:** `resolve_session_pane` → `None` → surface-only, no inject attempted, one log line.

## Build Order

1. `idle_probe` (port + adapt `cell_wake._probe_idle` / safe-modal-dismiss into swarph-cli) + tests.
2. `pane_inject` (multiplexer send-keys) + tests.
3. `session_target` resolver + tests.
4. `delivery_queue` (persisted, dedup, wake-flag) + tests.
5. `stall_alert` (backoff + reset) + tests.
6. Wire the drain loop (`_route_to_handler` → enqueue + `attempt_delivery`), guarded; integration test end-to-end on a real tmux session; version bump.

Plan ends at green tests + the bridge wired behind `--auto-act` + an end-to-end delivery proven on a real pane. **Validation rollout (post-merge, not part of the build): workstation-lc (the finder) + lab-ovh first, then droplet / razorpeter / gpu-wsl / gemini-researcher.**

## Open / Risks

- **Pane-resolution convention** — the one real correctness risk (mirrors whatweknow's path-format risk): the daemon must reliably find ITS cell's agent pane across cells that may name sessions differently. A build step verifies resolution against a real live session before wiring. If a cell's session naming is non-standard, it falls back to surface-only (safe) rather than injecting into the wrong pane.
- **Busy-cell latency** — a genuinely hot cell delivers late (on next idle); accepted, and the backoff-alert bounds how long a stall stays silent. Force-deliver-at-turn-end (Stop hook) was considered and rejected for v1 (Claude-Code-specific; nudges the next turn).
- **Injection nudges a turn** — waking an idle cell submits a turn; that's the intended behavior for actionable kinds. fyi/status never wake on their own (ride-along only), bounding the churn.
- **Cross-agent panes** — codex/grok panes have different idle sentinels than claude; v1 targets the claude membrane (the daemon-class cells above are claude); the probe's sentinel set is a per-agent config point for later.
