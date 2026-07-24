# Contributing a `swarph bench` pack

`packs/` is the v1 home for the community pack registry (board card #101). A
separate `swarph-packs` sibling repo is a plausible later graduation once this
directory outgrows a single repo's PR volume — not built yet; this directory
is the whole registry for now.

## The three required parts of a pack

A pack is **one JSON file** with three required parts (spec §1):

1. **`system`** — the skill/operating context. Every model under test gets
   this VERBATIM. This is the highest-leverage part of a pack: per
   `2026-07-24-model-showdown-findings.md`, changing ONLY the `system`
   flipped every reference model's behavior on identical tasks. A pack whose
   `system` doesn't mirror a real deployment context measures the author's
   framing, not the model.
2. **`tasks[].prompt`** — the tests, with **no answer leaked**.
3. **`tasks[].expected`** — the ground-truth answer, shaped to match
   `tasks[].type` (`numeric`→number, `categorical`→string, `ranking`→array,
   `text`→string).

Packs are **DATA only** — never code. The scoring engine
(`swarph_cli.bench.quality`) is fixed and shared across every pack.

See `packs/arithmetic_demo.json` for a minimal synthetic example covering all
four task types.

## The four `validate` gates (spec §5)

A PR adding or changing a pack **must pass `swarph bench validate`**:

- **(a) schema + integrity** — conforms to the normative pack schema; task
  `id`s are unique; `expected`'s shape matches `type`; `system` present
  (warning if not — an unusual but not-strictly-invalid pack).
- **(b) answer-leak scan** — FAILS the pack if `expected` (or an obvious
  paraphrase) appears in that task's own `prompt` or in `system`. Note: a
  categorical task's instruction naming BOTH valid answers (e.g. `"BUY" or
  "SKIP"`) is not a leak — that's the answer-format spec. A leak is when only
  the correct value's vocabulary term appears, with no genuine alternative
  offered.
- **(c) discrimination check** — run a small set of `--reference-models`; a
  pack that scores every model near-identically is flagged. A rigorous-
  looking pack that can't tell models apart is not useful (an arithmetic
  finance pack once scored 5 models all 1.00).
- **(d) context-calibration guidance** — `validate` always emits the
  calibration requirement above as a reminder. If the pack declares
  `meta.calibration` (`{model, task_ids, expected_mean_distance_max}`),
  `validate` checks that model+system reproduce it. This format is minimal
  and NOT yet fully standardized (deferred).

`swarph bench add <pack-file>` runs these same gates and, if they pass,
installs the pack at `packs/<theme>.json` — the filename is derived from the
pack's OWN `theme` header (slugified), never asked for on the command line.
It REFUSES (writes nothing) on a schema failure or an answer leak, and
refuses to overwrite an existing pack of the same theme without `--force`.

## What stays out of this registry

Private packs built from real proprietary data (e.g. a real trading desk's
trade history) stay OUT of this public registry. Community packs use
synthetic or otherwise-shareable examples — see `arithmetic_demo.json`.
