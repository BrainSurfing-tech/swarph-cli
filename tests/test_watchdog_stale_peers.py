"""Tests for ``swarph watchdog`` T2 — stale-peer scan (detection feeding --dm-wake).

Covers the PURE ``_stale_peers`` selector (no network — robust ISO-8601 parse,
skip-on-absent/unparseable, exclude-set, deterministic sorted output) and the
thin ``_fetch_peers`` network wrapper (GET /peers, bare-list + {"peers":[...]}
shapes, [] on error / never raises).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

from swarph_cli.commands.watchdog import _fetch_peers, _stale_peers


# A fixed reference "now". 2026-06-04T00:00:00Z.
_NOW_EPOCH = datetime(2026, 6, 4, 0, 0, 0, tzinfo=timezone.utc).timestamp()


def _iso_ago(seconds: float, *, suffix: str = "+00:00") -> str:
    """ISO-8601 string for (now - seconds). ``suffix`` controls the tz form."""
    dt = datetime.fromtimestamp(_NOW_EPOCH - seconds, tz=timezone.utc)
    base = dt.replace(tzinfo=None).isoformat()
    return base + suffix


# ---------------------------------------------------------------------------
# _stale_peers — pure selector
# ---------------------------------------------------------------------------


def test_stale_peers_returns_only_old_peers_sorted():
    peers = [
        {"name": "fresh", "last_health": _iso_ago(30)},          # 30s ago — NOT stale
        {"name": "tenmin", "last_health": _iso_ago(600)},        # 10min ago — NOT stale (<1800)
        {"name": "threedays", "last_health": _iso_ago(3 * 86400)},  # 3 days — stale
        {"name": "anhour", "last_health": _iso_ago(3600)},       # 1h ago — stale
        {"name": "empty", "last_health": ""},                    # empty — SKIP
        {"name": "garbage", "last_health": "not-a-timestamp"},   # malformed — SKIP
        {"name": "missing"},                                     # absent key — SKIP
    ]
    result = _stale_peers(peers, _NOW_EPOCH, stale_sec=1800)
    assert result == ["anhour", "threedays"]  # sorted, stale only


def test_stale_peers_skips_empty_and_malformed_not_treated_stale():
    peers = [
        {"name": "empty", "last_health": ""},
        {"name": "none", "last_health": None},
        {"name": "garbage", "last_health": "xyz"},
    ]
    assert _stale_peers(peers, _NOW_EPOCH, stale_sec=1) == []


def test_stale_peers_exclude_removes_named_even_if_stale():
    peers = [
        {"name": "myself", "last_health": _iso_ago(99999)},  # stale but excluded
        {"name": "other", "last_health": _iso_ago(99999)},   # stale, kept
    ]
    result = _stale_peers(peers, _NOW_EPOCH, stale_sec=1800, exclude={"myself"})
    assert result == ["other"]


def test_stale_peers_parses_z_suffix_plus0000_and_naive():
    # All three are ~1 hour old → all stale at stale_sec=1800.
    peers = [
        {"name": "zsuffix", "last_health": _iso_ago(3600, suffix="Z")},
        {"name": "plusoffset", "last_health": _iso_ago(3600, suffix="+00:00")},
        {"name": "naive", "last_health": _iso_ago(3600, suffix="")},  # no tz → assume UTC
    ]
    result = _stale_peers(peers, _NOW_EPOCH, stale_sec=1800)
    assert result == ["naive", "plusoffset", "zsuffix"]


def test_stale_peers_boundary_strictly_greater_than():
    # age == stale_sec is NOT stale (strict >).
    peers = [{"name": "exact", "last_health": _iso_ago(1800)}]
    assert _stale_peers(peers, _NOW_EPOCH, stale_sec=1800) == []
    peers = [{"name": "justover", "last_health": _iso_ago(1801)}]
    assert _stale_peers(peers, _NOW_EPOCH, stale_sec=1800) == ["justover"]


# ---------------------------------------------------------------------------
# _fetch_peers — thin network wrapper
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_peers_bare_list_shape():
    payload = [{"name": "a"}, {"name": "b"}]
    fake = _FakeResp(json.dumps(payload).encode("utf-8"))
    with patch("urllib.request.urlopen", return_value=fake):
        out = _fetch_peers("http://gw:8788", "tok")
    assert out == payload


def test_fetch_peers_wrapped_peers_shape():
    payload = {"peers": [{"name": "a"}, {"name": "b"}]}
    fake = _FakeResp(json.dumps(payload).encode("utf-8"))
    with patch("urllib.request.urlopen", return_value=fake):
        out = _fetch_peers("http://gw:8788", "tok")
    assert out == payload["peers"]


def test_fetch_peers_returns_empty_on_urlopen_raise():
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        out = _fetch_peers("http://gw:8788", "tok")
    assert out == []


def test_fetch_peers_returns_empty_on_unexpected_shape():
    fake = _FakeResp(json.dumps({"unexpected": 1}).encode("utf-8"))
    with patch("urllib.request.urlopen", return_value=fake):
        out = _fetch_peers("http://gw:8788", "tok")
    assert out == []
