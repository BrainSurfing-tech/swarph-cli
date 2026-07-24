#!/usr/bin/env python3
"""Refresh swarph-bench's LLM LIST-price base from the maintained LiteLLM
repo — LIFTED from the reference lab's already-tested
``scripts/refresh_llm_prices.py`` (adapted cache path only).

Fetches BerriAI/litellm ``model_prices_and_context_window.json``, extracts
``{normalized_model: {in, out}}`` in $/Mtok, sanity-guards against the prior
cache (reject >3x deltas = bad upstream edit), and writes the local price
cache atomically. :mod:`swarph_cli.bench.prices` reads it as the price base.

TODO: wire this behind a weekly cron / a `swarph bench refresh-prices` verb
the way the reference lab does (``0 6 * * 1 ... refresh_llm_prices.py``) —
not wired to any scheduler in this PR, just the ported fetch+guard logic.

Network call — never exercised in the offline test suite (mocked or skipped).
Read-only wrt the rest of swarph-cli: only ever writes the price cache.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

from .prices import _CACHE_PATH as CACHE_PATH

LITELLM_URL = ("https://raw.githubusercontent.com/BerriAI/litellm/main/"
               "model_prices_and_context_window.json")
GUARD_RATIO = 3.0  # reject a new price >3x or <1/3x the prior cached value


def _norm(key: str) -> str:
    return key.split("/")[-1].strip().lower()


def fetch() -> dict:
    raw = urllib.request.urlopen(LITELLM_URL, timeout=30).read()
    d = json.loads(raw)
    out: dict[str, dict] = {}
    for key, v in d.items():
        if not isinstance(v, dict):
            continue
        ic, oc = v.get("input_cost_per_token"), v.get("output_cost_per_token")
        if ic is None or oc is None:
            continue
        name = _norm(key)
        # prefer the first (bare) key seen; don't let a vertex_ai/ dup overwrite
        out.setdefault(name, {"in": round(ic * 1_000_000, 6),
                              "out": round(oc * 1_000_000, 6)})
    return out


def load_prior() -> dict:
    try:
        with open(CACHE_PATH) as f:
            return (json.load(f) or {}).get("prices", {}) or {}
    except Exception:
        return {}


def guard(fresh: dict, prior: dict) -> tuple[dict, list]:
    """Adopt fresh prices except where a value swings >GUARD_RATIO vs prior
    (keep prior, flag). New models (absent from prior) are adopted."""
    merged, flagged = {}, []
    for m, p in fresh.items():
        old = prior.get(m)
        if old:
            for field in ("in", "out"):
                a, b = p.get(field, 0), old.get(field, 0)
                if b > 0 and (a / b > GUARD_RATIO or (a > 0 and b / a > GUARD_RATIO)):
                    flagged.append(f"{m}.{field}: {b} -> {a} (>{GUARD_RATIO}x, kept prior)")
                    p = dict(p); p[field] = b
        merged[m] = p
    # keep any prior-only models the fresh set dropped (don't lose coverage)
    for m, p in prior.items():
        merged.setdefault(m, p)
    return merged, flagged


def main() -> int:
    try:
        fresh = fetch()
    except Exception as e:
        print(f"refresh_prices: FETCH FAILED ({e}) — cache unchanged", file=sys.stderr)
        return 1
    if not fresh:
        print("refresh_prices: empty fetch — cache unchanged", file=sys.stderr)
        return 1
    prior = load_prior()
    merged, flagged = guard(fresh, prior)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(),
               "source": "BerriAI/litellm model_prices_and_context_window.json",
               "count": len(merged), "prices": merged}
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_PATH), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, CACHE_PATH)
    print(f"refresh_prices: {len(merged)} models cached "
          f"({len(fresh)} fetched, {len(merged) - len(fresh)} prior-only kept). "
          f"{len(flagged)} guarded.")
    for f in flagged:
        print(f"  GUARD: {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
