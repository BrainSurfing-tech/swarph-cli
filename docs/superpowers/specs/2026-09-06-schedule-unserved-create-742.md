# Schedule create must refuse unserved targets (card #742) — Spec

**Goal:** A `scheduled_events` row whose `target_cell` matches no live
dispatcher must be impossible to create silently, and `schedule list`
must render ENABLED+UNSERVED as a third state — never as healthy.

This is a spec. It does not change live schedules.

Pairs with **#715** (exec-dispatcher silent: 861 runs / 1 log line —
this is *what* it was not selecting) and **#741** (PROVEN pack exit 1
read as systemd `failed` — why the detector was unheard). Do not fold
into either; fix surfaces differ.

## Defect (measured)

Six `enabled=1` events, all `target_cell='lab'`, all `last_fired_at=NULL`,
all past their one-shot cron by ~26–31 days (created Jul 11–30, due
Aug 6–11). Correlation with live delivery:

| `target_cell` shape | examples | fires |
|---|---|---|
| `exec:lab-ovh` / `exec:science-claude` | eod-*, weekly-newsletter | 4/4 fire |
| `lab` (bare cell name) | the six | 6/6 never |

`workers/exec_dispatcher.py` selects only:

```sql
enabled=1 AND (target_cell LIKE 'exec:%' OR target_cell LIKE 'dm:%')
```

`target_cell='lab'` matches nothing there. Wake-path `workers/scheduler.py`
skips `exec:` / `dm:` and attempts a tmux wake for bare names — but
`lab` is not a real session (the cell is `lab-ovh`). Net: **zero
working delivery path**.

`enabled=1` means "row switched on"; every reader takes it as "will
run". Nothing joins ENABLED to SELECTABLE.
[[feedback_absent_scheduler_vs_broken_one]].

### What was lost (not hypothetical)

Four rows are **auth-expiry backstops** (gpu-wsl, workstation-lc,
droplet, lab-ovh). On 2026-09-06 lab measured grok `~/.grok/auth.json`
(mtime 2026-08-21) rejected / deleted on use — the $0 grok lane down
on an expired token while the whole backstop class had been unrunnable
for a month. Two rows are graduation revisits
([[project_graduation_register]]) that passed unobserved.

Detector: `swarph-pack-unserved-registry` (PROVEN) has printed
`FAIL UNSERVED <name>` for all six every 20 minutes and exited 1.
That refusal is the product; systemd rendered it as unit failure
(#741). This card is the *content* of that alarm.

## Mechanism

### 1. Shared served-check (single source of truth)

Extract (or call) the same predicate the pack already uses:

- **SERVED** iff `target_cell` matches a live dispatcher selector
  currently: `exec:%` **or** `dm:%` (exec_dispatcher), **or** is an
  explicit wake-path target the wake scheduler will actually attempt
  (bare name that is allowlisted / co-located — see
  `SCHEDULER_LOCAL_CELLS` when set).
- **UNSERVED** otherwise (including bare names that look like cells
  but match no selector and no allowlist entry — today's `lab`).

Do **not** widen `exec:%`. The defect is write-time silence, not
selector breadth.

Gateway create path today (`POST /scheduled-events` in mesh-gateway +
swarph-cli twin) already validates cron, durable `context_ref`,
injection charset, and optional `SCHEDULER_LOCAL_CELLS` membership.
It does **not** check dispatcher selectability. Add that check after
existing validations, before INSERT.

### 2. `schedule create` / `POST /scheduled-events`

**Refuse** (HTTP 400) when UNSERVED — preferred default.

Body must name the failing predicate, not only recite policy
(lesson from #740): e.g. `target_cell 'lab' matches no live
dispatcher (exec:%|dm:%|wake-allowlist); served=?`.

Optional warn-and-create mode is acceptable only if the row is
persisted with an explicit `served=0` (or equivalent) that `list`
cannot hide — silent warn-to-log alone is a regression of this card.

Dual-gateway lockstep: mesh-gateway `server.py` +
`swarph-cli/.../gateway/server.py`.

### 3. `schedule list` / `GET /scheduled-events`

Each row includes a caller-visible served signal, e.g.:

```json
{ "name": "...", "enabled": 1, "served": false, "served_reason": "no live dispatcher matches target_cell" }
```

CLI `swarph schedule list` must surface SERVED/UNSERVED in human and
`--json` forms — not only dump `enabled`.

Enabled-and-unserved is a **third state**; it must not render as the
healthy one.

### 4. Decide the six (operator path — cells cannot apply)

Lab constraint (measured): `schedule create` is operator-gated
(403 for per-peer cells). Spec must **not** plan as if cursor-win
can PATCH the six.

For each of the six, commander/operator chooses one:

| action | when |
|---|---|
| Retarget + future cron | still want the backstop/revisit |
| Delete | obsolete |
| Disable + leave | audit only |

Auth-expiry four: prefer **recurring monthly** (or similar), not
one-shot calendar dates — a missed one-shot is gone forever (what
happened). Suggested target shape: `exec:<box>` or `dm:<box>` with
task text preserved / refreshed. Graduation two: retarget to a served
selector with a **future** revisit, or close via graduation register
process.

Document the decision table on the card after operator applies it;
this card's *code* accept does not require the six to be fixed first,
but build/test notes must include the operator checklist.

### 5. Optional: `swarph schedule doctor`

Pack logic as a verb: print SERVED/UNSERVED for every row on demand
without waiting for the 20-minute unit. Nice-to-have; can-fail below
does not depend on it.

## Can-fail (both directions)

1. Create with `target_cell='nobody'` → create **refuses** (or warns
   with persisted unserved mark) **and** list marks UNSERVED.
2. Create with a served selector (`exec:lab-ovh` or fixture allowlist
   entry) → list marks SERVED.
3. Mutation that must go red: checker always returns SERVED → assert
   (1) fails.

A one-sided "always UNSERVED" checker must also fail assert (2).

## Out of scope

| out | note |
|---|---|
| Widening `exec:%` | card forbids |
| #741 SuccessExitStatus / pack exit semantics | separate card |
| #715 dispatcher log silence | adjacent; separate |
| Cell applying the six row edits | operator-gated |
| Rewriting wake vs exec ownership | keep deconfliction |

## Accept

1. Unserved `target_cell` cannot be created silently (400 or explicit
   persisted unserved mark).
2. `schedule list` shows SERVED/UNSERVED per row.
3. Can-fail both directions green; always-SERVED mutation red.
4. Dual-gateway parity (or same-day twin merge).
5. Operator checklist for the six posted on the card (even if
   application waits on commander).
6. Links: related-715, blocked-by/related-741, pack provenance.

## Related

- #715 exec-dispatcher silence
- #741 alarm exit 1 == failed
- #739 / #740 two-states / 403-without-predicate family
- [[feedback_absent_scheduler_vs_broken_one]]
- [[feedback_the_refusal_is_the_product]]
- [[project_graduation_register]]
