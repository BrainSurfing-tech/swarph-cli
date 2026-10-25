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

import datetime
import json
import logging
import os
import tempfile

log = logging.getLogger("swarph.bench.prices")

# card #1250. A cache older than this is refreshed on load(). Exactly this
# age is still fresh: "older than", not "at least".
PRICE_CACHE_TTL_S = 7 * 24 * 60 * 60
# Both load()-triggered fetches. refresh_prices.main() keeps its own 30s.
LOAD_FETCH_TIMEOUT_S = 5


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
# lookup() warns once per model per process when a loaded table has no row.
_FALLBACK_WARNED: set[str] = set()

# Reconciled fallbacks ($/Mtok input, output) — used ONLY when a model isn't in
# the refreshed cache. The cache is the real source; these are the safety net.
_FALLBACK = {
    "pro": (1.25, 10.0),
    "lite": (0.019, 0.075),
    "flash": (0.30, 2.50),
}


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _read_payload() -> dict | None:
    try:
        with open(_CACHE_PATH) as f:
            data = json.load(f) or {}
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _prices_of(payload: dict | None) -> dict:
    if not payload:
        return {}
    prices = payload.get("prices")
    return prices if isinstance(prices, dict) else {}


def _generated_at(payload: dict | None) -> datetime.datetime | None:
    """UTC timestamp, or None when the field is missing or unparseable."""
    if not payload:
        return None
    raw = payload.get("generated_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _age_days_label(generated: datetime.datetime | None) -> str:
    if generated is None:
        return "unknown"
    return str(max(0, int((_now() - generated).total_seconds() // 86400)))


def _read_cache() -> dict:
    return _prices_of(_read_payload())


def _write_cache(prices: dict) -> None:
    """Atomic write of the user cache. Never writes inside this package."""
    dest = os.path.abspath(_CACHE_PATH)
    pkg = os.path.dirname(os.path.abspath(__file__))
    if dest == pkg or dest.startswith(pkg + os.sep):
        log.warning(
            "refusing to write the price table into the package tree (%s)", dest)
        return
    payload = {
        "generated_at": _now().isoformat(),
        "source": "BerriAI/litellm model_prices_and_context_window.json",
        "count": len(prices),
        "prices": prices,
    }
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, dest)
    except OSError as e:
        log.warning("llm price cache not written (%s)", e)


def _fetch_into_cache() -> dict:
    """Fill the user cache from the price source. Never writes inside
    this package (that path is site-packages once the wheel is installed).

    This is the empty-cache path. A stale cache goes through
    ``refresh_prices.guard`` in ``load()`` before it is written.
    """
    try:
        from .refresh_prices import fetch
        fresh = fetch(timeout=LOAD_FETCH_TIMEOUT_S)
    except Exception as e:
        log.warning("llm price fetch failed (%s)", e)
        return {}
    if not fresh:
        return {}
    _write_cache(fresh)
    return fresh


def _keep_stale(prior: dict, generated: datetime.datetime | None) -> dict:
    log.warning(
        "llm price refresh failed — keeping the stale table, %s days old",
        _age_days_label(generated),
    )
    return prior


def load(force: bool = False) -> dict:
    """The cached ``{normalized_model: {"in": $/Mtok, "out": $/Mtok}}`` map.
    Cached in-process; ``force=True`` re-reads. Never raises.

    A missing cache is fetched once into the user cache. If that also fails,
    the empty table is a WARNING, not a DEBUG line: every later lookup is
    then a built-in fallback (card #1248).

    A cache older than ``PRICE_CACHE_TTL_S``, or one whose ``generated_at``
    is missing or unparseable, is refreshed through ``refresh_prices.guard``.
    A failed refresh keeps the stale table (card #1250).
    """
    global _MAP
    if _MAP is not None and not force:
        return _MAP
    payload = _read_payload()
    prior = _prices_of(payload)
    generated = _generated_at(payload)
    if prior and generated is not None:
        age = (_now() - generated).total_seconds()
        if age <= PRICE_CACHE_TTL_S:
            _MAP = prior
            return _MAP
    if prior:
        table = _refresh_stale(prior, generated)
    else:
        table = _fetch_into_cache()
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


def _refresh_stale(prior: dict, generated: datetime.datetime | None) -> dict:
    """Refetch a stale table. The guard runs only on this path, and the
    guarded-count WARNING is logged only here — a first fill has no prior
    to guard."""
    try:
        from .refresh_prices import fetch, guard
        fresh = fetch(timeout=LOAD_FETCH_TIMEOUT_S)
    except Exception as e:
        log.warning("llm price fetch failed (%s)", e)
        return _keep_stale(prior, generated)
    if not fresh:
        return _keep_stale(prior, generated)
    merged, flagged = guard(fresh, prior)
    log.warning(
        "llm price refresh guarded %d change(s) against the stale table",
        len(flagged),
    )
    _write_cache(merged)
    return merged


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
        chosen, label = _FALLBACK["lite"], "lite"
    elif "pro" in m:
        chosen, label = _FALLBACK["pro"], "pro"
    else:
        chosen, label = _FALLBACK["flash"], "flash"
    # A loaded table that lacks this model is the #1248 defect class for a
    # model upstream does not list yet. Once per model per process. An empty
    # table already warned in load(); don't warn again on every lookup.
    if prices and m not in _FALLBACK_WARNED:
        _FALLBACK_WARNED.add(m)
        log.warning(
            "llm price for %s is not in the loaded table — using the %s "
            "fallback %s",
            m, label, chosen,
        )
    return chosen


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
