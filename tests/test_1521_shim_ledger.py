"""Tests for Card #1521 — swarph-cli shim firing ledger helper and pilot wiring."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from swarph_cli import shim_ledger
from swarph_cli.commands import codegraph_hook, sweep, wake_hook_output, watchdog


def test_record_shim_firing_writes_to_swarph_state_shim_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SWARPH_SHIM_LEDGER_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SWARPH_SELF", "gemini-researcher")

    ok = shim_ledger.record_shim_firing(
        "claude-settings-sessionstart-wake-hook",
        outcome="fired",
        ts="2026-11-09T16:30:00+00:00",
    )
    assert ok is True

    target = tmp_path / "swarph_state" / "shim-ledger" / "firings.jsonl"
    assert target.is_file()
    row = json.loads(target.read_text(encoding="utf-8").strip())
    assert list(row.keys()) == ["ts", "shim", "cell", "outcome"]
    assert row == {
        "ts": "2026-11-09T16:30:00+00:00",
        "shim": "claude-settings-sessionstart-wake-hook",
        "cell": "gemini-researcher",
        "outcome": "fired",
    }


def test_record_shim_firing_swallows_write_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "regular_file"
    blocker.write_text("not a dir", encoding="utf-8")
    assert shim_ledger.record_shim_firing("swarph-watchdog", ledger_dir=blocker / "sub") is False

    def raise_io_error(target_file: Path, record: dict[str, str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(shim_ledger, "_write_firing_line", raise_io_error)
    assert shim_ledger.record_shim_firing("swarph-watchdog", ledger_dir=tmp_path) is False


def test_four_swarph_cli_pilot_shims_record_firings_and_survive_ledger_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_dir = tmp_path / "shim-ledger"
    monkeypatch.setenv("SWARPH_SHIM_LEDGER_DIR", str(ledger_dir))

    # 1. claude-settings-sessionstart-wake-hook
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    rc_wake = wake_hook_output.run_wake_hook_output(["--harness", "claude", "--cell", "lab-ovh"])
    assert rc_wake == 0

    # 2. claude-settings-posttooluse-codegraph-on-grep
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s1"})),
    )
    rc_cg = codegraph_hook.run_codegraph_hook(["--as", "lab-ovh"])
    assert rc_cg == 0

    # 3. swarph-watchdog
    monkeypatch.setattr(watchdog, "run_check", lambda args: 0)
    rc_wd = watchdog.run_watchdog(["--check", "--cell", "lab-ovh"])
    assert rc_wd == 0

    # 4. swarph-stuck-sweep
    rc_sw, findings = sweep.run_sweep(
        gateway="http://127.0.0.1:8788",
        token="tok",
        sender="lab-ovh",
        repos=[],
        state_file=str(tmp_path / "state.json"),
        report_file=str(tmp_path / "report.log"),
        now=1700000000.0,
        http_get=lambda url, tok: (200, {"obligations": [], "peers": []}),
        http_post=lambda url, body, tok: (201, {}),
        gh_prs=lambda repo: [],
        dry_run=False,
    )
    assert rc_sw == 0
    assert findings == []

    lines = (ledger_dir / "firings.jsonl").read_text(encoding="utf-8").strip().splitlines()
    recorded_shims = [json.loads(line)["shim"] for line in lines]
    assert "claude-settings-sessionstart-wake-hook" in recorded_shims
    assert "claude-settings-posttooluse-codegraph-on-grep" in recorded_shims
    assert "swarph-watchdog" in recorded_shims
    assert "swarph-stuck-sweep" in recorded_shims

    # Now make _write_firing_line raise PermissionError and verify all 4 shims still succeed
    def broken_write(target_file: Path, record: dict[str, str]) -> None:
        raise PermissionError("read-only ledger")

    monkeypatch.setattr(shim_ledger, "_write_firing_line", broken_write)

    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert wake_hook_output.run_wake_hook_output(["--harness", "claude", "--cell", "lab-ovh"]) == 0

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s2"})),
    )
    assert codegraph_hook.run_codegraph_hook(["--as", "lab-ovh"]) == 0

    assert watchdog.run_watchdog(["--check", "--cell", "lab-ovh"]) == 0

    rc_sw2, _ = sweep.run_sweep(
        gateway="http://127.0.0.1:8788",
        token="tok",
        sender="lab-ovh",
        repos=[],
        state_file=str(tmp_path / "state.json"),
        report_file=str(tmp_path / "report.log"),
        now=1700000000.0,
        http_get=lambda url, tok: (200, {"obligations": [], "peers": []}),
        http_post=lambda url, body, tok: (201, {}),
        gh_prs=lambda repo: [],
        dry_run=False,
    )
    assert rc_sw2 == 0


def test_mutant_propagating_write_error_fails_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def check_fault_isolation(fn) -> None:
        try:
            res = fn("swarph-watchdog", ledger_dir=tmp_path)
        except Exception as exc:
            raise AssertionError(f"shim broken by propagating write error: {exc}") from exc
        if res is not False:
            raise AssertionError(f"expected False on failed write, got {res!r}")

    monkeypatch.setattr(
        shim_ledger,
        "_write_firing_line",
        lambda target_file, record: (_ for _ in ()).throw(OSError("simulated failure")),
    )

    check_fault_isolation(shim_ledger.record_shim_firing)

    def mutant_fn(shim: str, *, ledger_dir: Path) -> bool:
        shim_ledger._write_firing_line(
            ledger_dir / "firings.jsonl",
            {"ts": "t", "shim": shim, "cell": "lab-ovh", "outcome": "fired"},
        )
        return True

    with pytest.raises(AssertionError, match="shim broken by propagating write error"):
        check_fault_isolation(mutant_fn)
