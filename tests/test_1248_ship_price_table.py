"""card #1248 — an installed wheel prices from a user cache, and says so
when it has none.

The old cache lived beside the module and was gitignored, so a wheel
shipped no table and every model fell through to the flash fallback at
DEBUG. The table is not written into site-packages. A fresh install
fetches it into the user cache. No cache and no fetch is a warning.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import swarph_cli.bench.prices as prices

ROOT = Path(__file__).resolve().parents[1]
_FLASH = (0.3, 2.5)


def _clean_env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def test_card_1248_default_cache_is_outside_the_package(monkeypatch):
    monkeypatch.delenv("SWARPH_BENCH_PRICE_CACHE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    path = prices._default_cache_path()
    pkg = os.path.dirname(os.path.abspath(prices.__file__))
    assert not os.path.abspath(path).startswith(pkg + os.sep)
    assert path.endswith(os.path.join("swarph", "llm_prices.json"))


def test_card_1248_missing_cache_warns(monkeypatch, caplog):
    monkeypatch.setattr(prices, "_MAP", None)
    monkeypatch.setattr(prices, "_CACHE_PATH", "/nonexistent/swarph-1248.json")
    monkeypatch.setattr(prices, "_fetch_into_cache", lambda: {})
    with caplog.at_level("WARNING"):
        assert prices.load(force=True) == {}
    assert any(
        r.levelno >= 30 and "fallback" in r.message for r in caplog.records)


def test_card_1248_installed_wheel_prices_opus_and_warns_when_offline(tmp_path):
    """Build the wheel, install it in a fresh venv, and run outside the tree."""
    wheeldir = tmp_path / "dist"
    wheeldir.mkdir()
    env = _clean_env()
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
         "-w", str(wheeldir)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    wheels = list(wheeldir.glob("swarph_cli-*.whl"))
    assert len(wheels) == 1
    venv = tmp_path / "venv"
    assert subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        env=env, capture_output=True, text=True, timeout=120,
    ).returncode == 0
    vpy = venv / "bin" / "python"
    if not vpy.exists():
        vpy = venv / "Scripts" / "python.exe"
    installed = subprocess.run(
        [str(vpy), "-m", "pip", "install", "--no-deps", str(wheels[0])],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert installed.returncode == 0, installed.stderr[-2000:]

    cache_home = tmp_path / "cache"
    run_env = _clean_env()
    run_env["HOME"] = str(tmp_path / "home")
    run_env["XDG_CACHE_HOME"] = str(cache_home)
    run_env.pop("SWARPH_BENCH_PRICE_CACHE", None)
    priced = subprocess.run(
        [str(vpy), "-c",
         "import swarph_cli.bench.prices as p\n"
         "table = p.load()\n"
         "assert len(table) > 0, len(table)\n"
         "assert p.is_known('claude-opus-5-5'), 'opus not in the fetched table'\n"
         "got = p.lookup('claude-opus-5-5')\n"
         "assert got != (0.3, 2.5), got\n"
         "print(len(table), got)\n"],
        cwd=str(tmp_path), env=run_env, capture_output=True, text=True,
        timeout=120,
    )
    assert priced.returncode == 0, priced.stderr[-2000:]
    cache = cache_home / "swarph" / "llm_prices.json"
    assert cache.is_file()
    assert not list((venv / "lib").rglob("llm_prices.json"))

    offline_env = dict(run_env)
    offline_env["XDG_CACHE_HOME"] = str(tmp_path / "empty-cache")
    offline_env["https_proxy"] = "http://127.0.0.1:9"
    offline_env["http_proxy"] = "http://127.0.0.1:9"
    offline_env["HTTPS_PROXY"] = "http://127.0.0.1:9"
    offline_env["HTTP_PROXY"] = "http://127.0.0.1:9"
    offline_env.pop("NO_PROXY", None)
    offline_env.pop("no_proxy", None)
    offline = subprocess.run(
        [str(vpy), "-c",
         "import logging\n"
         "logging.basicConfig(level=logging.WARNING)\n"
         "import swarph_cli.bench.prices as p\n"
         "table = p.load()\n"
         "assert table == {}, len(table)\n"
         "assert p.lookup('claude-opus-5-5') == (0.3, 2.5)\n"],
        cwd=str(tmp_path), env=offline_env, capture_output=True, text=True,
        timeout=60,
    )
    assert offline.returncode == 0, offline.stderr[-2000:]
    assert "WARNING" in offline.stderr
    assert "fallback" in offline.stderr
