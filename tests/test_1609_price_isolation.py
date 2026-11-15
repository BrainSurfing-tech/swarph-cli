"""Ordinary tests price from a fixed table, not the user's cache or LiteLLM."""
import builtins
import subprocess
import sys
import urllib.request

import pytest

from swarph_cli.bench import prices


def test_default_prices_need_neither_files_nor_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default test pricing must not read a cache or fetch prices")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert prices.is_known("test-price-control") is True
    assert prices.is_known("jev") is False
    assert prices.lookup("jev") == (0.30, 2.50)


def test_subprocess_uses_fixture_prices_without_fetching():
    probe = subprocess.run([sys.executable, "-c", """
import urllib.request
def forbidden(*args, **kwargs):
    raise AssertionError('fixture prices must not fetch')
urllib.request.urlopen = forbidden
from swarph_cli.bench import prices
assert prices.is_known('test-price-control')
assert not prices.is_known('jev')
"""], capture_output=True, text=True, timeout=10)
    assert probe.returncode == 0, probe.stderr
