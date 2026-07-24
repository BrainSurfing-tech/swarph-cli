# Changelog

Notable changes to `swarph-cli`. Earlier history: `git log`.

## 0.38.1 — 2026-07-24
- **docs:** README `swarph bench` section + this changelog (0.38.0 shipped the
  feature without them; README is the PyPI long-description, so this refreshes
  the PyPI page). No code change.

## 0.38.0 — 2026-07-24
- **`swarph bench`** — a deterministic LLM benchmark-pack runner (card #101).
  - `bench run` — N-way model showdown on a pack → confusion-matrix report
    (per-class hit-rates + ground-truth distance + tokens + metered-$ + latency
    + parse-fails), ranked by (distance, cost). Credential **preflight** skips
    key-less models cleanly instead of a mid-run 401; `--strict` aborts.
  - `bench validate` — four trust gates: schema/integrity, answer-leak scan,
    discrimination, and context-calibration guidance.
  - `bench add` — validates + self-registers a pack at `packs/<theme>.json`
    from its own header (no manual name); refuses on leak/schema failure.
  - `bench prices` — the full LiteLLM price list (~1500+ models), a shared cache.
  - Task types: numeric / categorical / ranking / text. Packs are data-only;
    the scoring engine is fixed and shared. `[bench]` extra pulls `google-genai`.
  - Spec + reference impl by droplet; ported into swarph-cli under review.
