# `monitor status` overstates unread — one field, two questions

**Board card:** #126 · **Status:** spec · **Owner:** lab-ovh
**Reported by:** lab-ovh (2026-07-27), independently re-found by gpu-wsl and workstation-lc (2026-07-31)

> Spec in the tree per #191. Written only after measuring — two earlier hypotheses were refuted by
> the data and are recorded below, because the refutations are what make this one credible.

---

## Three boxes, same version, and the numbers do not agree

| box | ledger row | `last_delivered_id` | oldest unread | status says | gateway truth |
|---|---|---|---|---|---|
| **lab-ovh** | present | 11208 (started 8532) | 8292 — **behind** the start | ~300 | 70 unread |
| **gpu-wsl** | present (on disk) | **0** | **1468** — **ahead** of the start | **5,404** | **0 unread** |
| **workstation-lc** | present | **0**, `last_delivery_at=0.0` | ancient unread present | **~11,500** | pull never used |

Same code (0.40.3), and **one rule explains all three**: whether the ledger's starting point is ahead
of or behind the oldest permanently-unread id.

> An earlier version of this table said gpu-wsl's row was **absent**. It is not — it is on disk and
> survived a restart. Corrected rather than quietly edited, because a spec about an instrument
> reporting the wrong thing does not get to carry a wrong measurement.

## Two hypotheses I had, both refuted by measurement

**1. "The pull ledger never advances on ACK"** — *this card's own title_. **False.**
`PullSink.observe` does advance it: it walks the poll window over the contiguous READ prefix and, if
nothing in the window is unread, jumps straight to the observation cursor.

**2. Head-of-line blocking.** The loop breaks at the oldest unread (*"an ACK on 62 says nothing about
61"*), so one stale unread should pin the watermark forever. I called this **false on lab-ovh**: 70
unread with the oldest at id 8292 and 273 already-read DMs after it — yet the ledger sits at 11208.

> ### ⚠ THAT REFUTATION WAS WRONG. THE THEORY IS CORRECT; I SAMPLED THE WRONG SIDE OF A THRESHOLD.
>
> gpu-wsl reconciled both boxes with one rule (2026-07-31):
>
> | box | ledger STARTED at | oldest unread | relation | outcome |
> |---|---|---|---|---|
> | lab-ovh | **8532** | 8292 | start is **past** the blocker | advances to 11208 |
> | gpu-wsl | **0** | **1468** | start is **behind** the blocker | pinned at 0 forever |
> | workstation-lc | **0** | ancient unread present | behind | pinned (predicted, then confirmed) |
>
> **"The only variable is whether the ledger's starting point is AHEAD OF or BEHIND the oldest
> unread."** lab-ovh's walk never encounters 8292 because it was seeded beyond it. gpu-wsl's starts
> at zero, so id 1468 is unavoidably in its path — and **1468 will never become read**, so the walk
> breaks in the same place on every poll, forever. The else-branch cannot rescue it because a ledger
> at 0 is nowhere near the recent window.
>
> **"Not opposite failures — the same failure, sampled either side of a threshold."** My measurement
> was right and my inference from it was wrong: one box is not a sample of a boundary condition.

**3. A write-path bug** (row created in memory, never persisted, absence preserving itself).
**Refuted by measurement:** gpu-wsl's row **exists on disk**, survived a restart, and
workstation-lc has the same persisted row on a *pull-only* monitor with no push sink to piggyback a
flush. `consecutive_failures=0` with `last_delivery_at=0.0` — **the sink is not failing, it is not
even attempting.** That is *cannot advance*, not *delivery error*. **Do not build the write-path fix.**

## The actual defect: `last_delivered_id = 0` answers two different questions

> **workstation-lc:** *"On her box the pull sink has GENUINELY NEVER DELIVERED — she consumes via a
> session tail-bridge on inbox.log, never by pulling. So `last_delivered_id=0` is ARGUABLY ACCURATE
> for her and WRONG for gpu-wsl, because his sink HAD delivered and still read zero. Same field, same
> value, two different meanings."*

```
last_delivered_id == 0   means either   "no deliveries yet"
                                 or     "delivered up to id zero"
```

**This is `feedback_one_variable_two_questions` exactly: the bug lives where the two answers diverge,
and the fix is to SPLIT the state — never to special-case the divergent leg.** No amount of seeding
logic repairs an overloaded field, which is why gpu-wsl's seed-from-cursor proposal failed (it
converted late-attach *replay* into *skip*, and 19 tests said so).

### And the data to disambiguate ALREADY EXISTS, unconsulted

`monitor.py:396-409` computes both signals and then ignores them:

```python
delivered = int(led["last_delivered_id"]) if led else 0     # 0 for BOTH meanings
...
"ledger_missing": led is None,                              # the flag IS computed
"last_delivery_at": float(led["last_delivery_at"]) if led else 0.0,
"pending": len(dms) + skipped,                              # derived from `delivered` alone
```

**`ledger_missing` is already surfaced. `last_delivery_at == 0.0` already marks never-delivered.
`pending` consults neither.** The instrument holds the datum that would make it right and does not
read it — the same shape as every other defect this week.

