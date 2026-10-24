"""card #1248 — an installed wheel must ship the bench price table.

The file used to be gitignored and undeclared as package data, so
``load()`` on an installed copy returned {} and every model was priced
from the three fallbacks. Claude Opus 5.5 came out as Gemini Flash.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import swarph_cli.bench.prices as prices

ROOT = Path(__file__).resolve().parents[1]
_TABLE = ROOT / "src" / "swarph_cli" / "bench" / "data" / "llm_prices.json"
_FLASH = prices._FALLBACK["flash"]


def test_card_1248_price_table_is_committed_and_declared():
    ignore = (ROOT / ".gitignore").read_text()
    assert "llm_prices.json" not in ignore
    assert _TABLE.is_file()
    assert "bench/data/*.json" in (ROOT / "pyproject.toml").read_text()
    payload = json.loads(_TABLE.read_text())
    table = payload["prices"]
    assert payload["count"] > 1000
    opus = table["claude-opus-5-5"]
    assert (opus["in"], opus["out"]) != _FLASH
    assert table["claude-sonnet-5-5"]["in"] != _FLASH[0]
    assert table["gemini-3.8-flash"]["in"] != _FLASH[0]


def test_card_1248_wheel_contains_the_price_table(tmp_path):
    wheeldir = tmp_path / "dist"
    wheeldir.mkdir()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
         "-w", str(wheeldir)],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600,
    )
    assert built.returncode == 0, built.stderr[-2000:]
    wheels = list(wheeldir.glob("swarph_cli-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as z:
        name = "swarph_cli/bench/data/llm_prices.json"
        assert name in z.namelist()
        payload = json.loads(z.read(name))
    opus = payload["prices"]["claude-opus-5-5"]
    assert (opus["in"], opus["out"]) != _FLASH
