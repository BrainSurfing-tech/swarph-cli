# Multi-membrane monitor/sidecar stability — design (board cards #439, #517, #544)

**Status:** spec, for peer review before plan.
**Owner:** science-claude, per #544's explicit handoff ("Handing to science-claude for the #439 build").
**Folds:** #439 (2026-08-13, first description, Linux-only, never built), #517 (5-of-9 lab-ovh sidecars unsupervised), #544 (2026-08-20, mesh-wide reframe, 5-of-5 reachable membranes fanned out and every one broke the original 4-point design). Per #544's own finding: three cards describing one defect, eight days, none built — this spec is the fold, not a fourth description.
**Builds on:** `2026-07-26-swarph-monitor-verb-122-design.md` (per-sink ledger, observation-cursor-never-gated, pull-beats-push doctrine — unchanged, extended) and `2026-07-04-watchdog-pluggable-liveness-design.md` (A1/A2 dormancy recovery — explicitly untouched, see Non-goals).

## Why this exists

Every failure measured this session shares one signature: **delivery stops and every surface reads healthy.** Process-alive, task-Running, `read_at`-advancing, and tail-armed are all satisfiable by a cell receiving nothing. Three different root causes on three membranes produced the identical symptom:

- **Linux (lab-ovh):** 4 cells with no monitor process at all, one dead 231 minutes before detection. No systemd unit, no watchdog behind it.
- **Windows (workstation-lc):** a hardcoded arrow character hit a cp1252 console, `UnicodeEncodeError` on every DM, the cursor never advanced, zero DMs delivered — and the handler swallowed it as "iteration error (continuing)" while Task Scheduler reported `Running`.
- **Any (mistral):** monitor running, no `--deliver` sink configured. 4 DMs sat for 7.5 hours. `read_at` was set the whole time (a different write path), so every mesh surface showed the cell reading its mail.

None of this is bad luck. `swarph monitor {start,status,stop}` has no install/supervise verb — supervision exists exactly where a human remembered to create one, by hand, per box. The split is clean: supervised cells stayed up today, unsupervised ones did not.

## The organizing principle (commander, #544 19:25)

> "Its a windows, linux, macos delivering to x cli (agnostically since it all happens OUTSIDE of it except the delivery of the wake, but that's the SessionStart process."

**The stability problem is an OS problem, not a CLI problem. The matrix is 3, not 3×N.**

| Layer | Scope | Testable without a live agent session? |
|---|---|---|
| **OS layer** — supervisor exists & survives crash/logout/reboot; monitor keeps draining; `inbox.log` gets written | CLI-agnostic — every failure measured today lives here | **Yes.** A cell running claude, cursor, grok, codex, or agy fails identically; none of them is involved yet. |
| **The seam** — the wake reaching the session | The *only* CLI-specific part (`SessionStart` / `PreInvocation`) | No — already carded separately, correctly, as #537 (which harnesses honour a declarative manifest) and #533 (delivered ≠ submitted). Out of scope here. |

Consequence for build order: proposals A/B/C below are OS-layer and can be built and tested with **no CLI present at all** — including `launchd` on the `macos-latest` CI runner, with no resident macOS cell (real macOS via EnvironmentMembrane CI since #259). Only D touches the seam, and only lightly (it watches a file; it does not touch the harness).

## The seam is two routes with opposite dependencies (commander, 19:27–19:29)

| | Mechanism | Acceptance |
|---|---|---|
| **PULL** | `SessionStart`/`PreInvocation` hook; the CLI *reads* the sidecar. | CLI-specific — #537 measured cursor-agent/claude/antigravity **HONOUR**, codex/grok-cli **GATE** (install accepted, hook never fires), muse/vibe **REFUSE** (no manifest surface). |
| **PUSH** | `--deliver tmux:<target>` types into the pane. The CLI never participates. | OS-specific, not universal — tmux (Linux/macOS) vs. **psmux** (Windows, third-party dependency `marlocarlo.psmux`, own defects, see below). |

The two routes cover each other's gaps (push needs no harness cooperation, so it's the answer for muse/vibe), but neither is universal — pull is CLI-specific in mechanism, agnostic in acceptance; push is the reverse. **Hard constraint: arm exactly one route per cell** — overlapping push and tail wakes abort in-flight turns invisibly (13 historical `mid_turn_aborts`, grok-researcher, 2026-08-18).

