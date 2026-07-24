"""``swarph_cli.bench.prices`` — LIFTED from the reference lab's already-tested
``workers/llm_prices.py`` (see ``2026-07-24-model-showdown-findings.md``:
this exact lookup already caught a real ~5x under-count on
gemini-2.5-flash-lite). Pins the lookup semantics (exact hit, provider-prefix
strip, flash-lite-before-flash fallback ORDER, never-raise) independent of
whatever's in the live cache — ported test-for-test from the reference
``tests/test_llm_prices.py`` (the two ``workers.llm_config``-specific tests
are dropped: swarph-cli has no such module to test against; replaced with an
equivalent check against this package's own ``cost_usd``)."""
from __future__ import annotations

import swarph_cli.bench.prices as p


def _fake_cache(monkeypatch, mapping):
    monkeypatch.setattr(p, "_MAP", mapping)


def test_exact_cache_hit_wins(monkeypatch):
    _fake_cache(monkeypatch, {"gemini-3.6-flash": {"in": 1.5, "out": 7.5}})
    assert p.lookup("gemini-3.6-flash") == (1.5, 7.5)


def test_provider_prefix_stripped(monkeypatch):
    _fake_cache(monkeypatch, {"gemini-3.1-flash-lite": {"in": 0.25, "out": 1.5}})
    # a 'gemini/'-prefixed id resolves to the same normalized key
    assert p.lookup("gemini/gemini-3.1-flash-lite") == (0.25, 1.5)


def test_flash_lite_falls_back_to_lite_not_flash(monkeypatch):
    # not in cache -> substring fallback. 'flash-lite' must resolve as LITE,
    # never as flash (order-sensitive bug guard).
    _fake_cache(monkeypatch, {})
    assert p.lookup("gemini-9-flash-lite") == p._FALLBACK["lite"]
    assert p.lookup("gemini-9-flash-lite") != p._FALLBACK["flash"]


def test_flash_and_pro_fallbacks(monkeypatch):
    _fake_cache(monkeypatch, {})
    assert p.lookup("gemini-9-flash") == p._FALLBACK["flash"]
    assert p.lookup("gemini-9-pro") == p._FALLBACK["pro"]


def test_unknown_falls_back_to_flash_default(monkeypatch):
    _fake_cache(monkeypatch, {})
    assert p.lookup("totally-unknown-model") == p._FALLBACK["flash"]


def test_never_raises_on_junk(monkeypatch):
    _fake_cache(monkeypatch, {})
    for junk in (None, "", "   ", "///", "GEMINI-2.5-FLASH"):
        r = p.lookup(junk)  # must return a (float,float), never raise
        assert isinstance(r, tuple) and len(r) == 2


def test_missing_cache_file_returns_empty(monkeypatch):
    # force a re-read pointed at a nonexistent path -> {} not a crash
    monkeypatch.setattr(p, "_MAP", None)
    monkeypatch.setattr(p, "_CACHE_PATH", "/nonexistent/does/not/exist.json")
    assert p.load(force=True) == {}
    assert isinstance(p.lookup("gemini-2.5-flash"), tuple)  # still resolves via fallback


def test_cost_usd_uses_price_table(monkeypatch):
    _fake_cache(monkeypatch, {"gemini-3.6-flash": {"in": 1.5, "out": 7.5}})
    # 1M input + 1M output at 1.5/7.5 $/Mtok = 1.5 + 7.5 = $9.00
    assert abs(p.cost_usd("gemini-3.6-flash", 1_000_000, 0, 1_000_000) - 9.0) < 1e-9


def test_cost_usd_includes_thought_tokens_at_output_price(monkeypatch):
    _fake_cache(monkeypatch, {"gemini-x": {"in": 1.0, "out": 2.0}})
    # thought tokens are billed at the OUTPUT price, same as candidates
    assert abs(p.cost_usd("gemini-x", 1_000_000, 500_000, 500_000) - 3.0) < 1e-9


def test_all_cached_returns_only_well_formed_entries(monkeypatch):
    _fake_cache(monkeypatch, {
        "good": {"in": 1.0, "out": 2.0},
        "malformed": {"in": 1.0},  # missing "out" — must be excluded
        "not-a-dict": "oops",
    })
    assert p.all_cached() == {"good": (1.0, 2.0)}


def test_is_known_true_only_for_exact_cache_hit(monkeypatch):
    _fake_cache(monkeypatch, {"gemini-3.6-flash": {"in": 1.5, "out": 7.5}})
    assert p.is_known("gemini-3.6-flash") is True
    assert p.is_known("totally-unknown-model") is False


def test_fallback_buckets_has_three_tiers():
    buckets = p.fallback_buckets()
    assert set(buckets) == {"pro", "lite", "flash"}
