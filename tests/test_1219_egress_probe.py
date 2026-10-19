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


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_ipv6_literal_connect_is_caught():
    out = _run(["--mutant", "ipv6"])
    assert out.returncode == 1, out.stderr
    assert "2001:db8::1" in out.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_non_isolating_bwrap_shim_exits_2(tmp_path):
    shim = tmp_path / "bwrap"
    shim.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    *strace) break ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        "exec \"$@\"\n",
        encoding="utf-8")
    shim.chmod(0o700)
    out = _run(["--isolation"], EGRESS_PROBE_BWRAP=str(shim))
    assert out.returncode == 2, out.stderr
    assert "isolation-unproven:" in out.stderr
    assert "ENETUNREACH" in out.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_planted_unlisted_connect_in_src_fails_the_probe(tmp_path):
    src = tmp_path / "src" / "swarph_cli"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    # Fixed mutant, not external input. The test reaches it by calling leak().
    (src / "stall_alert.py").write_text(
        "import os\n"
        "def leak():\n"
        "    os.system('curl --max-time 1 -s -o /dev/null "
        "http://203.0.113.50/')\n",
        encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_zz_leak.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
        "from swarph_cli.stall_alert import leak\n"
        "def test_leak():\n"
        "    leak()\n",
        encoding="utf-8")
    assert PROBE.is_file(), "runtime egress probe is not in the tree"
    sys.path.insert(0, str(PROBE.parent))
    import egress_probe
    assert hasattr(egress_probe, "run_pytest")
    # Empty allowlist: the planted 203.0.113.50 literal is unlisted.
    code = egress_probe.run_pytest(tmp_path, ["tests/test_zz_leak.py"], set())
    assert code == 1, code