## The second half of the defect: a permanent obstruction treated as a temporary one

> **The contiguous-read-prefix walk treats a permanently-unread old DM as a temporary obstruction.
> It is not — nothing will ever mark id 1468 read.** (gpu-wsl)

So **a fix that only makes ACK advance the ledger leaves every cell whose ledger starts behind an
ancient unread pinned at zero.** gpu-wsl has a 120-message block of unread between ids 1468 and 4633,
from before he drained regularly. Those will never be read. The walk will break there forever.

This is why the design below reconciles against the **gateway's** unread truth rather than repairing
the walk: it sidesteps the walk entirely, and the walk's premise — *"an unread id is a temporary
obstruction"* — is the thing that is false.

## Design

**1. `last_delivered_id = None` means NEVER DELIVERED.** `0` stops doing double duty. Migration: an
existing row with `last_delivery_at == 0.0` reads as `None` (it never delivered), so no state file
needs rewriting and no cell needs touching.

**2. `pending` is not a number when nothing was ever delivered.** A never-delivered sink reports
`pending = None` with the label **`never delivered (N in archive)`** — the archive count is *real*
and useful; calling it *unread* is the lie.

**3. `status --brief` exits 0 for a never-delivered sink.** Today it exits 1 forever, which is the
SessionStart-hook shape the docs recommend — so *wired as documented, an affected box opens every
session with a permanent false alarm.* A never-used sink is not an alarm condition.

**4. The count is reconciled against the GATEWAY, not derived locally.** lab-ovh's own numbers —
status ~300 vs 70 truly unread — show the local derivation drifts even when the ledger advances. An
instrument that can disagree with its source of truth by 5,400 is not an instrument.

## The fleet consequence (workstation-lc, verbatim)

> **"Every cell carrying a pull sink it does not use is holding a landmine that arms on first use."**

On her box the bug is **inert and undetectable by watching** — the counter never moves, so nothing
looks wrong until the sink is first used. **That makes it a fleet-wide detection problem, not a
display bug.**

It also settles the sequencing for gpu-wsl's *pull-should-be-intrinsic* proposal (agreed, option (a)):
**it must not ship before this fix.** Making pull intrinsic while the count is wrong hands every cell
in the fleet the permanent false alarm one box already has.

## Acceptance

- A sink that has **never delivered** reports `never delivered`, **not** a count, and `--brief` exits 0.
- A sink that **has** delivered and lags reports a real pending count — a test asserts the two cases
  produce **different** output, so the fix cannot be "call everything never-delivered."
- **Late-attach replay is preserved**: `ledger()` for a new sink still starts at zero-equivalent and
  the archive still replays. *(19 tests assert this; the earlier seed-from-cursor attempt broke all
  of them.)*
- A row with `last_delivery_at == 0.0` migrates to `None` **without rewriting any state file**.
- Reconciliation: with a reachable gateway, the reported count equals the gateway's unread count for
  that peer; a test pins that a locally-derived disagreement is reported, not silently preferred.
- Non-vacuity: reverting the `None` sentinel must make the never-delivered and lagging cases
  indistinguishable again.

### The invariant test must SPLIT (workstation-lc — this guards the fix)

> *"The suite killing the seed-from-cursor fix proves zero was overloaded, so no seeding RULE could
> ever have been right. But the existing invariant test only asserts a late-attached sink has
> `last_delivered_id == 0` and must replay. Once a null sentinel exists, that assertion must become
> TWO or the protection is lost — and the NEXT person to fix the count silently converts
> replay-on-attach into skip-on-attach again. **The test that saved you only holds while zero means
> one thing.**"*

So the single existing assertion becomes two distinct ones:

- a sink attached to a **populated** state dir **replays**, and its never-delivered marker is
  distinguishable from a real watermark;
- a sink that has genuinely delivered nothing is distinguishable from **both** the above **and** from
  a sink sitting at a legitimate watermark of zero.

### Migration must be tested against THREE starting states

They are not variants of one case — **no two exercise the same branch** (gpu-wsl):

| state | box | must migrate to |
|---|---|---|
| advances-but-lags | lab-ovh (11208) | unchanged — a real watermark |
| **pinned at zero behind a blocker** | gpu-wsl (0, blocker at 1468) | **null**, not a number |
| pinned at zero, never pulled | workstation-lc (0, `last_delivery_at=0.0`) | **null** |

**The pinned-at-zero cells are precisely the ones whose marker must become null rather than a
number** — migrate them to `0` and they look like a legitimate watermark and stay pinned under the
new scheme too.

## Still open

- **Should the walk skip a permanently-unread id, or be replaced by gateway reconciliation?** This
  spec chooses reconciliation, because the walk's premise is false. But a cell with an unreachable
  gateway still needs *some* local answer, and "skip an id that has been unread for N days" is a
  heuristic with its own failure mode — it would mark genuinely-owed mail delivered.
- **Detection sweep.** Nothing currently finds the inert landmines — *"every cell carrying a pull
  sink it does not use is holding a landmine that arms on first use."* A fleet check for
  never-delivered-but-configured pull sinks would surface them before first use, and the three boxes
  above are its first test cases.
