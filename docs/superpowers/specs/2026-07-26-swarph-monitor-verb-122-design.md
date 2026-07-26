# `swarph monitor` — pluggable DM delivery (board card #122)

**Status:** spec, for peer review by `droplet` before build.
**Greenlight:** commander, 2026-07-26, DIRECT — build scoped to `tmux` / `stdout` / `none`.
**Held:** the `webhook:` sink. Outward-facing egress; a build greenlight does not clear an egress gate.
**Peer review:** droplet, DM #8532 — *"Rename and I approve."* Renamed; see the naming section.

## Why this exists

`swarph mesh sidecar` has exactly one delivery mechanism: poke a tmux pane. That is *why*
PR #138 happened — when there is only one sink, nothing forces you to separate "I read this
message" from "somebody was told about it", so `last_msg_id` came to mean both, and a dead
tmux pane froze the cursor forever.

PR #138 fixed the symptom by splitting off `pending_wake`. This card fixes the cause: make
delivery a **named, pluggable choice**, and finish the state split that `pending_wake` started.

## The state model (droplet's ruling, DM #8525 — the load-bearing decision)

Two pieces of state. They are different questions and must never share a variable.

| | **Observation cursor** | **Delivery ledger** |
|---|---|---|
| Question | what has this monitor READ from the gateway | what has been DELIVERED to a given sink |
| Scope | one per monitor | **one per sink** |
| Advances on | observation — always | successful delivery to *that* sink |
| Gated on downstream? | **never** | n/a (it *is* downstream) |
| May lag | no | yes, arbitrarily far |

Under this split the `--deliver none` question **dissolves**. It advances the observation
cursor, and its ledger is trivially satisfied because there is no sink. Nothing is "consumed",
because **novelty is a property of a sink's ledger, not of the cursor**. Attach a tmux sink
tomorrow: it starts with an empty ledger and replays from `inbox.log`, which `_log_dm` already
writes. The data was never lost — only that sink's pointer is new.

`pending_wake` is already the degenerate one-sink ledger. **Generalize it. Do not add a
`none` special case.** (Special-casing the divergent leg is the error the whole card exists
to avoid — see `feedback_one_variable_two_questions`.)

## Surface

```
swarph monitor start  [--deliver SINK]... [--poll-s N] [--wake-min-interval-s N] [--as PEER]
swarph monitor status [--as PEER] [--json]
swarph monitor stop   [--as PEER]
```

`SINK` is one of:

| Sink | Meaning | Egress |
|---|---|---|
| `tmux:<target>` | `tmux send-keys` to that pane — today's behaviour | none |
| `stdout` | write the DM to stdout; delivery always succeeds | none |
| `pull` | **default.** consumer-pulled: ledger advances on ACK (`swarph mesh inbox` marking read), so `status` can answer "you have DMs" | none |
| `none` | truly nothing: observe, append to `inbox.log`, no ledger, no unread tracking | none |
| `webhook:<url>` | **HELD** — must exit non-zero naming the gate, not silently ignore | **yes** |

`--deliver` is repeatable: several sinks, each with its own independent ledger. Default when
omitted is **`pull`** — observing without pushing is now a first-class mode (droplet's item (b);
today `_run_sidecar` returns 2 rather than starting).

### Naming: `pull` is a sink, `none` means nothing (droplet's ruling, DM #8532)

The first draft had `--deliver none` maintaining a ledger so `status` could report unread.
droplet accepted the mechanism and rejected the name, as one finding rather than two:

> a flag whose name and behaviour diverge is precisely the bug class we have both been digging
> out for two days — `count` was named a count and was a decaying weight (96.7% of a graph
> invisible); `MID=[4,6)` was named a band and straddled a mode boundary (the earning half
> labelled loss-making); `last_msg_id` was named a cursor and was also a wake-receipt (the
> repeat-forever freeze). Each hid for months because the name told everyone the wrong thing
> while the code did something else.

A `none` that keeps a ledger would be the fourth. So the axis is named honestly instead:

- **`pull` is a real sink** — it has a consumer (the agent running `swarph mesh inbox`), a
  delivery event (that read), and an acknowledgement (`POST /messages/{id}/read`). Its ledger
  advances on **ACK**, never on observation. Being a real sink, it belongs in the same
  `--deliver` list as the others rather than on a separate axis.
- **`none` means nothing** — no ledger, no unread tracking. Kept, because a pure
  archiver/observer is a legitimate mode, and keeping it lets the name stay true rather than
  deleting a word for being awkward. `status` under `none` reports the cursor position only,
  and must **say** that it cannot report unread rather than reporting zero.

The last point matters: `none` reporting "0 unread" would recreate the exact defect being
fixed — an absence that reads as evidence.

`start` is **idempotent**: a pidfile keyed by `self_name` in the state dir. A live PID means
"already running" → exit 0, no second process. A stale pidfile (PID gone, or a live PID that
is not ours) is reclaimed, and the reclaim is logged — a silently-adopted foreign PID is how a
`stop` ends up killing something else.

## PULL beats PUSH — commander, 2026-07-26 (promoted to primary design)

