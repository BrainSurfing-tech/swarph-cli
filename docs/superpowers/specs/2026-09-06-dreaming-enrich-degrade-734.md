# Dreaming enrich must degrade, not exit 1 (card #734) — Spec

**Goal:** `swarph dreaming run` on an installed wheel never treats a
missing SLM/`workers` import as "findings" (exit 1). Enrich **skips
loudly**; verify/organize still run; infrastructure failure uses exit
**2** (already documented as "refused to run").

This is a spec. It does not change PyPI yet.

Gates the commander dreaming schedule HOLD (lab 06:3xZ): schedule
stays held until (a) this card lands and (b) 0.54.1 carries #689.

## Measured (shipped 0.54.0, lab-ovh, 2026-09-06)

```
swarph dreaming run --corpus ... --out <scratch>
  File ".../dreaming/enrich.py", line 19, in _client
    from workers.slm_client import SLMClient
ModuleNotFoundError: No module named 'workers'
rc=1
```

`workers` exists in private lab layout, **not** in installed
swarph-cli. Enrich has never run outside that private tree.

Help text (verbatim contract):

| rc | meaning |
|---|---|
| 0 | adjudicated >0 claims AND no disagreements |
| 1 | findings (disagree / surface_disagreement) |
| 2 | refused to run |
| 3 | ran but adjudicated nothing |

Uncaught `ModuleNotFoundError` → process exit 1 = **same code as a
healthy findings run**. A scheduler cannot tell them apart. That is
the #715 / #729 family: silence and death look identical, here as
"working with findings."

What already works on the same install:

- `--no-enrich` → rc=1 with real report (779 claims / 24 disagree…)
- `--verify-only` → same, verify only
- live corpus byte-identical under `--no-enrich` (sha256 checked)

Only enrich is broken; only because of one import.

## Mechanism

1. **Degrade, do not crash.** Catch unavailable SLM client at the
   enrich boundary (`ImportError` / explicit probe). Skip enrich.
   Emit in the report, same voice as existing "DID WE LOOK?" lines:
   `enrich skipped: no SLM client` (exact phrasing may match report
   style; must be greppable and present on stdout/report artifact).
2. **Default on installed builds:** `--no-enrich` becomes the
   **default** when the SLM client module is absent; enrich is
   **opt-in** where the module imports cleanly. Do not require every
   operator to discover `--no-enrich` after a traceback.
3. **Exit codes:** reserve **1** for findings only. If the run cannot
   proceed at all (broader infra), use **2**. A skipped enrich with
   verify/organize complete is **not** a refuse — use the normal
   findings/success codes from the stages that ran. A hard enrich
   failure when `--enrich` was explicitly demanded may be **2**.
4. **Do not** bundle `workers` into the wheel as the fix for this
   card. Optional extra later is fine; this card is degrade + honest
   rc.

### CAN-FAIL (required)

```
# with workers / SLMClient deliberately absent (uninstall or block import)
swarph dreaming run --corpus <fixture> --out <scratch>
EXPECT: rc != 1 solely due to the import crash
EXPECT: no ModuleNotFoundError traceback as the only signal
EXPECT: report states enrich skipped
EXPECT: verify/organize still produce a report when those stages run
```

Today that condition is rc=1 + traceback. That must flip.

## Accept

1. Can-fail above green in CI.
2. Help / guide still list exit codes; enrich-skip is documented in
   one line under dreaming.
3. Explicit `--enrich` when client missing → refuse loudly (rc=2) or
   skip with the same report line — pick one in build and lock with a
   test; prefer **rc=2** so a scheduler that demanded enrich does not
   look like findings.
4. Card notes this unblocks schedule trigger (a); (b) remains 0.54.1
   / #689 on the install.

## Out of scope

| out | owner |
|---|---|
| shipping the SLM client in-tree | separate |
| scheduling the cron/timer | held until (a)+(b) |
| the 24 false pkg_version disagrees | #689 (merged; needs release) |
| MCP DM tools | #735 |

## Related

- #684 shipped the verb; this import is what it missed
- #689 false positives in 0.54.0
- #656 dreaming itself
- #715 / #729 silent-failure family
