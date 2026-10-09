"""card #1073 packaging (#1432): the installed wheel runs the router.

Installed 0.72.13 failed `swarph triage --dry-run` with 'owner map
unreadable' — owners.json was never declared as package data, so every
test and the PR dry run passed from the source tree only (the 0.39.3 /
#523 bug a third time). This test builds the wheel, installs it into a
fresh venv, and runs the INSTALLED copy with the source tree nowhere in
reach: triage and sweep dry-runs must get past their data reads (they
stop at the unreachable board, which proves the owner map loaded).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=600, **kwargs)


def _clean_env():
    """The build/install children must not see the source tree: with
    PYTHONPATH=src (which this suite sets) pip finds the in-tree
    egg-info, judges swarph-cli already satisfied, and installs only
    the dependencies — a green-looking install of nothing."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def _venv_python(venv: Path) -> Path:
    exe = venv / "bin" / "python"
    return exe if exe.exists() else venv / "Scripts" / "python.exe"


def _entrypoint(vpy: Path) -> Path:
    """Console-script path beside the venv interpreter (swarph.exe on
    Windows, plain swarph elsewhere)."""
    base = Path(vpy).parent / "swarph"
    if base.is_file():
        return base
    return base.with_suffix(".exe")


@pytest.fixture(scope="module")
def installed_swarph(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("wheel-install")
    wheeldir = tmp / "dist"
    wheeldir.mkdir()
    build_env = _clean_env()
    built = _run([sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
                  "-w", str(wheeldir)], cwd=str(ROOT), env=build_env)
    assert built.returncode == 0, built.stderr[-2000:]
    wheels = sorted(wheeldir.glob("swarph_cli-*.whl"))
    assert len(wheels) == 1, [w.name for w in wheels]
    venv = tmp / "venv"
    created = _run([sys.executable, "-m", "venv", str(venv)],
                   env=build_env)
    assert created.returncode == 0, created.stderr[-2000:]
    vpy = str(_venv_python(venv))
    installed = _run([vpy, "-m", "pip", "install", str(wheels[0])],
                     env=build_env)
    assert installed.returncode == 0, installed.stderr[-2000:]
    assert "swarph-cli" in installed.stdout.replace("_", "-"), \
        installed.stdout[-2000:]
    swarph = str(_entrypoint(Path(vpy)))
    assert os.path.isfile(swarph), f"no swarph entrypoint beside {vpy}"
    return swarph


def _dry_run_env(tmp_path):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # the tree must not leak into the venv
    env.pop("SWARPH_SWEEP_STATE", None)
    env["MESH_GATEWAY_TOKEN"] = "wheel-test-token"
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    return env


def test_installed_triage_reads_its_owner_map(installed_swarph, tmp_path):
    """`swarph triage --dry-run` from the wheel must pass the owner-map
    read. The board is unreachable by design (unroutable gateway), so
    'board unreadable' is the PASS readout; 'owner map unreadable' is
    the 0.72.13 failure."""
    proc = _run(
        [installed_swarph, "triage", "--dry-run", "--as", "wheel-test",
         "--gateway", "http://127.0.0.1:9"],
        cwd=str(tmp_path), env=_dry_run_env(tmp_path))
    assert "owner map unreadable" not in (proc.stdout + proc.stderr), \
        proc.stderr[-2000:]
    assert "board unreadable" in (proc.stdout + proc.stderr), \
        proc.stdout[-2000:] + proc.stderr[-2000:]


def test_installed_sweep_still_dry_runs(installed_swarph, tmp_path):
    """Companion: the installed sweep dry-run passes its own reads too.
    An unreachable board reads rc=1 with 'board unreadable' by design —
    the assertion is that it gets that far, from the wheel."""
    proc = _run(
        [installed_swarph, "sweep", "--dry-run", "--as", "wheel-test",
         "--gateway", "http://127.0.0.1:9"],
        cwd=str(tmp_path), env=_dry_run_env(tmp_path))
    assert "board unreadable" in (proc.stdout + proc.stderr), \
        proc.stdout[-2000:] + proc.stderr[-2000:]
