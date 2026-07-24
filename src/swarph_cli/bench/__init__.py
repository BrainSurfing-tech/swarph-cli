"""``swarph bench`` — deterministic LLM benchmark-pack runner (board card #101).

A *pack* is a self-contained, subject-agnostic unit of {skill, tests, expected
results} — one JSON file with three required parts: ``system`` (skill/context),
``tasks[].prompt`` (tests), ``tasks[].expected`` (ground truth). Every score is a
DISTANCE in [0,1] (0 = perfect); the engine is fixed and domain-agnostic, packs
ship data only, never code.

Ported from the reference implementation (``hedge-fund-mcp/scripts/{model_showdown,
bench_quality,bench_news98,build_judgment_pack}.py``) per
``docs/2026-07-24-swarph-bench-pack-spec.md``. See submodules:

- :mod:`swarph_cli.bench.quality` — the distance engine (numeric/categorical/
  ranking/text) + answer parsing. Ported verbatim from ``bench_quality.py``.
- :mod:`swarph_cli.bench.pack` — pack loading + normative JSON-Schema validation.
- :mod:`swarph_cli.bench.prices` — a small model-id -> ($/1M in, $/1M out) table.
- :mod:`swarph_cli.bench.backends` — the backend abstraction (metered google-genai
  Developer API + a subscription stub).
- :mod:`swarph_cli.bench.runner` — the N-way showdown loop -> confusion view.
- :mod:`swarph_cli.bench.validate` — the four ``validate`` disciplines.
"""
from __future__ import annotations
