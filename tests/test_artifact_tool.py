"""``swarph add`` (T3-tool) — the ``tool`` handler + mesh lane-config install.

The 4th artifact class. ``swarph add swarph://tool/swarph-builtin/<adapter>``
bridges to swarph-mesh's adapter registry and records the chosen adapter as an
available ``$0-first`` mesh lane in a local config (``~/.swarph/tool_lanes.json``).

Covers:

* lane machinery: ``_merge_lane`` creates ``lanes.<name>``, idempotent,
  preserves siblings + top-level keys; ``_load_lanes`` non-object → ValueError,
  missing → ``{}``.
* ``resolve_builtin_tool`` known (real swarph-mesh adapter) → ``(name, dict)``;
  unknown → ValueError.
* builtin install via full ``run_add`` (returns 0, records the lane, idempotent).
* published publisher FAILS CLOSED — returns 2, writes NOTHING.
* swarph-mesh-absent path is GRACEFUL — returns 6, writes nothing, message
  mentions swarph-mesh (patched at the ``resolve_builtin_tool`` boundary).
* ``#sha256`` mismatch → returns 5, writes nothing; correct sha installs.
"""

from __future__ import annotations

import pytest

from swarph_cli.commands import add as add_mod
from swarph_cli.commands.add import (
    ToolHandler,
    _load_lanes,
    _merge_lane,
    _save_lanes,
    resolve_builtin_tool,
    run_add,
)

# A real swarph-mesh builtin adapter name (v0.5.0 ships gemini + deepseek +
# claude + openai + grok). We use "gemini" — the $0-first subscription lane.
REAL_ADAPTER = "gemini"


# --------------------------------------------------------------------------- #
# lane machinery
# --------------------------------------------------------------------------- #


def test_merge_lane_creates_lanes_key():
    cfg = _merge_lane({}, "gemini", {"name": "gemini"})
    assert cfg["lanes"]["gemini"] == {"name": "gemini"}


def test_merge_lane_idempotent():
    cfg = _merge_lane({}, "gemini", {"name": "gemini"})
    cfg2 = _merge_lane(cfg, "gemini", {"name": "gemini"})
    assert cfg2["lanes"] == {"gemini": {"name": "gemini"}}


def test_merge_lane_preserves_siblings_and_top_level():
    cfg = {"lanes": {"openai": {"name": "openai"}}, "version": 1}
    cfg = _merge_lane(cfg, "gemini", {"name": "gemini"})
    assert cfg["lanes"]["openai"] == {"name": "openai"}
    assert cfg["lanes"]["gemini"] == {"name": "gemini"}
    assert cfg["version"] == 1


def test_load_lanes_missing_returns_empty(tmp_path):
    assert _load_lanes(tmp_path / "nope.json") == {}


def test_load_lanes_non_object_raises(tmp_path):
    p = tmp_path / "tool_lanes.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_lanes(p)


def test_load_lanes_corrupt_raises(tmp_path):
    p = tmp_path / "tool_lanes.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_lanes(p)


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / "sub" / "tool_lanes.json"
    obj = {"lanes": {"gemini": {"name": "gemini"}}}
    _save_lanes(p, obj)
    assert _load_lanes(p) == obj


# --------------------------------------------------------------------------- #
# resolve_builtin_tool — the swarph-mesh bridge
# --------------------------------------------------------------------------- #


def test_resolve_builtin_tool_known():
    name, spec = resolve_builtin_tool(REAL_ADAPTER)
    assert name == REAL_ADAPTER
    assert isinstance(spec, dict)
    assert spec  # non-empty
    assert spec["name"] == REAL_ADAPTER


def test_resolve_builtin_tool_unknown_raises():
    with pytest.raises(ValueError):
        resolve_builtin_tool("no-such-adapter-xyz")


# --------------------------------------------------------------------------- #
# builtin install via run_add
# --------------------------------------------------------------------------- #


