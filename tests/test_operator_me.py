"""#660 — operator surface isolation + behaviour.

The load-bearing invariant: ``swarph_cli.operator`` must not import any
symbol that reads ``$SWARPH_SELF``. Without that test the 2026-08-26 forgery
is only documented in a comment.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from swarph_cli.operator import config as op_config
from swarph_cli.operator.cli import extract_as, main, resolve_session


OPERATOR_ROOT = Path(__file__).resolve().parents[1] / "src" / "swarph_cli" / "operator"

# Modules known to read or fall back to $SWARPH_SELF. Importing ANY of these
# from the operator package is a FAIL under card #660.
FORBIDDEN_IMPORT_ROOTS = {
    "swarph_cli.tokens",
    "swarph_cli.commands.mesh",
    "swarph_cli.commands._gateway_client",
    "swarph_cli.gh_identity",
    "swarph_cli.main",
    "swarph_cli.caller",
    "swarph_cli.gateway_default",
}


def _module_files() -> list[Path]:
    return sorted(OPERATOR_ROOT.rglob("*.py"))


def test_operator_package_never_mentions_swarph_self():
    offenders = []
    for path in _module_files():
        text = path.read_text(encoding="utf-8")
        if "SWARPH_SELF" in text:
            offenders.append(str(path.relative_to(OPERATOR_ROOT.parent.parent)))
    assert not offenders, (
        "swarph_cli.operator must not mention SWARPH_SELF anywhere — "
        f"found in: {offenders}"
    )


def test_operator_import_graph_forbids_cell_identity_modules():
    """AST-level: operator/*.py may only import relative/. or stdlib-safe peers."""
    forbidden_hits: list[str] = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    for root in FORBIDDEN_IMPORT_ROOTS:
                        if name == root or name.startswith(root + "."):
                            forbidden_hits.append(f"{path.name}: import {name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.module is None:
                    continue  # relative `from . import x`
                mod = node.module or ""
                if node.level:
                    # relative within operator package — OK
                    continue
                for root in FORBIDDEN_IMPORT_ROOTS:
                    if mod == root or mod.startswith(root + "."):
                        forbidden_hits.append(f"{path.name}: from {mod}")
                if mod.startswith("swarph_cli.") and not mod.startswith(
                    "swarph_cli.operator"
                ):
                    # Any other swarph_cli import is also forbidden — belt and braces.
                    forbidden_hits.append(f"{path.name}: from {mod}")
    assert not forbidden_hits, (
        "operator package imported cell-identity modules:\n  "
        + "\n  ".join(forbidden_hits)
    )


def test_extract_as_consumes_flag_anywhere_on_the_line():
    # The measured incident: `me move 648 test --as lab-ovh` left --as as a
    # leftover positional when only argv[0] was scanned.
    as_name, rest = extract_as(["move", "648", "test", "--as", "lab-ovh"])
    assert as_name == "lab-ovh"
    assert rest == ["move", "648", "test"]

    as_name, rest = extract_as(["--as", "commander", "cards"])
    assert as_name == "commander"
    assert rest == ["cards"]

    as_name, rest = extract_as(["cards", "ready"])
    assert as_name is None
    assert rest == ["cards", "ready"]


def test_config_default_token_file_is_portable(tmp_path: Path):
    """The default token_file must use ~, not a lab home literal."""
    cfg = op_config.save_config(
        identity="commander",
        gateway="http://example.test:8788",
        path=tmp_path / "operator.json",
    )
    assert cfg.token_file == "~/.config/swarph/commander.peer_token"
    assert "/home/ubuntu" not in cfg.token_file
    assert "tail288b3b" not in (tmp_path / "operator.json").read_text(encoding="utf-8")
    assert "tail288b3b" not in cfg.gateway


def test_resolve_session_ignores_swarph_self_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Can-fail from the card: $SWARPH_SELF=cell must NOT become the actor."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    token = tmp_path / "commander.peer_token"
    token.write_text("test-token-value\n", encoding="utf-8")
    cfg_path = tmp_path / "operator.json"
    op_config.save_config(
        identity="commander",
        gateway="http://example.test:8788",
        token_file=str(token),
        path=cfg_path,
    )
    identity, gateway, tok = resolve_session(None, config_path=cfg_path)
    assert identity == "commander"
    assert gateway == "http://example.test:8788"
    assert tok == "test-token-value"
    # Env still set — prove we did not inherit it.
    assert os.environ.get("SWARPH_SELF") == "lab-ovh"


def test_console_script_entrypoint_declared():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert 'swarph-me = "swarph_cli.operator:main"' in text
    assert 'swarph = "swarph_cli.main:main"' in text


def test_main_config_and_help(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    cfg = tmp_path / "operator.json"
    rc = main([
        "config",
        "--identity", "commander",
        "--gateway", "http://example.test:8788",
        "--token-file", str(tmp_path / "commander.peer_token"),
        "--config", str(cfg),
    ])
    assert rc == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["identity"] == "commander"
    assert data["gateway"] == "http://example.test:8788"

    rc = main(["--config", str(cfg), "help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "swarph-me" in out
    assert "channels" in out
