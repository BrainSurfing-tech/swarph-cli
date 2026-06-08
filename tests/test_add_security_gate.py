"""§3.1 watchtower v1 — install-path static-scan gate tests.

The static scan runs AFTER ``_hash_guard`` passes and BEFORE the actual
install. A ``FAIL`` verdict refuses install with exit code 7, mutating
NOTHING (defense-in-depth — even a builtin tripping a HIGH rule is refused).

Covers:

* a real builtin install still works (a PASS verdict doesn't block).
* monkeypatched ``static_scan`` → FAIL refuses with code 7, nothing written
  (hook + mcp classes).
"""

from __future__ import annotations

from swarph_cli.commands import security
from swarph_cli.commands.add import run_add
from swarph_cli.commands.security import ScanFinding, ScanResult


def _fail_result(*_args, **_kwargs):
    return ScanResult(
        verdict="FAIL",
        findings=(
            ScanFinding(
                severity="high",
                rule="test-injected",
                message="injected FAIL for the gate test",
                excerpt="x",
            ),
        ),
    )


def test_builtin_install_passes_gate(tmp_path):
    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    rc = run_add(
        ["swarph://hook/swarph-builtin/cell-resilience", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 0
    assert (hooks_home / "cell-resilience.sh").exists()


def test_gate_refuses_hook_on_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "static_scan", _fail_result)

    settings_path = tmp_path / "settings.json"
    hooks_home = tmp_path / "hooks"
    rc = run_add(
        ["swarph://hook/swarph-builtin/cell-resilience", "--yes"],
        settings_path=settings_path,
        hooks_home=hooks_home,
    )
    assert rc == 7
    # NOTHING written.
    assert not settings_path.exists()
    assert not (hooks_home / "cell-resilience.sh").exists()


def test_gate_refuses_mcp_on_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "static_scan", _fail_result)

    mcp_config = tmp_path / ".mcp.json"
    rc = run_add(
        ["swarph://mcp/swarph-builtin/everything", "--yes"],
        mcp_config_path=mcp_config,
    )
    assert rc == 7
    assert not mcp_config.exists()
