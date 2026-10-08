"""card #1065 — release.sh stops for an approval at head, then restarts importers one at a time."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release.sh"
CHECK = ROOT / "scripts" / "release_check.py"

from scripts.release_check import (  # noqa: E402
    approval_at_head,
    bump_text,
    discover_units,
    script_imports_swarph_cli,
    start_epoch_from_stat,
    verify_process,
    versions_in_image,
)


def _run(args, env, cwd=None):
    return subprocess.run(
        args, cwd=cwd or ROOT, env=env, text=True, capture_output=True,
    )


def test_dry_run_lists_every_step_and_touches_nothing(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "called"
    for name in ("git", "gh", "sudo", "systemctl", "pip", "curl"):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\necho {name} >> {called}\nexit 99\n")
        stub.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    proc = _run(["bash", str(RELEASE), "--dry-run", "9.9.9"], env)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for phrase in (
        "scratch-clone",
        "bump version to 9.9.9 in pyproject.toml, src/swarph_cli/__init__.py, plugins/swarph/.claude-plugin/plugin.json",
        "commit and push branch release/9.9.9",
        "open the release PR and stop",
        "refuse to merge",
        "merge as orchestrators-hue",
        "tag v9.9.9",
        "wait for PyPI to serve 9.9.9, retrying",
        "pip install --no-cache-dir swarph-cli==9.9.9",
        "imports swarph_cli",
        "one at a time",
        "process image",
    ):
        assert phrase in out, phrase
    assert not called.exists()


def test_refuses_to_merge_without_an_approval_at_head(tmp_path):
    log = tmp_path / "gh.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"""#!/bin/bash
printf '%s\\n' "$*" >> {log}
case "$*" in
  *"pr list"*) echo 7 ;;
  *"pr view"*) echo deadbeefdeadbeefdeadbeefdeadbeefdeadbeef ;;
  *"/reviews"*)
    printf '%s' '[{{"state":"APPROVED","commit_id":"oldsha","user":{{"login":"reviewers-pixel"}},"submitted_at":"t","id":1}}]'
    ;;
  *"pr merge"*) echo MERGE >> {log}; exit 0 ;;
  *) echo "unexpected: $*" >> {log}; exit 99 ;;
esac
""")
    gh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    proc = _run(["bash", str(RELEASE), "--resume", "9.9.9"], env)
    assert proc.returncode != 0
    assert "refusing to merge 9.9.9" in proc.stderr
    assert "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" in proc.stderr
    assert "MERGE" not in log.read_text()


def test_resume_merges_only_after_approval_at_head(tmp_path):
    log = tmp_path / "gh.log"
    sudo_log = tmp_path / "sudo.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    head = "a" * 40
    (bin_dir / "gh").write_text(f"""#!/bin/bash
printf '%s\\n' "$*" >> {log}
case "$*" in
  *"auth token"*) echo test-token ;;
  *"pr list"*) echo 7 ;;
  *"pr view"*) echo {head} ;;
  *"/reviews"*)
    printf '%s' '[{{"state":"APPROVED","commit_id":"{head}","user":{{"login":"reviewers-pixel"}},"submitted_at":"t","id":1}}]'
    ;;
  *"pr merge"*) echo MERGE >> {log}; exit 0 ;;
  *) echo "unexpected gh: $*" >> {log}; exit 99 ;;
esac
""")
    (bin_dir / "git").write_text(f"#!/bin/sh\necho git $* >> {log}\nexit 0\n")
    (bin_dir / "curl").write_text(
        "#!/bin/sh\npython3 -c 'import json,sys; json.dump({\"releases\":{\"9.9.9\":[]}}, sys.stdout)'\n"
    )
    (bin_dir / "sudo").write_text(f"#!/bin/sh\necho \"$*\" >> {sudo_log}\nexit 0\n")
    (bin_dir / "systemctl").write_text("#!/bin/sh\necho 1\n")
    (bin_dir / "pip").write_text(f"#!/bin/sh\necho pip $* >> {log}\nexit 0\n")
    for name in ("gh", "git", "curl", "sudo", "systemctl", "pip"):
        (bin_dir / name).chmod(0o755)
    units = tmp_path / "units"
    units.write_text("swarph-monitor@cursor-lin.service\nswarph-monitor@drop-on-meta-edge.service\n")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RELEASE_UNITS_FILE"] = str(units)
    env["RELEASE_VERIFY_CMD"] = "true"
    env["RELEASE_PYPI_SLEEP"] = "0"
    proc = _run(["bash", str(RELEASE), "--resume", "9.9.9"], env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    text = log.read_text()
    assert "MERGE" in text
    assert "pip install --no-cache-dir swarph-cli==9.9.9" in text
    assert "pip show" not in text
    sudo_lines = [ln for ln in sudo_log.read_text().splitlines() if ln]
    restarts = [ln for ln in sudo_lines if ln.startswith("-n systemctl restart ")]
    assert restarts == [
        "-n systemctl restart swarph-monitor@cursor-lin.service",
        "-n systemctl restart swarph-monitor@drop-on-meta-edge.service",
    ]


def test_open_pr_bumps_three_files_and_stops(tmp_path):
    src = tmp_path / "origin"
    src.mkdir()
    (src / "pyproject.toml").write_text('version = "0.0.1"\n')
    init = src / "src" / "swarph_cli"
    init.mkdir(parents=True)
    (init / "__init__.py").write_text('__version__ = "0.0.1"\n')
    plugin = src / "plugins" / "swarph" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"version": "0.0.1"}\n')
    subprocess.run(["git", "init", "-b", "main"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@local", "-c", "user.name=t", "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "-c", "user.email=t@local", "-c", "user.name=t", "commit", "-m", "base"], check=True, capture_output=True)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "--bare", str(src), str(bare)], check=True, capture_output=True)
    log = tmp_path / "gh.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(f"""#!/bin/bash
