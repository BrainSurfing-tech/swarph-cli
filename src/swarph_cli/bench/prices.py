"""The auto-refreshed LLM LIST-price table (spec §4) — LIFTED from the
reference lab's already-tested ``workers/llm_prices.py`` (adapted imports/
cache path only; lookup semantics, fallback table, and substring-order bug
guard are unchanged). Do NOT reinvent this — see
``docs/2026-07-24-model-showdown-findings.md`` shipped-alongside note: it
already caught a real ~5x under-count on gemini-2.5-flash-lite.

The LIST-price BASE is auto-refreshed from the community-maintained LiteLLM
repo (:mod:`swarph_cli.bench.refresh_prices` -> a local JSON cache; TODO wire
a weekly cron the way the reference lab does), so new models are never
silently mis-priced by a stale hardcoded bucket.

Public API:
    lookup(model) -> (input_$/Mtok, output_$/Mtok)   # never raises
    cost_usd(model, tokens_in, tokens_thought, tokens_out) -> $
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("swarph.bench.prices")


def _default_cache_path() -> str:
    """User cache, never the installed package.

    card #1248: a table written next to this file lands in site-packages and
    goes stale in the wheel. The cache lives under XDG_CACHE_HOME (or
    ~/.cache). SWARPH_BENCH_PRICE_CACHE overrides it.
    """
    override = os.environ.get("SWARPH_BENCH_PRICE_CACHE")
    if override:
        return override
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")
    return os.path.join(base, "swarph", "llm_prices.json")


_CACHE_PATH = _default_cache_path()
_MAP: dict | None = None

# Reconciled fallbacks ($/Mtok input, output) — used ONLY when a model isn't in
# the refreshed cache. The cache is the real source; these are the safety net.
_FALLBACK = {
    "pro": (1.25, 10.0),
    "lite": (0.019, 0.075),
    "flash": (0.30, 2.50),
}


def _read_cache() -> dict:
    try:
        with open(_CACHE_PATH) as f:
            return (json.load(f) or {}).get("prices", {}) or {}
    except Exception:
        return {}


def _fetch_into_cache() -> dict:
    """Fill the user cache from the LiteLLM price JSON. Never writes inside
    this package (that path is site-packages once the wheel is installed)."""
    try:
        from .refresh_prices import fetch
        fresh = fetch()
    except Exception as e:
        log.warning("llm price fetch failed (%s)", e)
        return {}
    if not fresh:
        return {}
    import datetime
    import tempfile
    dest = os.path.abspath(_CACHE_PATH)
    pkg = os.path.dirname(os.path.abspath(__file__))
    if dest == pkg or dest.startswith(pkg + os.sep):
        log.warning(
            "refusing to write the price table into the package tree (%s)", dest)
        return fresh
    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "BerriAI/litellm model_prices_and_context_window.json",
        "count": len(fresh),
        "prices": fresh,
    }
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, dest)
    except OSError as e:
        log.warning("llm price cache not written (%s)", e)
    return fresh


def load(force: bool = False) -> dict:
    """The cached ``{normalized_model: {"in": $/Mtok, "out": $/Mtok}}`` map.
    Cached in-process; ``force=True`` re-reads. Never raises.

    A missing cache is fetched once into the user cache. If that also fails,
    the empty table is a WARNING, not a DEBUG line: every later lookup is
    then a built-in fallback (card #1248)."""
    global _MAP
    if _MAP is not None and not force:
        return _MAP
    table = _read_cache() or _fetch_into_cache()
    if not table:
        log.warning(
            "llm price table unavailable — pricing from the built-in "
            "fallbacks (pro/lite/flash). No cache at %s and the price "
            "source could not be fetched.",
            _CACHE_PATH,
        )
        _MAP = {}
        return _MAP
    _MAP = table
    return _MAP


def _norm(model: str) -> str:
    """Strip provider prefix (gemini/, vertex_ai/, ...) + lowercase."""
    return (model or "").split("/")[-1].strip().lower()


def lookup(model: str) -> tuple[float, float]:
    """(input_$/Mtok, output_$/Mtok) for ``model``. Exact cache hit ->
    substring fallback (lite before flash so 'flash-lite' resolves as lite —
    order-sensitive, don't reorder). Never raises."""
    m = _norm(model)
    prices = load()
    p = prices.get(m)
    if p and "in" in p and "out" in p:
        return (float(p["in"]), float(p["out"]))
    # substring fallback — ORDER MATTERS: lite is a substring of flash-lite
    # and must win over flash.
    if "lite" in m:
        return _FALLBACK["lite"]
    if "pro" in m:
        return _FALLBACK["pro"]
    return _FALLBACK["flash"]  # flash + safest default


def is_known(model_id: str) -> bool:
    """True if ``model_id`` has an exact hit in the refreshed cache (as
    opposed to resolving via the substring fallback)."""
    p = load().get(_norm(model_id))
    return bool(p and "in" in p and "out" in p)


def cost_usd(model_id: str, tokens_in: int, tokens_thought: int, tokens_out: int) -> float:
    """Metered $ for one call: in-tokens at the input price, (thought + out)
    tokens at the output price (mirrors the reference ``metered_usd()``)."""
    price_in, price_out = lookup(model_id)
    thought = tokens_thought or 0
    return (tokens_in * price_in + (thought + tokens_out) * price_out) / 1_000_000


def all_cached(force: bool = False) -> dict[str, tuple[float, float]]:
    """Every ``{model_id: (in, out)}`` currently in the refreshed cache — the
    full LiteLLM-sourced price list, not just the models ``bench`` happens to
    price. Exposed so the cache is a reusable mesh asset (e.g. a
    cost-governor/ledger can read the same cache via ``swarph bench prices``)
    rather than plumbing private to the runner."""
    return {m: (float(v["in"]), float(v["out"])) for m, v in load(force=force).items()
            if isinstance(v, dict) and "in" in v and "out" in v}


def fallback_buckets() -> dict[str, tuple[float, float]]:
    """The 3 built-in substring-fallback buckets (pro/lite/flash), for
    display when the cache is empty/stale."""
    return dict(_FALLBACK)
