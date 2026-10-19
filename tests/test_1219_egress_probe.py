"""card #1219 / #1225 — runtime egress probe.

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


def _run(args, timeout=60, **env):
    assert PROBE.is_file(), "runtime egress probe is not in the tree"
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(
        [sys.executable, str(PROBE), *args],
        capture_output=True, text=True, env=merged, timeout=timeout)


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


# The four lines run_pytest uses to refuse a pytest run whose netns was
# not proven. Mutant M4 deletes them. Fixed source, not external input.
_ISOLATION_GUARD = (
    "    code = prove_isolation()\n"
    "    if code != 0:\n"
    "        print(\"pytest isolation was not proven\", file=sys.stderr)\n"
    "        return code\n"
)


def _shim(path: Path) -> None:
    # Drops every bwrap flag and execs strace on the host network.
    path.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    *strace) break ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        "exec \"$@\"\n",
        encoding="utf-8")
    path.chmod(0o700)


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_nonexistent_pytest_path_exits_nonzero():
    out = _run(["--pytest", "tests/no_such_1225.py"], timeout=90)
    assert out.returncode != 0, out.stderr
    assert out.returncode == 2, out.stderr
    assert "isolation-unproven:" in out.stderr
    assert "pytest exited" in out.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_import_error_only_file_is_not_a_vacuous_pass(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_only_import.py").write_text(
        "import does_not_exist_1225\n", encoding="utf-8")
    sys.path.insert(0, str(PROBE.parent))
    import egress_probe
    code = egress_probe.run_pytest(tmp_path, ["tests/test_only_import.py"], set())
    assert code == 2, code


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_pin_pytest_failure_without_a_connect_is_judged_by_the_trace(tmp_path):
    """pin: pytest exit 1 with a clean trace stays a pass."""
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_fails.py").write_text(
        "def test_x():\n    assert False\n", encoding="utf-8")
    sys.path.insert(0, str(PROBE.parent))
    import egress_probe
    code = egress_probe.run_pytest(tmp_path, ["tests/test_fails.py"], set())
    assert code == 0, code


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_shim_through_pytest_exits_2(tmp_path, monkeypatch):
    shim = tmp_path / "bwrap"
    _shim(shim)
    monkeypatch.setenv("EGRESS_PROBE_BWRAP", str(shim))
    tree = tmp_path / "tree"
    tests = tree / "tests"
    tests.mkdir(parents=True)
    (tests / "test_clean.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    sys.path.insert(0, str(PROBE.parent))
    import egress_probe
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        code = egress_probe.run_pytest(tree, ["tests/test_clean.py"], set())
    err = buf.getvalue()
    assert code == 2, err
    assert "pytest isolation was not proven" in err
    assert "ENETUNREACH" in err


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_m4_removing_prove_isolation_is_killed(tmp_path):
    source = PROBE.read_text(encoding="utf-8")
    assert _ISOLATION_GUARD in source
    mutated = source.replace(_ISOLATION_GUARD, "    # M4 removed prove_isolation\n", 1)
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "egress_probe.py").write_text(mutated, encoding="utf-8")
    (repo / "tests" / "test_1219_egress_probe.py").write_text(
        (ROOT / "tests" / "test_1219_egress_probe.py").read_text(encoding="utf-8"),
        encoding="utf-8")
    (repo / "EGRESS.md").write_text("# none\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line",
         "tests/test_1219_egress_probe.py::test_shim_through_pytest_exits_2"],
        cwd=repo, capture_output=True, text=True, timeout=90)
    assert out.returncode != 0, out.stdout + out.stderr


def _mini_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    src = repo / "src" / "swarph_cli"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    # Fixed mutant, not external input. test_zz_leak.py is outside the
    # two-file list make used at 62fb0ee.
    (src / "stall_alert.py").write_text(
        "import os\n"
        "def leak():\n"
        "    os.system('curl --max-time 1 -s -o /dev/null "
        "http://203.0.113.50/')\n",
        encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "egress_probe.py").write_text(PROBE.read_text(encoding="utf-8"), encoding="utf-8")
    (tests / "test_zz_leak.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n"
        "from swarph_cli.stall_alert import leak\n"
        "def test_leak():\n"
        "    leak()\n",
        encoding="utf-8")
    (repo / "Makefile").write_text((ROOT / "Makefile").read_text(encoding="utf-8"), encoding="utf-8")
    (repo / "EGRESS.md").write_text("# none\n", encoding="utf-8")
    return repo


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_plant_outside_the_old_list_fails_make(tmp_path):
    repo = _mini_repo(tmp_path)
    out = subprocess.run(
        ["make", "egress-probe"], cwd=repo, capture_output=True, text=True, timeout=90)
    assert out.returncode != 0, out.stdout + out.stderr
    assert "egress-probe-status 1" in out.stdout, out.stdout + out.stderr
    assert "unlisted-connect" in out.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="bwrap --unshare-net is Linux")
def test_make_prints_unproven_status(tmp_path):
    repo = _mini_repo(tmp_path)
    env = os.environ.copy()
    env["EGRESS_PROBE_BWRAP"] = "/no/such/bwrap"
    out = subprocess.run(
        ["make", "egress-probe"], cwd=repo, capture_output=True, text=True,
        timeout=60, env=env)
    assert "egress-probe-status 2" in out.stdout, out.stdout + out.stderr
    assert "isolation-unproven:" in out.stderr
