"""card #1219 — runtime egress probe.

The probe runs under bwrap --unshare-net. An unlisted connect fails it.
A host that cannot prove isolation exits with isolation-unproven.
On main this file's first assertion fails: tests/egress_probe.py is absent.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "egress_probe.py"


def _run(args, **env):
    assert PROBE.is_file(), "runtime egress probe is not in the tree"
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        capture_output=True, text=True, env=merged, timeout=60)


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_os_system_curl_is_an_unlisted_connect():
    out = _run(["--mutant", "os-system-curl"])
    assert out.returncode == 1, out.stderr
    assert "203.0.113.2" in out.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_absolute_curl_is_an_unlisted_connect():
    out = _run(["--mutant", "abs-curl"])
    assert out.returncode == 1, out.stderr
    assert "203.0.113.3" in out.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_asyncio_ssh_is_an_unlisted_connect():
    out = _run(["--mutant", "asyncio-ssh"])
    assert out.returncode == 1, out.stderr
    assert "203.0.113.4" in out.stderr


def test_missing_bwrap_fails_closed_with_a_named_reason():
    out = _run(["--isolation"], EGRESS_PROBE_BWRAP="/no/such/bwrap")
    assert out.returncode == 2, out.stderr
    assert "isolation-unproven: bwrap missing" in out.stderr
