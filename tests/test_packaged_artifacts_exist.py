"""Files the docs tell operators to run must actually be IN the package.

0.39.3 shipped `deploy/monitor/README.md` instructing peers to call
`scripts/ensure_monitor.sh`, and the wheel did not contain it: non-.py files
need declaring in [tool.setuptools.package-data] and it was not. A clean-room
`pip install` from PyPI is what caught it, after twine, CI and the build all
reported success.

Same class as the deployment-artifact scan (test_shipped_units_match_the_cli):
the docs and the artifact disagreed and nothing compared them.

Run: venv/bin/python -m pytest tests/test_packaged_artifacts_exist.py -v
"""
import pathlib

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent / "src" / "swarph_cli"
# (relative path under the package, why it must ship)
REQUIRED = [
    ("scripts/ensure_monitor.sh",
     "deploy/monitor/README.md tells operators to run it"),
    ("systemd/swarph-watchdog.service",
     "`swarph watchdog --install-service` reads it via importlib.resources"),
    ("systemd/swarph-peer-reply-drain@.service",
     "peer-service reply delivery must be installed with its bounded supervisor"),
    ("systemd/swarph-peer-reply-drain@.timer",
     "peer-service reply delivery must be scheduled after installation"),
    ("systemd/swarph-monitor@.service",
     "#648: `swarph monitor install-unit` reads it via importlib.resources — "
     "the mesh's own DM path was the one supervisor the wheel did not carry"),
]


@pytest.mark.parametrize("rel,why", REQUIRED, ids=[r for r, _ in REQUIRED])
def test_file_exists_in_the_package_tree(rel, why):
    assert (PKG / rel).exists(), f"{rel} missing from the package tree — {why}"


@pytest.mark.parametrize("rel,why", REQUIRED, ids=[r for r, _ in REQUIRED])
def test_file_is_declared_as_package_data(rel, why):
    """Existing in src/ is NOT enough — it must be declared or the wheel omits it."""
    import tomllib
    cfg = tomllib.loads((PKG.parent.parent / "pyproject.toml").read_text())
    patterns = cfg["tool"]["setuptools"]["package-data"]["swarph_cli"]
    suffix = pathlib.Path(rel).suffix
    parent = pathlib.Path(rel).parent.as_posix()
    assert any(p.startswith(parent + "/") and p.endswith(suffix) for p in patterns), (
        f"{rel} exists in src/ but no package-data pattern covers it "
        f"({patterns}) — the wheel will silently omit it. {why}"
    )