`--deliver tmux:...` currently names one flag for two backends that have not been proven to share a target-syntax and submit-behaviour contract (cursor-win, unverified — see Test obligations).

---

## Proposal A — drain heartbeat (OS layer)

**Not** "process alive", **not** `read_at` (two writers make it lie — the mistral specimen). The signal: *"I completed a drain iteration successfully at T."*

**Must be a separate process from the monitor** (workstation-lc, answered negatively — his box's monitor is the *only* thing that talks to the gateway, so a heartbeat the monitor emits dies with the monitor; A proves nothing on a box shaped like his). droplet gets this free from an existing crontab line; Windows does not, and Windows is where the deaths are — Windows needs a second, even trivial, process.

**Must resolve its own identity from the pane/session, never from inherited env** (cursor-win: `tmux new-window`/psmux children inherit the server's env, so a heartbeat keyed on `$SWARPH_SELF` would report `workstation-lc` while running inside `cursor-win`'s session — attributing one cell's liveness to another).

**Justification for mandatory, not optional** (gpu-wsl): `Restart=on-failure` does not restart a clean `exit(0)`, and does not restart a **silent hang with no exit at all** — a monitor stuck in a blocking read is indistinguishable from healthy to every supervisor measured. Only an independent heartbeat closes this; no OS-native supervisor primitive can.

Reported to the gateway so one view shows per-cell drain freshness across every membrane, replacing the ambiguous "stale log" read with a named fact.

## Proposal B — failure escalation (OS layer)

Zero membranes have this today. Original design named 5 causes (encoding/auth/quota/gateway/sink); **workstation-lc's own death was none of them** — plain session-lifecycle churn with a clean exit. **Sixth required cause: `supervisor_absent`** (clean exit, nothing configured to restart it) — this is what actually killed cells today, and the original five would have misreported every Windows death as one of the wrong five.

N consecutive failed drain iterations → mark the cell `DEGRADED` on the mesh with the cause **named**, not folded into one line (per Family B-DUAL's own law: one message for two causes hides which one you have). The primitive already exists: `swarph monitor status`'s pidfile check already distinguishes "not running, stale pidfile" from "live process" — B extends this, does not replace it.

**`DEGRADED` must push somewhere a human/process will actually see it — not peer-metadata-only.** (droplet, AI² review: walking a concrete failure through both this design and `watchdog`'s A2 finds a real gap, not a false one — the monitor dies, `DEGRADED` fires, but the tmux *session* is still alive, so A2's own liveness check sees nothing wrong and has no reason to respawn anything; if `DEGRADED` is metadata nobody reads, the cell sits dead-mail-but-alive-session indefinitely, caught by neither system.) This is not a separate open question deferred to plan — it is part of this proposal's own acceptance bar. Reuse whatever surface already alerts on stale heartbeat freshness (Proposal A's own gateway view); do not build a second, parallel alert channel for the same underlying fact. The **unrecognized-cause non-vacuity path** (below, Test obligations) routes to the identical place — a cause nobody named is exactly as invisible as a cause named into an unread field if both just log.

## Proposal C — `swarph monitor install` (OS layer, 3 backends)

Detects the membrane, emits the right artifact: systemd unit (Linux + WSL), `launchd` plist (macOS), Scheduled Task (Windows). **Not one implementation** — per-membrane constraints, all measured, not assumed:

- **Must read `Linger` on `systemd --user`** and refuse loudly if `no` (gpu-wsl: a fresh setup defaults to `Linger=no`; a unit installed without checking dies at first logout, indistinguishable from a healthy exit).
- **Cannot promise reboot survival on WSL** and must say so, not imply it — `wsl.conf`'s `systemd=true` only governs what happens once WSL starts; making Windows start WSL at boot needs an external Windows Scheduled Task swarph does not ship and cannot assume (gpu-wsl, verified end to end with log evidence).
- **`schtasks /create` needs `/f`** — without it, install is not idempotent against an existing task (cursor-win, PR #274).
- **Registration must be emit → human-registers → separately verify, never emit-and-claim.** `Register-ScheduledTask` is blocked by workstation-lc's own harness permission classifier as a persistent-config action (3 attempts, all blocked) — this is a **privilege boundary**, not a bug, and the verb must report `EMITTED, NOT REGISTERED` as a distinct, loud state.
- **Verification must come from *outside* the installing harness** (cursor-win: same box, same OS user, opposite outcomes between two harnesses — the harness that was blocked cannot be the one reporting whether registration succeeded; it has no visibility into what it was denied).
- **Use an interactive principal, never S4U** — an S4U scheduled task reports `LastTaskResult=0` while the payload never ran at all (workstation-lc, tested). Sibling trap to droplet's `systemctl start` finding below — found independently on two platforms.

### The accept-check trap that invalidates a naive verify (droplet + workstation-lc, independently)

**Every supervisor measured has a green-without-execution mode:**
- `systemctl start` on an already-active `oneshot`+`RemainAfterExit` unit is a **silent no-op** — exit 0, no error, `ExecStart` never re-runs. Droplet hit this live restoring a firewall rule: killed it, ran `start`, got `SUCCESS`, rule stayed dead.
- An S4U scheduled task reports success with the payload never executed (above).

**Corrected accept check: assert the payload ran (`ActiveEnterTimestamp` advancing, or equivalent), never trust the supervisor's exit code.** A verification that starts-and-reads-exit-code passes on a service that has been dead-but-marked-active for a week.

### Windows-native launcher shape (workstation-lc, has working reference)

`powershell -WindowStyle Hidden` still creates a console and steals keyboard focus. A VBS `WScript.Shell.Run` wrapper hides the window but gives the child no stdin, hanging anything that reads it. Correct: a GUI-subsystem launcher that redirects and immediately closes stdin (reference implementation: `RunHidden.exe`, on that box). Registered correctly, it survives session exit, reboot (`-AtLogOn`), and non-zero exit (`RestartCount`/`RestartInterval`).

## Proposal D — dead-writer detector (the seam; cell-owned)

A session watching its own `inbox.log` mtime, shouting when it stops growing (grok's suggestion — better than a central probe because it measures the property the cell actually depends on).

**Must not route through PowerShell 5.1 native pipes on Windows** — proven live on cursor-win's box: `Get-Content -Wait -Tail 0 | python -u filter.py`, both processes alive, pipe open, **zero lines ever reached stdin**. This is armed-and-deaf one layer below the drop/grok specimen (theirs was a live tail on a dead file; this is a live tail on a live file whose output never arrives) — every surface an operator would check reads healthy. Working shape on Windows: Git Bash `tail -F | python -u` via a `.cmd` wrapper (POSIX pipes).

## Cross-cutting requirement — wake route, sinks, and wake-policy must be queryable peer state

Three facts about a seat currently live only in that cell's head, and all three produced a real mistake in one night when nobody could query them:

1. **Wake route + sinks** (`pull | push | none`, target). Specimen: lab restarted four dead sidecars with `--deliver pull` only — a safe default, chosen blind, because the prior config was unrecoverable and undisclosed. science-claude's own instance had run `tmux:science-claude` + `pull` since 2026-08-13; the restart silently downgraded it to pull-only, and mail arrived with nothing waking anyone to look, for hours, until noticed by hand. **A safe default applied to an unknown configuration is still a configuration change, and must be announced as one** — the instinct was right, the silence was the error.
2. **Reachability at fan-out time.** Specimen: a 6-peer constraint fan-out reported "3 of 6 in" while one recipient (`razorpeter`) had been dark 8 days — `last_seen` was one query away and wasn't run before sending. The real denominator was 5; a peer that never answers sits in "still outstanding" forever, indistinguishable from one that's thinking.
3. **Wake policy per kind.** Specimen: a mesh-wide safety stop-order went out as `kind=fyi` — the one kind most cells are configured not to wake on — so only cells that happened to drain on their own schedule ever saw it. **Urgency belongs in the `kind`, not the prose**; capitals and `>>> <<<` markers inside a `kind=fyi` body change nothing about whether anyone is woken to read them.

**Requirement:** the peer record (mesh-gateway `/peers/{name}` capabilities) gains `wake_route`, `wake_sinks`, and (if kind-level wake policy is itself configurable per-cell) a queryable wake-policy field. This turns "this cell's log is stale" (ambiguous — ephemeral or broken?) into "this cell has no route" (loud, unambiguous), and a restart restores what a cell *had* instead of what the restarter *guessed*. Same property underlies all three specimens above; one fix, one schema addition on the gateway side — out of scope for this repo's own build, tracked as the gateway-side counterpart.

## Scoped-but-not-transport: the psmux asymmetry (cursor-win)

Push on Windows carries a **supply dependency** (`marlocarlo.psmux`) that Linux/macOS tmux does not. Two defects found, correctly scoped rather than over-attributed:

- **SGR mouse-input leak — observed under `cursor` only, not shown under other CLIs on the same transport.** Original framing overstated this as a transport defect on "the commander's primary input path"; the corrected framing narrows it to `cursor`-observed until a second CLI reproduces it (cheap discriminator: run a non-agent program like `cat` in the same pane/transport — if the leak appears only under `cursor`, it's the CLI's TUI enabling SGR mouse reporting, not psmux). Boards #204/#495 should carry the narrowed wording.
- **Identity leak via env inheritance** — folded into Proposal A above (must resolve identity from pane, not `$SWARPH_SELF`).

## Non-goals

- **The A2 destructive respawn path itself** (`swarph watchdog`, dormancy recovery) — unchanged, per the 2026-07-04 spec's own boundary; no shared signal, no duplicated logic, and that much stays clean (droplet, AI² review). **Not a non-goal in full, though**: a monitor-dead-but-session-alive cell is invisible to *both* systems unless `DEGRADED` alerts somewhere real (see Proposal B, resolved above) — the boundary holds once that's true, and is unclean-by-omission until it is. Recorded here so a future reader doesn't re-read "unchanged" as "unrelated."
- **`#537`'s manifest-honour/gate/refuse story and `#533`'s submission question** — already carded, correctly, as the one CLI-bound piece of the seam. This design's D touches the seam only to watch a file, not to negotiate with the harness.
- **Landing all three backends of C in one PR** — explicitly not required, and for a specific reason (resolved below, formerly open question 3): #439's actual failure mode wasn't staged delivery, it was a card allowed to *close* on a partial ship. Linux/WSL is fully independently specified (Linger check, `ActiveEnterTimestamp` accept check) and lands first; macOS and Windows stay as named, dated, required sub-items on this same card rather than a fresh one. The fix is a closure rule, not a delivery-order rule — the card does not close until all three have their own passing, membrane-specific acceptance evidence.
- **Fixing `#545`'s parity-ratchet field-blindness or `#541`'s creator-guard** — related findings surfaced during #544's fan-out, filed separately, not this build's scope.

## Test obligations

- **A:** kill the monitor process directly (not `stop`); assert the heartbeat's own process is unaffected and continues reporting, then assert the mesh view shows drain-freshness advancing independent of monitor liveness. Identity resolution test: run inside a psmux/tmux child window with `$SWARPH_SELF` set to a *different* cell's name; assert the heartbeat reports the pane-resolved identity, not the env one.
- **B:** parametrized over all 6 causes including `supervisor_absent`; assert `DEGRADED` names the cause, never folds two into one line. Non-vacuity: a failure with an unrecognized cause must not silently pass as one of the six — assert an explicit "unrecognized, logged" path (same discipline as the Family E scheduler-status law this project already carries).
- **C, per membrane:**
  - Linux/WSL: `Linger=no` on a fresh `systemd --user` → install refuses loudly, names the reason. Induced-outage accept check asserts `ActiveEnterTimestamp` advances on a forced crash, never reads the supervisor's own exit code.
  - Windows: install without `/f` against an existing task → the *documented* non-idempotent failure, asserted (not silently worked around); registration verified from a process outside the installing harness; S4U principal explicitly rejected in a unit test (assert the installer never emits an S4U task).
  - macOS: `launchd` supervision validated on the `macos-latest` CI runner with **no resident cell** — if this surfaces new redness on a non-required check, that redness must be resolved before the evidence is trusted (macos-latest is not currently a required status check on `main` and carries known baseline failures; landing new evidence on ungated ground is the same defect this whole card is about, one layer up).
- **D:** Windows build explicitly asserted to route through Git Bash `tail -F`, never a PowerShell-native pipeline — a regression test that pipes a known line count through the shipped watcher and asserts 100% arrival, on the actual Windows membrane, not an interactive console (an interactive UTF-8/65001 console proves nothing about a scheduled launch context — workstation-lc's cp1252 incident happened in the scheduled context specifically).
- **Push transport:** an *executed* cross-platform check — not a reading of both docs — that `tmux:<target>` and psmux's backend accept the same `session:window.pane` syntax and the same double-Enter submit behaviour. If they diverge, the flag is two specs sharing one name, and that must be caught here, not in the delivery path where #533 already showed every success counter reads green regardless.
- **Encoding, any membrane:** any encoding-shaped test must report the environment it ran under (`PYTHONUTF8`, code page, locale, interactive vs. scheduled) — a bare pass is uninterpretable (gpu-wsl found `PYTHONUTF8=1` silently masking a real cp1252 defect in unrelated work; nothing on that box's surface distinguished "fixed" from "masked").

## Open questions for peer review

1. ~~Does `wake_route`/`wake_sinks` belong on the mesh-gateway's peer capabilities...~~ **Resolved (droplet, AI² review, msg 25394): gateway-schema.** droplet's argument uses my own lab-restart specimen against the local-declaration alternative: my sidecar's wake sinks were silently downgraded TWICE (once on a manual restart, once again by the shared systemd template's own default `ExecStart`) precisely because the true state lived only locally and nothing else could see it had drifted from what was intended. A separate sync step just adds a second place that can go stale, with the SAME failure mode one layer up — it's `DEGRADED`'s "how do you see it" problem again. Gateway-authoritative means any peer (or the peer itself, on the pattern this design already needs for A) can ask "what does this cell think its own wake state is" and get an answer that isn't self-reported into a void. Decided: `wake_route`/`wake_sinks` live on mesh-gateway peer capabilities.
2. Escalation (`B`)'s `DEGRADED` state and whether it needs its own alert path — **folded into Proposal B's own body above, not left as a separate open item.** Answer: reuse the same freshness-alert surface Proposal A's gateway view already needs, rather than building a second parallel channel for the same fact. See Proposal B for the full reasoning (droplet's supervisor-blind-spot walkthrough).
3. ~~`C`'s three backends: build and land together, or ship Linux/WSL first...~~ **Resolved (droplet, AI² review, msg 25394): staged delivery, closure discipline not delivery-order discipline.** droplet agreed with the #439 instinct but corrected the mechanism: #439's actual failure wasn't shipping Linux first, it was a card allowed to *close* on a partial ship with no forcing function to finish the rest. Land Linux/WSL first (fully specified already — Linger check, `ActiveEnterTimestamp` accept-check) as an independently mergeable piece; keep #544 open with macOS and Windows as named, dated, required sub-items until each has its own passing acceptance evidence. See Non-goals above for the landed wording.

## Attribution

Every constraint above is measured, not assumed, and sourced from a specific peer's own board-card contribution: droplet (heartbeat-independence half, `systemctl start` no-op, `LANG=C.UTF-8` encoding immunity), gpt-lc (no independent heartbeat writer, scheduled-context encoding caveat), gpu-wsl (silent-hang supervisor blindness, `Linger` precondition, `PYTHONUTF8` masking, WSL reboot-survival gap), workstation-lc (heartbeat-must-be-separate ruling, sixth escalation cause, S4U trap, `RunHidden.exe` reference, harness-diversity-as-accidental-isolation census), cursor-win (per-cell supervision lottery, PS 5.1 pipe defect, psmux identity leak, SGR-leak scope correction), and the commander (the OS-layer/seam decomposition that reorganized the whole card, the push/pull dependency-inversion, the `kind=fyi` wake-policy rule). Full specimens: board card #544.
