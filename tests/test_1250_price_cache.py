"""card #1250 — the bench price cache expires, a missing model is loud,
and load() does not wait 30s or need the live network.

A, B and D are asserted here against the library. The installed-wheel
leg of C lives in test_1248_ship_price_table.py.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import swarph_cli.bench.prices as prices
import swarph_cli.bench.refresh_prices as refresh_prices

_OPUS = {"claude-opus-5-5": {"in": 4.0, "out": 20.0}}


def _reset(monkeypatch, cache: Path):
    monkeypatch.setattr(prices, "_MAP", None)
    monkeypatch.setattr(prices, "_CACHE_PATH", str(cache))
    monkeypatch.setattr(prices, "_FALLBACK_WARNED", set(), raising=False)
    # urlopen caches an opener that captured the previous test's proxy.
    monkeypatch.setattr(urllib.request, "_opener", None, raising=False)
    for key in ("https_proxy", "http_proxy", "HTTPS_PROXY", "HTTP_PROXY",
                "NO_PROXY", "no_proxy", "SWARPH_BENCH_PRICE_URL"):
        monkeypatch.delenv(key, raising=False)


def _write(cache: Path, table: dict, generated_at):
    payload = {"prices": table}
    if generated_at is not None:
        payload["generated_at"] = generated_at
    cache.write_text(json.dumps(payload))


def _iso(when: datetime) -> str:
    return when.isoformat()


def _patch_fetch(monkeypatch, fresh, exc=None):
    calls = []

    def fake(timeout=30):
        calls.append(timeout)
        if exc is not None:
            raise exc
        return fresh

    monkeypatch.setattr(refresh_prices, "fetch", fake)
    return calls


def _warnings(caplog, needle):
    return [r for r in caplog.records
            if r.levelno >= 30 and needle in r.message]


def test_card_1250_one_day_cache_is_not_refetched(monkeypatch, tmp_path, caplog):
    cache = tmp_path / "llm_prices.json"
    _reset(monkeypatch, cache)
    _write(cache, _OPUS, _iso(datetime.now(timezone.utc) - timedelta(days=1)))
    calls = _patch_fetch(monkeypatch, {"claude-opus-5-5": {"in": 16.0, "out": 20.0}})
    with caplog.at_level("WARNING"):
        table = prices.load(force=True)
    assert calls == []
    assert table["claude-opus-5-5"]["in"] == 4.0
    assert _warnings(caplog, "guarded") == []


def test_card_1250_eight_day_cache_is_guarded(monkeypatch, tmp_path, caplog):
    cache = tmp_path / "llm_prices.json"
    _reset(monkeypatch, cache)
    _write(cache, _OPUS, _iso(datetime.now(timezone.utc) - timedelta(days=8)))
    calls = _patch_fetch(monkeypatch, {"claude-opus-5-5": {"in": 16.0, "out": 20.0}})
    with caplog.at_level("WARNING"):
        table = prices.load(force=True)
    assert calls == [5]
    assert table["claude-opus-5-5"]["in"] == 4.0
    guarded = _warnings(caplog, "guarded")
    assert len(guarded) == 1
    assert "1" in guarded[0].message


def test_card_1250_failed_refresh_keeps_eight_day_table(monkeypatch, tmp_path, caplog):
    cache = tmp_path / "llm_prices.json"
    _reset(monkeypatch, cache)
    _write(cache, _OPUS, _iso(datetime.now(timezone.utc) - timedelta(days=8)))
    _patch_fetch(monkeypatch, {}, exc=OSError("down"))
    with caplog.at_level("WARNING"):
        table = prices.load(force=True)
    assert table["claude-opus-5-5"]["in"] == 4.0
    kept = _warnings(caplog, "keeping the stale table")
    assert len(kept) == 1
    assert "8 days" in kept[0].message
    assert _warnings(caplog, "guarded") == []


def test_card_1250_missing_generated_at_refreshes_through_the_guard(
        monkeypatch, tmp_path, caplog):
    cache = tmp_path / "llm_prices.json"
    _reset(monkeypatch, cache)
    _write(cache, _OPUS, None)
    calls = _patch_fetch(monkeypatch, {"claude-opus-5-5": {"in": 16.0, "out": 20.0}})
    with caplog.at_level("WARNING"):
        table = prices.load(force=True)
    assert calls == [5]
    assert table["claude-opus-5-5"]["in"] == 4.0
    assert len(_warnings(caplog, "guarded")) == 1


def test_card_1250_unparseable_generated_at_refreshes_through_the_guard(
        monkeypatch, tmp_path):
    cache = tmp_path / "llm_prices.json"
    _reset(monkeypatch, cache)
    _write(cache, _OPUS, "not-a-timestamp")
    calls = _patch_fetch(monkeypatch, {"claude-opus-5-5": {"in": 16.0, "out": 20.0}})
    table = prices.load(force=True)
    assert calls == [5]
    assert table["claude-opus-5-5"]["in"] == 4.0


def test_card_1250_first_fill_does_not_log_the_guarded_count(
        monkeypatch, tmp_path, caplog):
    cache = tmp_path / "missing.json"
    _reset(monkeypatch, cache)
    calls = _patch_fetch(monkeypatch, _OPUS)
    with caplog.at_level("WARNING"):
        table = prices.load(force=True)
    assert calls == [5]
    assert table["claude-opus-5-5"]["in"] == 4.0
    assert _warnings(caplog, "guarded") == []


def test_card_1250_lookup_warns_once_per_model(monkeypatch, caplog):
    monkeypatch.setattr(prices, "_MAP", {"claude-opus-5-5": {"in": 4.0, "out": 20.0}})
    monkeypatch.setattr(prices, "_FALLBACK_WARNED", set(), raising=False)
    with caplog.at_level("WARNING"):
        assert prices.lookup("gpt-6") == (0.3, 2.5)
        assert len(_warnings(caplog, "gpt-6")) == 1
        assert "fallback" in _warnings(caplog, "gpt-6")[0].message
        caplog.clear()
        assert prices.lookup("gpt-6") == (0.3, 2.5)
        assert _warnings(caplog, "gpt-6") == []


def test_card_1250_empty_table_does_not_warn_per_model(monkeypatch, caplog):
    monkeypatch.setattr(prices, "_MAP", {})
    monkeypatch.setattr(prices, "_FALLBACK_WARNED", set(), raising=False)
    with caplog.at_level("WARNING"):
        assert prices.lookup("gpt-6") == (0.3, 2.5)
    assert _warnings(caplog, "gpt-6") == []


def test_card_1250_file_url_is_the_source_even_with_a_dead_proxy(
        monkeypatch, tmp_path):
    cache = tmp_path / "missing.json"
    _reset(monkeypatch, cache)
    source = tmp_path / "upstream.json"
    source.write_text(json.dumps({
        "claude-opus-5-5": {
            "input_cost_per_token": 0.000004,
            "output_cost_per_token": 0.00002,
        }
    }))
    monkeypatch.setenv("SWARPH_BENCH_PRICE_URL", source.as_uri())
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    table = prices.load(force=True)
    assert table.get("claude-opus-5-5", {}).get("in") == 4.0


def test_card_1250_source_url_rejects_other_schemes(monkeypatch, tmp_path, caplog):
    cache = tmp_path / "missing.json"
    _reset(monkeypatch, cache)
    monkeypatch.setenv("SWARPH_BENCH_PRICE_URL", "ftp://127.0.0.1/prices.json")
    opened = []

    def _opened(url, timeout=30, *args, **kwargs):
        opened.append(url)
        raise AssertionError("urlopen")

    monkeypatch.setattr(urllib.request, "urlopen", _opened)
    with caplog.at_level("WARNING"):
        assert prices.load(force=True) == {}
    assert opened == []


def _hanging_server():
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    box = {"accepted": False, "conn": None}

    def _serve():
        conn, _ = sock.accept()
        box["accepted"] = True
        box["conn"] = conn
        time.sleep(30)

    threading.Thread(target=_serve, daemon=True).start()
    port = sock.getsockname()[1]
    return sock, box, f"http://127.0.0.1:{port}/prices.json"


def test_card_1250_load_without_a_cache_returns_inside_seven_seconds(
        monkeypatch, tmp_path, caplog):
    cache = tmp_path / "missing.json"
    _reset(monkeypatch, cache)
    sock, box, url = _hanging_server()
    monkeypatch.setenv("SWARPH_BENCH_PRICE_URL", url)
    started = time.monotonic()
    try:
        with caplog.at_level("WARNING"):
            table = prices.load(force=True)
        elapsed = time.monotonic() - started
    finally:
        sock.close()
        if box["conn"] is not None:
            box["conn"].close()
    assert box["accepted"]
    assert elapsed < 7
    assert table == {}
    assert _warnings(caplog, "fallback")


def test_card_1250_stale_load_against_a_silent_socket_keeps_the_table(
        monkeypatch, tmp_path, caplog):
    cache = tmp_path / "llm_prices.json"
    _reset(monkeypatch, cache)
    _write(cache, _OPUS, _iso(datetime.now(timezone.utc) - timedelta(days=8)))
    sock, box, url = _hanging_server()
    monkeypatch.setenv("SWARPH_BENCH_PRICE_URL", url)
    started = time.monotonic()
    try:
        with caplog.at_level("WARNING"):
            table = prices.load(force=True)
        elapsed = time.monotonic() - started
    finally:
        sock.close()
        if box["conn"] is not None:
            box["conn"].close()
    assert box["accepted"]
    assert elapsed < 7
    assert table["claude-opus-5-5"]["in"] == 4.0
    assert any("8 days" in r.message for r in _warnings(caplog, "days"))


def test_card_1250_cli_fetch_keeps_the_thirty_second_timeout(monkeypatch):
    seen = []

    class _Body:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _opened(url, timeout=30, *args, **kwargs):
        seen.append(timeout)
        return _Body()

    monkeypatch.setattr(urllib.request, "urlopen", _opened)
    monkeypatch.delenv("SWARPH_BENCH_PRICE_URL", raising=False)
    assert refresh_prices.fetch() == {}
    assert seen == [30]
