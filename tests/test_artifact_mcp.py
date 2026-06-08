"""``swarph add`` (T3-mcp) — the ``mcp`` handler + ``.mcp.json`` merge core.

Exercises the pure ``.mcp.json`` merge machinery and the full
``run_add`` install path with an injected ``mcp_config_path`` (no CLI
shell-out). Covers:

* ``_merge_mcp_server`` / ``_unmerge_mcp_server`` mutate-and-return,
  idempotent, sibling-preserving
* ``_load_mcp_config`` missing → ``{}``; non-object → ``ValueError``
* ``resolve_builtin_mcp`` known/unknown
* builtin install via the full ``run_add`` path (returns 0, writes the
  reference server spec, idempotent)
* published publisher FAILS CLOSED — non-zero, mutates NOTHING
* an existing ``.mcp.json`` is preserved across a builtin install
"""

from __future__ import annotations

import pytest

from swarph_cli.commands.add import (
    BUILTIN_MCP,
    McpBundle,
    _load_mcp_config,
    _merge_mcp_server,
    _save_mcp_config,
    _unmerge_mcp_server,
    resolve_builtin_mcp,
    run_add,
)


_EVERYTHING_SPEC = {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-everything"],
}


# --------------------------------------------------------------------------- #
# _merge_mcp_server
# --------------------------------------------------------------------------- #


def test_merge_creates_mcp_servers_on_empty():
    config = {}
    out = _merge_mcp_server(config, "everything", _EVERYTHING_SPEC)
    assert out["mcpServers"]["everything"] == _EVERYTHING_SPEC


def test_merge_is_idempotent():
    config = {}
    _merge_mcp_server(config, "everything", _EVERYTHING_SPEC)
    _merge_mcp_server(config, "everything", _EVERYTHING_SPEC)
    assert list(config["mcpServers"]) == ["everything"]
    assert config["mcpServers"]["everything"] == _EVERYTHING_SPEC


def test_merge_second_server_makes_two_entries():
    config = {}
    _merge_mcp_server(config, "everything", _EVERYTHING_SPEC)
    _merge_mcp_server(config, "fmp", {"type": "http", "url": "http://x"})
    assert set(config["mcpServers"]) == {"everything", "fmp"}


def test_merge_preserves_top_level_and_other_server():
    config = {
        "someTopKey": {"k": "v"},
        "mcpServers": {"fmp": {"type": "http", "url": "http://x"}},
    }
    _merge_mcp_server(config, "everything", _EVERYTHING_SPEC)
    assert config["someTopKey"] == {"k": "v"}
    assert config["mcpServers"]["fmp"] == {"type": "http", "url": "http://x"}
    assert config["mcpServers"]["everything"] == _EVERYTHING_SPEC


# --------------------------------------------------------------------------- #
# _unmerge_mcp_server
# --------------------------------------------------------------------------- #


def test_unmerge_removes_and_prunes_empty():
    config = {"mcpServers": {"everything": _EVERYTHING_SPEC}}
    _unmerge_mcp_server(config, "everything")
    assert "mcpServers" not in config


def test_unmerge_noop_on_absent():
    config = {"mcpServers": {"fmp": {"type": "http", "url": "http://x"}}}
    _unmerge_mcp_server(config, "everything")
    assert config["mcpServers"] == {"fmp": {"type": "http", "url": "http://x"}}


def test_unmerge_noop_when_no_mcp_servers_key():
    config = {"other": 1}
    _unmerge_mcp_server(config, "everything")
    assert config == {"other": 1}


def test_unmerge_preserves_siblings():
    config = {
        "mcpServers": {
            "everything": _EVERYTHING_SPEC,
            "fmp": {"type": "http", "url": "http://x"},
        }
    }
    _unmerge_mcp_server(config, "everything")
    assert config["mcpServers"] == {"fmp": {"type": "http", "url": "http://x"}}


# --------------------------------------------------------------------------- #
# _load_mcp_config
# --------------------------------------------------------------------------- #


def test_load_missing_returns_empty(tmp_path):
    assert _load_mcp_config(tmp_path / "nope.json") == {}


def test_load_non_object_raises(tmp_path):
    p = tmp_path / ".mcp.json"
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_mcp_config(p)


def test_load_corrupt_raises(tmp_path):
    p = tmp_path / ".mcp.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_mcp_config(p)


def test_save_then_load_roundtrips(tmp_path):
    p = tmp_path / "sub" / ".mcp.json"
    obj = {"mcpServers": {"everything": _EVERYTHING_SPEC}}
    _save_mcp_config(p, obj)
    assert _load_mcp_config(p) == obj


# --------------------------------------------------------------------------- #
# resolve_builtin_mcp
# --------------------------------------------------------------------------- #


def test_resolve_builtin_everything():
    bundle = resolve_builtin_mcp("everything")
    assert isinstance(bundle, McpBundle)
    assert bundle.trust == "builtin"
    assert bundle.publisher == "swarph-builtin"
    assert bundle.server_spec == _EVERYTHING_SPEC


def test_resolve_builtin_unknown_raises_naming_everything():
    with pytest.raises(ValueError) as exc:
        resolve_builtin_mcp("does-not-exist")
    assert "everything" in str(exc.value)


def test_builtin_mcp_catalog_has_everything():
    assert "everything" in BUILTIN_MCP


# --------------------------------------------------------------------------- #
# builtin install via the full run_add path
# --------------------------------------------------------------------------- #


def test_builtin_mcp_installs(tmp_path):
    mcp_path = tmp_path / ".mcp.json"
    rc = run_add(
        ["swarph://mcp/swarph-builtin/everything", "--yes"],
        mcp_config_path=mcp_path,
    )
    assert rc == 0
    config = _load_mcp_config(mcp_path)
    assert config["mcpServers"]["everything"] == _EVERYTHING_SPEC


def test_builtin_mcp_install_is_idempotent(tmp_path):
    mcp_path = tmp_path / ".mcp.json"
    run_add(
        ["swarph://mcp/swarph-builtin/everything", "--yes"],
        mcp_config_path=mcp_path,
    )
    rc = run_add(
        ["swarph://mcp/swarph-builtin/everything", "--yes"],
        mcp_config_path=mcp_path,
    )
    assert rc == 0
    config = _load_mcp_config(mcp_path)
    assert list(config["mcpServers"]) == ["everything"]


# --------------------------------------------------------------------------- #
# published publisher FAILS CLOSED
# --------------------------------------------------------------------------- #


def test_published_mcp_fails_closed(tmp_path, capsys):
    mcp_path = tmp_path / ".mcp.json"
    rc = run_add(
        ["swarph://mcp/lab-ovh/foo", "--yes"],
        mcp_config_path=mcp_path,
    )
    assert rc != 0
    assert not mcp_path.exists()
    combined = capsys.readouterr()
    assert "not yet trusted" in (combined.out + combined.err)


# --------------------------------------------------------------------------- #
# existing .mcp.json preserved across a builtin install
# --------------------------------------------------------------------------- #


def test_existing_mcp_config_preserved(tmp_path):
    mcp_path = tmp_path / ".mcp.json"
    _save_mcp_config(
        mcp_path, {"mcpServers": {"fmp": {"type": "http", "url": "http://x"}}}
    )
    rc = run_add(
        ["swarph://mcp/swarph-builtin/everything", "--yes"],
        mcp_config_path=mcp_path,
    )
    assert rc == 0
    config = _load_mcp_config(mcp_path)
    assert config["mcpServers"]["fmp"] == {"type": "http", "url": "http://x"}
    assert config["mcpServers"]["everything"] == _EVERYTHING_SPEC
