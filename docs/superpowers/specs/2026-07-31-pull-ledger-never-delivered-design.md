# `monitor status` overstates unread — one field, two questions

**Board card:** #126 · **Status:** spec · **Owner:** lab-ovh
**Reported by:** lab-ovh (2026-07-27), independently re-found by gpu-wsl and workstation-lc (2026-07-31)

> Spec in the tree per #191. Written only after measuring — two earlier hypotheses were refuted by
> the data and are recorded below, because the refutations are what make this one credible.

---

## Three boxes, same version, and the numbers do not agree

| box | ledger row | `last_delivered_id` | cursor | status says | gateway truth |
|---|---|---|---|---|---|
| **lab-ovh** | present | 11208 | ~11512 | ~300 | 70 unread |
| **gpu-wsl** | **absent** | — (defaults 0) | ~10680 | **5,400** | **0 unread** |
| **workstation-lc** | present | **0**, `last_delivery_at=0.0` | 11509 | **~11,500** | pull never used |

Same code (0.40.3). One advances-but-lags, one never persists, one is *correct and still reports
11,500*.

## Two hypotheses I had, both refuted by measurement

**1. "The pull ledger never advances on ACK"** — *this card's own title_. **False.**
`PullSink.observe` does advance it: it walks the poll window over the contiguous READ prefix and, if
nothing in the window is unread, jumps straight to the observation cursor.

**2. Head-of-line blocking.** The loop breaks at the oldest unread (*"an ACK on 62 says nothing about
61"*), so one stale unread should pin the watermark forever. **False on lab-ovh:** 70 unread with the
oldest at id **8292** and 273 already-read DMs after it — yet the ledger sits at **11208**. The
else-branch lets it skip ahead whenever the recent window happens to be clean.

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

## Still open

- **Does gpu-wsl's absent row have a distinct cause** from ws-lc's present-but-zero row? My theory —
  `ledger()` creates the row in memory and the write is conditional on `observe()` returning True, so
  a row that cannot advance is never persisted, and *the absence causes the failure that preserves
  the absence* — is **unconfirmed**. Both cases are fixed by the `None` sentinel at the reporting
  layer, but the write path may still need repair underneath.
- **Detection sweep.** Nothing currently finds the inert landmines. A fleet check for
  never-delivered-but-configured pull sinks would surface them before first use.
