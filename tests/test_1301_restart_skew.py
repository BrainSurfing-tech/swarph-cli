"""card #1301: a same-second restart is not a failed restart.

/proc btime is a whole second, so the derived start can read up to 1s
before the install cutoff. A process that did not restart still fails.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "release_check.py"
RELEASE = ROOT / "release.sh"


def _run(args, env):
    return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True)


def test_1301_same_second_restart_passes_and_an_old_process_fails(tmp_path):
    image = tmp_path / "image"
    image.write_bytes(b"swarph_cli-9.9.9.dist-info")
    env = os.environ.copy()
    env["RELEASE_IMAGE"] = str(image)
    env["RELEASE_START_EPOCH"] = "648.800"
    ok = _run(
        ["python3", str(CHECK), "verify", "--pid", "42", "--expected", "9.9.9",
         "--install-epoch", "649.000"],
        env,
    )
    assert ok.returncode == 0, ok.stderr
    assert "before the install" not in ok.stderr
    env["RELEASE_START_EPOCH"] = "100.000"
    bad = _run(
        ["python3", str(CHECK), "verify", "--pid", "42", "--expected", "9.9.9",
         "--install-epoch", "649.000"],
        env,
    )
    assert bad.returncode != 0
    assert "before the install" in bad.stderr
    env["RELEASE_START_EPOCH"] = "648.800"
    image.write_bytes(b"swarph_cli-0.1.0.dist-info")
    wrong = _run(
        ["python3", str(CHECK), "verify", "--pid", "42", "--expected", "9.9.9",
         "--install-epoch", "649.000"],
        env,
    )
    assert wrong.returncode != 0
    assert "0.1.0" in wrong.stderr


def test_1301_record_mtime_is_the_install_moment(tmp_path):
    record = tmp_path / "RECORD"
    record.write_bytes(b"x")
    os.utime(record, (649.0, 649.0))
    proc = _run(
        ["python3", str(CHECK), "record-mtime", "--version", "9.9.9",
         "--record", str(record)],
        os.environ.copy(),
    )
    assert proc.returncode == 0, proc.stderr
    assert abs(float(proc.stdout.strip()) - 649.0) < 0.01
    text = RELEASE.read_text()
    assert 'record-mtime --version "$VERSION"' in text
    assert "install_epoch=$(date +%s)" not in text
