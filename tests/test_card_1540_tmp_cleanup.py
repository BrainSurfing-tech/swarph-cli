"""Card #1540 regression tests:
1. test_472_register_caller_binding.py, test_564_bootstrap_first_peer.py, and
   test_gateway_brain.py clean up all temporary directories created for MESH_DB_PATH.
2. release.sh removes its scratch dir on every exit (normal and errors under set -e).
3. cell_probe's temp json is removed after use on every path while keeping its
   output and exit codes identical.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from swarph_cli.commands import cell_probe as cp

pytest.importorskip("fastapi")
pytest.importorskip("jwt")

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO_ROOT / "release.sh"


@pytest.mark.parametrize(
    "test_file",
    [
        "tests/test_472_register_caller_binding.py",
        "tests/test_564_bootstrap_first_peer.py",
        "tests/test_gateway_brain.py",
        "tests/test_1065_release.py",
        "tests/test_139_tool_probe.py",
    ],
)
def test_suite_files_leave_zero_tmp_entries(test_file: str, tmp_path: Path) -> None:
    scratch_tmp = tmp_path / "scratch_tmp"
    scratch_tmp.mkdir()
    basetemp = tmp_path / "basetemp"
    env = os.environ.copy()
    env["TMPDIR"] = str(scratch_tmp)
    env["TEMP"] = str(scratch_tmp)
    env["TMP"] = str(scratch_tmp)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT / "src")

    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            f"--basetemp={basetemp}",
            "-q",
            test_file,
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert res.returncode == 0, f"pytest {test_file} failed:\n{res.stdout}\n{res.stderr}"
    leftovers = sorted(p.name for p in scratch_tmp.iterdir())
    assert leftovers == [], f"{test_file} leaked temp entries: {leftovers}"


def test_release_sh_removes_scratch_on_error_under_set_e(tmp_path: Path) -> None:
    """When git clone fails under set -e inside open_release_pr, the EXIT trap
    still removes the mktemp -d scratch directory."""
    scratch_tmp = tmp_path / "scratch_tmp"
    scratch_tmp.mkdir()
    env = os.environ.copy()
    env["TMPDIR"] = str(scratch_tmp)
    env["TEMP"] = str(scratch_tmp)
    env["TMP"] = str(scratch_tmp)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_ALLOW_PROTOCOL"] = "file"
    env["RELEASE_REMOTE"] = (tmp_path / "nonexistent-bare-repo.git").as_uri()

    res = subprocess.run(
        ["bash", str(RELEASE_SH), "9.9.9"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode != 0
    leftovers = sorted(p.name for p in scratch_tmp.iterdir())
    assert leftovers == [], f"release.sh leaked scratch dir on error exit: {leftovers}"


def test_cell_probe_removes_temp_json_and_preserves_output_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """cell_probe's mcp-config can_fail removes its temporary .json file on both
    normal return and exception paths, and keeps output and exit codes identical."""
    scratch_tmp = tmp_path / "scratch_tmp"
    scratch_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch_tmp))
    monkeypatch.setenv("TEMP", str(scratch_tmp))
    monkeypatch.setenv("TMP", str(scratch_tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))

    # 1. Direct Verdict comparison against baseline probe_mcp_config on the same doc
    ref_file = tmp_path / "ref_can_fail.json"
    ref_file.write_text(
        json.dumps({"mcpServers": {"swarph": {"env": {"MESH_GATEWAY_URL": "x"}}}}),
        encoding="utf-8",
    )
    expected_verdict = cp.probe_mcp_config(str(ref_file))
    actual_verdict = cp.PROBES["mcp-config"].can_fail()
    assert actual_verdict == expected_verdict
    assert actual_verdict.state == cp.State.ABSENT
    assert list(scratch_tmp.iterdir()) == []

    # 2. CLI --can-fail text and --json outputs and exit codes
    rc_text = cp.run_cell_probe(["--only", "mcp-config", "--can-fail"])
    out_text = capsys.readouterr().out
    assert rc_text == 0
    assert (
        f"swarph cell probe  (cell_probe.py sha256 {cp.self_sha256()})\n"
        f"  mcp-config   [ABSENT        ] consumer=mcp-server       "
        f"{expected_verdict.detail}\n"
    ) == out_text
    assert list(scratch_tmp.iterdir()) == []

    rc_json = cp.run_cell_probe(["--only", "mcp-config", "--can-fail", "--json"])
    out_json = capsys.readouterr().out
    assert rc_json == 0
    assert json.loads(out_json) == [
        {
            "check": "mcp-config",
            "verdict": "ABSENT",
            "reason": "SWARPH_BRAIN_MCP missing in 1 of 1 mcp entry",
        }
    ]
    assert list(scratch_tmp.iterdir()) == []

    # 3. Even if probe_mcp_config raises inside _mcp_config_can_fail, temp file is unlinked
    def _boom(_path: str | None = None) -> cp.Verdict:
        raise RuntimeError("simulated probe failure")

    monkeypatch.setattr(cp, "probe_mcp_config", _boom)
    with pytest.raises(RuntimeError, match="simulated probe failure"):
        cp.PROBES["mcp-config"].can_fail()
    assert list(scratch_tmp.iterdir()) == []
