# Schedule unserved create (#742) — Plan

Implements spec `docs/superpowers/specs/2026-09-06-schedule-unserved-create-742.md`
(merged swarph-cli#385). Incorporates lab seat-A (33401/33428):

1. Can-fail **both directions** (always-UNSERVED and always-SERVED mutations red).
2. **Derive** live selectors from the units/dispatcher source of truth —
   do **not** hardcode `exec:%` in the gateway forever.

## Approach

### A. Single source of truth for "served"

Today `workers/exec_dispatcher.py` hardcodes:

```sql
target_cell LIKE 'exec:%' OR target_cell LIKE 'dm:%'
```

Wake-path `workers/scheduler.py` takes the complement (`exec:`/`dm:` skipped)
and wakes bare cell names (subject to `SCHEDULER_LOCAL_CELLS` when set).

**Build:**

1. Extract prefix/predicate list into one module used by the dispatcher, e.g.
   `lab_orchestrator/schedule_selectors.py` (name flexible):

   ```python
   EXEC_DISPATCHER_PREFIXES = ("exec:", "dm:")  # LIKE f"{p}%"
   ```

2. `exec_dispatcher.py` builds its SQL FROM that tuple (no second copy).

3. Publish the same list to the gateway without importing lab-orchestrator
   into mesh-gateway:

   - **Preferred:** gateway env `SCHEDULE_SERVED_PREFIXES=exec:,dm:` set by the
     **same systemd unit drop-in** that runs `exec_dispatcher` (or a small
     `schedule-selectors.env` written next to the unit and EnvironmentFile='d).
   - **Fallback for tests:** default `("exec:", "dm:")` only when env unset,
     with a WARNING that production must set the env from the unit — same
     pattern as empty `SCHEDULER_LOCAL_CELLS`.

4. Wake-path served rule (separate): bare `target_cell` is SERVED iff
   `SCHEDULER_LOCAL_CELLS` is non-empty **and** the name is listed; if the
   allowlist env is empty, bare names are **UNSERVED at create** (refuse) —
   today's silent `lab` row becomes impossible. Document: ops must populate
   `SCHEDULER_LOCAL_CELLS` for intentional wake targets.

`served(target)` = matches any served prefix **OR** (wake allowlist hit).

### B. Create gate (`POST /scheduled-events`)

After existing validations, before INSERT:

- If not `served(req.target_cell)` → **400** with predicate named, e.g.
  `target_cell 'lab' is UNSERVED (no live dispatcher prefix match; not in
  SCHEDULER_LOCAL_CELLS). served_prefixes=exec:,dm: allowlist=…`

Dual-gateway: mesh-gateway + swarph-cli `gateway/server.py` lockstep.

### C. List surface

`GET /scheduled-events` (+ get-one) include:

```json
"served": true|false,
"served_reason": null|"no live dispatcher matches …"
```

CLI `swarph schedule list` prints a `served` column / field in JSON.

### D. Optional `schedule doctor`

Thin wrapper: list all rows with SERVED/UNSERVED (same predicate). Can ship
in the same PR or follow-up; not required for accept.

### E. The six rows (operator checklist — not cell-applied)

Commander/operator only (403 for peers):

| event | decision |
|---|---|
| auth-expiry-{gpu-wsl,workstation-lc,droplet,lab-ovh} | retarget `exec:<box>` or `dm:<box>`, cron → **monthly** recurring |
| graduation-board-caller-binding-176 | future revisit + served target, or delete |
| graduation-merge-check-137 | same |

Document on card after apply; code accept does not wait on it.

## Tasks

1. **Selectors module + dispatcher SQL** (lab-orchestrator) — can-fail: changing
   prefixes changes which rows `main()` selects (unit test with fixture DB).
2. **Unit env / EnvironmentFile** documenting `SCHEDULE_SERVED_PREFIXES`.
3. **Gateway `served()` + create 400** (mesh-gateway) + twin (swarph-cli).
4. **List `served` / `served_reason`** + CLI display.
5. **Can-fail tests (both directions):**
   - create `target_cell='nobody'` → 400 (or list UNSERVED if warn-mode — we choose refuse).
   - create `target_cell='exec:lab-ovh'` (and/or allowlisted bare) → 201 + list SERVED.
   - mutation: `served()` always True → first assert fails.
   - mutation: `served()` always False → second assert fails.
6. **Operator checklist** posted on #742 thread.

## Out of scope

- Widening `exec:%` semantics
- #741 SuccessExitStatus
- #715 dispatcher logging
- Cell applying the six edits

## Accept

Matches spec accept + seat-A: both-direction can-fail green; selectors derived
from dispatcher module / unit env (not a forever-hardcoded string only in
gateway); dual-gateway same-day; operator checklist on card.
