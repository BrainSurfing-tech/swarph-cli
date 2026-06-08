"""T4: ``#sha256`` content-hash verification in ``swarph add``.

A ``swarph://...#<sha256>`` URI carries the content-addressed digest that
lets a metaedge magnet link be verified against the actual artifact bytes
regardless of which cell served it. These tests cover:

* the pure hash primitives (:func:`sha256_hex` / :func:`verify_sha256`);
* the per-class canonical-bytes serialization;
* correct-hash → installs (returns 0) for hook / mcp / skill;
* wrong-hash → refuses (returns 5), mutating NOTHING;
* no-hash → installs unchanged (regression guard);
* the trust-boundary predicate.
"""

from __future__ import annotations

import json

import pytest

from swarph_cli.commands.add import (
    HookHandler,
    McpHandler,
    SkillHandler,
    run_add,
    sha256_hex,
    verify_sha256,
    _is_trusted_publisher,
)
from swarph_cli.commands import hooks
from swarph_cli.commands.add import resolve_builtin_mcp, resolve_builtin_skill


# --------------------------------------------------------------------------- #
# Pure hash primitives
# --------------------------------------------------------------------------- #


def test_sha256_hex_known_digest():
    assert (
        sha256_hex(b"abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_verify_sha256_case_insensitive():
    digest = sha256_hex(b"abc")
    assert verify_sha256(b"abc", digest.upper()) is True
    assert verify_sha256(b"abc", f"  {digest}  ") is True


def test_verify_sha256_wrong_is_false():
    assert verify_sha256(b"abc", sha256_hex(b"not-it")) is False


def test_is_trusted_publisher():
    assert _is_trusted_publisher("swarph-builtin") is True
    assert _is_trusted_publisher("lab-ovh") is False


# --------------------------------------------------------------------------- #
# Correct-hash installs (per class)
# --------------------------------------------------------------------------- #


def test_correct_hash_installs_hook(tmp_path):
    bundle = hooks.resolve_builtin("cell-resilience")
    sha = sha256_hex(HookHandler()._canonical_bytes(bundle))
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_add(
        [f"swarph://hook/swarph-builtin/cell-resilience#{sha}", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 0
    assert (hooks_home / "cell-resilience.sh").exists()
    assert settings_path.exists()


def test_correct_hash_installs_mcp(tmp_path):
    bundle = resolve_builtin_mcp("everything")
    sha = sha256_hex(McpHandler()._canonical_bytes(bundle))
    mcp_config_path = tmp_path / ".mcp.json"

    rc = run_add(
        [f"swarph://mcp/swarph-builtin/everything#{sha}", "--yes"],
        mcp_config_path=mcp_config_path,
    )
    assert rc == 0
    assert mcp_config_path.exists()
    cfg = json.loads(mcp_config_path.read_text())
    assert "everything" in cfg["mcpServers"]


def test_correct_hash_installs_skill(tmp_path):
    bundle = resolve_builtin_skill("swarph-intro")
    sha = sha256_hex(SkillHandler()._canonical_bytes(bundle))
    skills_home = tmp_path / "skills"

    rc = run_add(
        [f"swarph://skill/swarph-builtin/swarph-intro#{sha}", "--yes"],
        skills_home=skills_home,
    )
    assert rc == 0
    assert (skills_home / "swarph-intro" / "SKILL.md").exists()


# --------------------------------------------------------------------------- #
# Wrong-hash refuses (per class) — mutates nothing
# --------------------------------------------------------------------------- #


def _wrong_sha():
    return sha256_hex(b"not-it")


def test_wrong_hash_refuses_hook(tmp_path, capsys):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    rc = run_add(
        [f"swarph://hook/swarph-builtin/cell-resilience#{_wrong_sha()}", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 5
    assert not settings_path.exists()
    assert not (hooks_home / "cell-resilience.sh").exists()


def test_wrong_hash_message_hook(tmp_path, capsys):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"

    run_add(
        [f"swarph://hook/swarph-builtin/cell-resilience#{_wrong_sha()}", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "content-hash mismatch" in combined


def test_wrong_hash_refuses_mcp(tmp_path, capsys):
    mcp_config_path = tmp_path / ".mcp.json"

    rc = run_add(
        [f"swarph://mcp/swarph-builtin/everything#{_wrong_sha()}", "--yes"],
        mcp_config_path=mcp_config_path,
    )
    assert rc == 5
    assert not mcp_config_path.exists()


def test_wrong_hash_message_mcp(tmp_path, capsys):
    mcp_config_path = tmp_path / ".mcp.json"
    run_add(
        [f"swarph://mcp/swarph-builtin/everything#{_wrong_sha()}", "--yes"],
        mcp_config_path=mcp_config_path,
    )
    captured = capsys.readouterr()
    assert "content-hash mismatch" in (captured.out + captured.err)


def test_wrong_hash_refuses_skill(tmp_path, capsys):
    skills_home = tmp_path / "skills"

    rc = run_add(
        [f"swarph://skill/swarph-builtin/swarph-intro#{_wrong_sha()}", "--yes"],
        skills_home=skills_home,
    )
    assert rc == 5
    assert not (skills_home / "swarph-intro").exists()


def test_wrong_hash_message_skill(tmp_path, capsys):
    skills_home = tmp_path / "skills"
    run_add(
        [f"swarph://skill/swarph-builtin/swarph-intro#{_wrong_sha()}", "--yes"],
        skills_home=skills_home,
    )
    captured = capsys.readouterr()
    assert "content-hash mismatch" in (captured.out + captured.err)


# --------------------------------------------------------------------------- #
# No-hash still installs (regression guard)
# --------------------------------------------------------------------------- #


def test_no_hash_still_installs_hook(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    rc = run_add(
        ["swarph://hook/swarph-builtin/cell-resilience", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 0
    assert (hooks_home / "cell-resilience.sh").exists()


def test_no_hash_still_installs_mcp(tmp_path):
    mcp_config_path = tmp_path / ".mcp.json"
    rc = run_add(
        ["swarph://mcp/swarph-builtin/everything", "--yes"],
        mcp_config_path=mcp_config_path,
    )
    assert rc == 0
    assert mcp_config_path.exists()


def test_no_hash_still_installs_skill(tmp_path):
    skills_home = tmp_path / "skills"
    rc = run_add(
        ["swarph://skill/swarph-builtin/swarph-intro", "--yes"],
        skills_home=skills_home,
    )
    assert rc == 0
    assert (skills_home / "swarph-intro" / "SKILL.md").exists()