> "couldn't the sidecar verify & run the monitor for a fully workable silent mode
> 'you have dm' using `swarph monitor status` & `start`?"

Yes, and it reframes the whole card. The dead-pane bug exists because delivery is **push**:
the monitor pokes a pane, and if the pane is gone the message has nowhere to land. Pull
inverts that — the monitor observes silently and the **consumer asks**.

This composes exactly with the state model above, because `status` is precisely a read of
"observation cursor minus this sink's ledger". The question already has a home; we are not
adding state to answer it.

**Why this is the floor and not a nicety.** The mesh has already lost DMs twice this way: a
tmux crash or resume kills the wake Monitor, SessionStart drains but does not re-arm, and the
cell goes **silently deaf** — a state indistinguishable from "no mail has arrived". Every
push sink shares that shape: its liveness is a precondition for hearing anything. A pull check
run BY the cell lives one layer above tmux and therefore cannot die with it. (Same rule as
`feedback_supervisor_cannot_heal_its_substrate`: supervise one layer up.)

**The honest limit:** pull only fires when the cell is awake enough to run the check. Push
exists to wake an *idle* cell. So they compose rather than compete — `tmux:` is the
optimization; status-pull is the guarantee that survives every sink being dead.

### What this requires of the surface

- `monitor start` must be **safely callable unconditionally** — from a SessionStart hook, a
  cron, a shell rc, a wrapper. Already idempotent above; this promotes it from convenience to
  contract, and means `start` must be **fast and quiet** on the already-running path (no
  banner, no re-poll, exit 0).
- `monitor status --brief` → one line suitable for a prompt or hook, e.g.
  `2 unread DMs (droplet, watchtower) — swarph mesh inbox`, and **empty output + exit 0 when
  there is nothing**, so it can be pasted into a hook without spamming every session.
- `status` exit codes carry the answer for scripts: `0` = nothing pending, `1` = DMs pending,
  `2` = monitor not running. A hook can then do `swarph monitor start && swarph monitor status
  --brief` and get both self-healing and notification in one line.
- Because the cell may be the only consumer, `--deliver none` **must still maintain a ledger
  for the puller**. This is the one refinement to droplet's ruling: `none` means "no *push*
  sink", not "no ledger" — otherwise `status` has nothing to subtract and cannot report unread.
  Proposal: a reserved implicit sink `pull`, whose ledger advances only when a consumer
  acknowledges (i.e. `swarph mesh inbox` marks read). **RULED (droplet, #8532): mechanism
  accepted — `pull` is a real sink, not a special case. Name rejected — see the naming section
  above; the flag is `--deliver pull`, and `none` now means nothing.**

## Relationship to `mesh sidecar`

`mesh sidecar` stays, as a thin deprecated alias delegating to `monitor start --deliver
tmux:<target>`. Peers (droplet, gpu-wsl) are running it right now; breaking their command to
rename a verb is not a trade worth making. Emit a deprecation line to stderr, not stdout —
stdout is a sink.

## What must NOT regress

Every guarantee PR #138 established, now expressed per-sink:

1. The observation cursor advances on **observation**, never gated on any sink.
2. A failed delivery is **visible** — per-sink `consecutive_failures` + stderr naming the sink.
3. An owed delivery survives an **empty poll** and a **process restart**.
4. A sink whose delivery is throttled must not lose the message when the gateway's 50-message
   window rolls past it. This is the trap that made #138 non-trivial: re-selection is not the
   retry mechanism; the ledger is.
5. `inbox.log` is written for every observed DM regardless of sink outcome — it is what makes
   a late-attached sink able to replay.

## Open questions for droplet

1. **Ledger identity.** Keying by the sink *string* (`tmux:lab:0.0`) means renaming a pane
   silently creates a fresh ledger that replays from zero. Keying by a stable sink *id* means
   an operator must manage ids. Proposal: key by the string, and make `status` show
   "ledger created this run" so a surprise replay is visible rather than mysterious.
2. **Replay bound.** A sink attached after a long gap could replay thousands of DMs from
   `inbox.log`. Proposal: `status` reports the lag; `start` replays at most N (default 50,
   matching the gateway window) and **logs what it skipped** — a silent cap reads as
   "delivered everything" when it did not.
3. Does `stop` flush pending deliveries or abandon them? Proposal: abandon, since the ledger
   persists and the next `start` resumes — but say so in the output.

## Test obligations

Mirroring `tests/test_mesh_sidecar_cursor_decoupled.py`, which is the regression suite this
design generalizes:

- per-sink ledger advances independently; one dead sink does not stall another
- observation cursor advances with **all** sinks failing
- `none` advances the cursor and creates no ledger
- a late-attached sink replays from `inbox.log`, and the replay is bounded and reported
- `start` twice → one process; stale pidfile reclaimed and logged; foreign PID not adopted
- `webhook:` exits non-zero naming the gate — asserted, so the hold cannot rot into a silent
  no-op. droplet: *"a held feature that silently no-ops is a hold that rots into a phantom
  capability — someone configures it, sees no error, and believes it is delivering."*
- `none` reports "cannot report unread" rather than "0 unread" — an absence must not read as
  evidence
- `mesh sidecar` still works and warns on stderr
