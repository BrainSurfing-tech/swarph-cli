"""Tests for ``swarph mcp-server`` — Door 1 of the reach strategy.

These test the plain helper functions (``_search`` / ``_add`` / ``_describe``)
and the static registration of the three MCP tools on the FastMCP server. They
do NOT spin up the stdio server — that's an integration concern owned by the
MCP host.
"""

from __future__ import annotations

import asyncio
import io
import json
from unittest import mock

import pytest

from swarph_cli.commands import mcp_server
from swarph_cli import main as cli_main


# --------------------------------------------------------------------------- #
# _search — urllib POST to metaedge.surf /api/ask
# --------------------------------------------------------------------------- #


def _fake_urlopen_returning(body: dict):
    """Build a urlopen stand-in that returns a response yielding ``body``."""

    def _fake(req, timeout=None):  # noqa: ANN001 - test double
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(body).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        return resp

    return _fake


def test_search_returns_results_list():
    body = {
        "mode": "results",
        "answer": "found 1",
        "results": [
            {
                "name": "cell-resilience",
                "cell": "swarph-builtin",
                "what_it_does": "a hook",
                "how_to_request": "swarph add ...",
                "score": 0.9,
                "swarph_uri": "swarph://hook/swarph-builtin/cell-resilience@1.0",
            }
        ],
        "token": "tok",
    }
    with mock.patch(
        "swarph_cli.commands.mcp_server.urllib.request.urlopen",
        _fake_urlopen_returning(body),
    ):
        out = mcp_server._search("resilience", url="https://metaedge.surf")
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["name"] == "cell-resilience"
    assert out[0]["swarph_uri"].startswith("swarph://")


def test_search_returns_empty_on_error():
    def _boom(req, timeout=None):  # noqa: ANN001 - test double
        raise OSError("network down")

    with mock.patch(
        "swarph_cli.commands.mcp_server.urllib.request.urlopen", _boom
    ):
        out = mcp_server._search("anything", url="https://metaedge.surf")
    assert out == []


def test_search_passes_token_when_given():
    captured = {}

    def _fake(req, timeout=None):  # noqa: ANN001 - test double
        captured["data"] = json.loads(req.data.decode("utf-8"))
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps({"results": []}).encode("utf-8")
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: False
        return resp

    with mock.patch(
        "swarph_cli.commands.mcp_server.urllib.request.urlopen", _fake
    ):
        mcp_server._search("q", url="https://metaedge.surf/", token="secret")
    assert captured["data"]["message"] == "q"
    assert captured["data"]["token"] == "secret"


# --------------------------------------------------------------------------- #
# _add — parse_uri gate + add.run_add dispatch
# --------------------------------------------------------------------------- #


_VALID_URI = "swarph://hook/swarph-builtin/cell-resilience@1.0#abc"


def test_add_installed_true_on_zero():
    with mock.patch(
        "swarph_cli.commands.add.run_add", return_value=0
    ) as m_run:
        out = mcp_server._add(_VALID_URI)
    m_run.assert_called_once()
    assert out["installed"] is True
    assert out["code"] == 0


def test_add_installed_false_on_nonzero():
    with mock.patch(
        "swarph_cli.commands.add.run_add", return_value=2
    ) as m_run:
        out = mcp_server._add(_VALID_URI)
    m_run.assert_called_once()
    assert out["installed"] is False
    assert out["code"] == 2


def test_add_malformed_uri_does_not_call_run_add():
    with mock.patch("swarph_cli.commands.add.run_add") as m_run:
        out = mcp_server._add("http://x")
    m_run.assert_not_called()
    assert out["installed"] is False
    assert out["code"] == 2
    assert "detail" in out


# --------------------------------------------------------------------------- #
# _describe — parse_uri introspection, no install
# --------------------------------------------------------------------------- #


def test_describe_valid_uri():
    out = mcp_server._describe(_VALID_URI)
    assert out == {
        "class": "hook",
        "publisher": "swarph-builtin",
        "name": "cell-resilience",
        "version": "1.0",
        "sha256": "abc",
    }


def test_describe_bad_uri_returns_error():
    out = mcp_server._describe("http://x")
    assert "error" in out
    assert "class" not in out


# --------------------------------------------------------------------------- #
# Wiring + FastMCP tool registration
# --------------------------------------------------------------------------- #


def test_verb_handler_registered():
    assert "mcp-server" in cli_main._VERB_HANDLERS
    assert (
        cli_main._VERB_HANDLERS["mcp-server"]
        == "swarph_cli.commands.mcp_server.run_mcp_server"
    )


def test_three_tools_registered_on_server():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"swarph_search", "swarph_add", "swarph_describe"} <= names