def test_builtin_install_records_lane(tmp_path):
    lanes_path = tmp_path / "tool_lanes.json"
    rc = run_add(
        [f"swarph://tool/swarph-builtin/{REAL_ADAPTER}", "--yes"],
        lanes_path=lanes_path,
    )
    assert rc == 0
    cfg = _load_lanes(lanes_path)
    _, spec = resolve_builtin_tool(REAL_ADAPTER)
    assert cfg["lanes"][REAL_ADAPTER] == spec


def test_builtin_install_idempotent(tmp_path):
    lanes_path = tmp_path / "tool_lanes.json"
    rc1 = run_add(
        [f"swarph://tool/swarph-builtin/{REAL_ADAPTER}", "--yes"],
        lanes_path=lanes_path,
    )
    rc2 = run_add(
        [f"swarph://tool/swarph-builtin/{REAL_ADAPTER}", "--yes"],
        lanes_path=lanes_path,
    )
    assert rc1 == 0 and rc2 == 0
    cfg = _load_lanes(lanes_path)
    assert list(cfg["lanes"]) == [REAL_ADAPTER]


def test_unknown_builtin_name_writes_nothing(tmp_path):
    lanes_path = tmp_path / "tool_lanes.json"
    rc = run_add(
        ["swarph://tool/swarph-builtin/no-such-adapter-xyz", "--yes"],
        lanes_path=lanes_path,
    )
    assert rc == 2  # ValueError caught at run_add layer
    assert not lanes_path.exists()


# --------------------------------------------------------------------------- #
# published publisher FAILS CLOSED
# --------------------------------------------------------------------------- #


def test_published_tool_fails_closed(tmp_path):
    lanes_path = tmp_path / "tool_lanes.json"
    rc = run_add(["swarph://tool/lab-ovh/x", "--yes"], lanes_path=lanes_path)
    assert rc == 2
    assert not lanes_path.exists()


# --------------------------------------------------------------------------- #
# swarph-mesh-absent → graceful (code 6)
# --------------------------------------------------------------------------- #


def test_mesh_absent_is_graceful(tmp_path, monkeypatch, capsys):
    lanes_path = tmp_path / "tool_lanes.json"

    def _raise(name):
        raise RuntimeError(
            "tool install requires swarph-mesh — pip install swarph-mesh"
        )

    monkeypatch.setattr(add_mod, "resolve_builtin_tool", _raise)
    rc = run_add(
        [f"swarph://tool/swarph-builtin/{REAL_ADAPTER}", "--yes"],
        lanes_path=lanes_path,
    )
    assert rc == 6
    assert not lanes_path.exists()
    out = capsys.readouterr().out
    assert "swarph-mesh" in out


# --------------------------------------------------------------------------- #
# #sha256 verification
# --------------------------------------------------------------------------- #


def _canonical_sha(name):
    _, spec = resolve_builtin_tool(name)
    handler = ToolHandler()
    canonical = handler._canonical_bytes(spec)
    import hashlib

    return hashlib.sha256(canonical).hexdigest()


def test_hash_mismatch_writes_nothing(tmp_path):
    lanes_path = tmp_path / "tool_lanes.json"
    rc = run_add(
        [f"swarph://tool/swarph-builtin/{REAL_ADAPTER}#deadbeef", "--yes"],
        lanes_path=lanes_path,
    )
    assert rc == 5
    assert not lanes_path.exists()


def test_correct_hash_installs(tmp_path):
    lanes_path = tmp_path / "tool_lanes.json"
    good = _canonical_sha(REAL_ADAPTER)
    rc = run_add(
        [f"swarph://tool/swarph-builtin/{REAL_ADAPTER}#{good}", "--yes"],
        lanes_path=lanes_path,
    )
    assert rc == 0
    cfg = _load_lanes(lanes_path)
    assert REAL_ADAPTER in cfg["lanes"]