printf '%s\\n' "$*" >> {log}
case "$*" in
  *"pr create"*) exit 0 ;;
  *"pr merge"*) echo MERGE >> {log}; exit 99 ;;
  *) exit 0 ;;
esac
""")
    (bin_dir / "gh").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RELEASE_REMOTE"] = str(bare)
    proc = _run(["bash", str(RELEASE), "9.9.9"], env)
    assert proc.returncode == 0, proc.stderr
    assert "stopped after opening the release PR" in proc.stdout
    assert "MERGE" not in log.read_text()
    show = subprocess.run(
        ["git", "--git-dir", str(bare), "show", "release/9.9.9:pyproject.toml"],
        text=True, capture_output=True, check=True,
    )
    assert 'version = "9.9.9"' in show.stdout
    init_show = subprocess.run(
        ["git", "--git-dir", str(bare), "show", "release/9.9.9:src/swarph_cli/__init__.py"],
        text=True, capture_output=True, check=True,
    )
    assert '__version__ = "9.9.9"' in init_show.stdout
    plugin_show = subprocess.run(
        ["git", "--git-dir", str(bare), "show", "release/9.9.9:plugins/swarph/.claude-plugin/plugin.json"],
        text=True, capture_output=True, check=True,
    )
    assert '"version": "9.9.9"' in plugin_show.stdout


def test_verify_fails_when_the_process_started_before_the_install(tmp_path):
    image = tmp_path / "image"
    image.write_bytes(b"....swarph_cli-9.9.9.dist-info....")
    env = os.environ.copy()
    env["RELEASE_START_EPOCH"] = "1000"
    env["RELEASE_IMAGE"] = str(image)
    proc = _run(
        ["python3", str(CHECK), "verify", "--pid", "42", "--expected", "9.9.9", "--install-epoch", "5000"],
        env,
    )
    assert proc.returncode != 0
    assert "before the install" in proc.stderr
    assert "pip show" not in proc.stderr
    source = RELEASE.read_text() + CHECK.read_text()
    for line in source.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "pip show" not in line


def test_verify_reads_the_version_from_the_process_image(tmp_path):
    image = tmp_path / "image"
    image.write_bytes(b"noise swarph_cli-0.1.0.dist-info noise")
    env = os.environ.copy()
    env["RELEASE_START_EPOCH"] = "9000"
    env["RELEASE_IMAGE"] = str(image)
    proc = _run(
        ["python3", str(CHECK), "verify", "--pid", "42", "--expected", "9.9.9", "--install-epoch", "5000"],
        env,
    )
    assert proc.returncode != 0
    assert "loaded __version__ 0.1.0 from the process image" in proc.stderr
    image.write_bytes(b"swarph_cli-9.9.9.dist-info")
    ok = _run(
        ["python3", str(CHECK), "verify", "--pid", "42", "--expected", "9.9.9", "--install-epoch", "5000"],
        env,
    )
    assert ok.returncode == 0, ok.stderr


def test_start_epoch_and_image_parser():
    stat = "123 (python 3) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 100 0 0"
    assert start_epoch_from_stat(stat, btime=1_000, clk_tck=100) == 1001
    assert versions_in_image(b"swarph_cli-1.2.3.dist-info") == {"1.2.3"}


def test_approval_requires_the_head_commit():
    head = "a" * 40
    reviews = [{
        "state": "APPROVED", "commit_id": "older", "submitted_at": "t", "id": 1,
        "user": {"login": "reviewers-pixel"},
    }]
    assert approval_at_head(reviews, head) is False
    reviews[0]["commit_id"] = head
    assert approval_at_head(reviews, head) is True


def test_discover_keeps_units_whose_script_imports_swarph_cli(tmp_path):
    script = tmp_path / "swarph"
    script.write_text("from swarph_cli.main import main\n")
    other = tmp_path / "run.py"
    other.write_text("print('no package here')\n")

    def list_running():
        return ["swarph-monitor@cursor-lin.service", "swarph-desktop.service"]

    def main_pid(unit):
        return "1" if "monitor" in unit else "2"

    def cmdline(pid):
        if pid == "1":
            return ["/usr/bin/python3", str(script), "monitor"]
        return ["/usr/bin/python3", str(other)]

    found = discover_units(list_running, main_pid, cmdline, lambda p: Path(p).read_text())
    assert found == ["swarph-monitor@cursor-lin.service"]
    assert script_imports_swarph_cli(["python3", "-m", "swarph_cli"], lambda p: "")


def test_bump_rewrites_only_the_version_field():
    assert bump_text("pyproject", "9.9.9", 'version = "0.0.1"\n') == 'version = "9.9.9"\n'
    assert bump_text("init", "9.9.9", '__version__ = "0.0.1"\n') == '__version__ = "9.9.9"\n'
    assert bump_text("plugin", "9.9.9", '{"version": "0.0.1"}\n') == '{"version": "9.9.9"}\n'


def test_restart_loop_is_one_unit_per_systemctl_call():
    text = RELEASE.read_text()
    assert 'sudo -n systemctl restart "$unit"' in text
    assert "systemctl restart " + '"$@"' not in text
    assert "systemctl restart $units" not in text
