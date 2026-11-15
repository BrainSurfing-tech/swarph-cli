"""Ordinary tests price from a fixed table, not the user's cache or LiteLLM."""
import builtins
import urllib.request

import pytest

from swarph_cli.bench import prices


def test_default_prices_need_neither_files_nor_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default test pricing must not read a cache or fetch prices")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    assert prices.load() == {}
    assert prices.is_known("jev") is False
    assert prices.lookup("jev") == (0.30, 2.50)
