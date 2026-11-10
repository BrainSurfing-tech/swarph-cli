"""Card #1540 regression tests: test_472_register_caller_binding.py,
test_564_bootstrap_first_peer.py, and test_gateway_brain.py clean up all
temporary directories created for MESH_DB_PATH."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "test_file",
    [
        "tests/test_472_register_caller_binding.py",
        "tests/test_564_bootstrap_first_peer.py",
        "tests/test_gateway_brain.py",
    ],
)
def test_gateway_test_files_leave_zero_tmp_entries(test_file: str, tmp_path: Path) -> None:
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
        timeout=60,
    )
    assert res.returncode == 0, f"pytest {test_file} failed:\n{res.stdout}\n{res.stderr}"
    leftovers = sorted(p.name for p in scratch_tmp.iterdir())
    assert leftovers == [], f"{test_file} leaked temp entries: {leftovers}"
