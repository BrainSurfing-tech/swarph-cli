"""card #1065 — release.sh stops for an approval at head, then restarts importers one at a time."""
import importlib.util
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release.sh"
CHECK = ROOT / "scripts" / "release_check.py"

_spec = importlib.util.spec_from_file_location("release_check", CHECK)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
approval_at_head = _mod.approval_at_head
bump_text = _mod.bump_text
discover_units = _mod.discover_units
script_imports_swarph_cli = _mod.script_imports_swarph_cli
start_epoch_from_stat = _mod.start_epoch_from_stat
verify_process = _mod.verify_process
verify_until_loaded = _mod.verify_until_loaded
versions_in_image = _mod.versions_in_image


def _gnu_bash() -> bool:
    """Windows runners ship a `bash` that is the WSL stub, not a shell."""
    try:
        proc = subprocess.run(["bash", "--version"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    text = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    return proc.returncode == 0 and "GNU bash" in text


needs_bash = pytest.mark.skipif(not _gnu_bash(), reason="release.sh needs GNU bash")


def _run(args, env, cwd=None):
    return subprocess.run(
        args, cwd=cwd or ROOT, env=env, text=True, capture_output=True,
    )


def _git_with_valid_merge_tree(body):
    """Existing command-boundary fakes serve a valid tree for the new tag checks."""
    return body.replace("#!/bin/bash\n", '''#!/bin/bash
case "$1" in
  show)
    case "$2" in
      *^1:pyproject.toml) echo 'version = "0.0.1"' ;;
      *plugin.json) echo '{"version": "9.9.9"}' ;;
      *) exit 99 ;;
    esac
    exit 0 ;;
  ls-tree) printf 'plugins/swarph/.claude-plugin/plugin.json\\0plugins/swarph/.cursor-plugin/plugin.json\\0'; exit 0 ;;
  -C) if [ "$3" = grep ]; then exit 1; fi ;;
esac
''', 1)


@needs_bash
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
        "bump version to 9.9.9 in pyproject.toml, src/swarph_cli/__init__.py, plugins/swarph/.*-plugin/plugin.json",
        "commit and push branch release/9.9.9",
        "open the release PR and stop",
        "refuse to merge",
        "merge as orchestrators-hue",
        "tag v9.9.9",
        "wait for PyPI to serve 9.9.9, retrying",
        "pip install --no-cache-dir --user --break-system-packages swarph-cli==9.9.9, retrying until it succeeds",
        "imports swarph_cli",
        "one at a time",
        "process image",
    ):
        assert phrase in out, phrase
    assert not called.exists()


@needs_bash
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


@needs_bash
def test_resume_merges_only_after_approval_at_head(tmp_path):
    log = tmp_path / "gh.log"
    sudo_log = tmp_path / "sudo.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    head = "a" * 40
    merge = "b" * 40
    (bin_dir / "gh").write_text(f"""#!/bin/bash
printf '%s\\n' "$*" >> {log}
case "$*" in
  *"auth token"*) echo test-token ;;
  *"pr list"*) echo 7 ;;
  *"mergeCommit"*) echo {merge} ;;
  *"pr view"*) echo {head} ;;
  *"/reviews"*)
    printf '%s' '[{{"state":"APPROVED","commit_id":"{head}","user":{{"login":"reviewers-pixel"}},"submitted_at":"t","id":1}}]'
    ;;
  *"pr merge"*) echo MERGE >> {log}; exit 0 ;;
  *) echo "unexpected gh: $*" >> {log}; exit 99 ;;
esac
""")
    (bin_dir / "git").write_text(_git_with_valid_merge_tree(f"""#!/bin/bash
echo "git $*" >> {log}
case "$1" in
  rev-parse) exit 1 ;;
  *) exit 0 ;;
esac
"""))
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
    env["RELEASE_INSTALL_EPOCH"] = "1"
    proc = _run(["bash", str(RELEASE), "--resume", "--expected-head", head, "9.9.9"], env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    text = log.read_text()
    assert "MERGE" in text
    assert f"--match-head-commit {head}" in text
    assert "pip install --no-cache-dir --user --break-system-packages swarph-cli==9.9.9" in text
    assert "pip show" not in text
    sudo_lines = [ln for ln in sudo_log.read_text().splitlines() if ln]
    restarts = [ln for ln in sudo_lines if ln.startswith("-n systemctl restart ")]
    assert restarts == [
        "-n systemctl restart swarph-monitor@cursor-lin.service",
        "-n systemctl restart swarph-monitor@drop-on-meta-edge.service",
    ]
    assert f"fetch origin {merge}" in text
    assert f"tag -a v9.9.9 {merge}" in text
    assert f"tag -a v9.9.9 {head}" not in text
    assert f"tag v9.9.9 {head}" not in text


@needs_bash
def test_old_argument_parser_refuses_new_flag_before_commands(tmp_path):
    called = tmp_path / "called"
    old_parser = '''set -eu
VERSION=""
usage() { exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|--resume|--restart-only) ;;
    -h|--help) usage ;;
    *) if [ -n "$VERSION" ]; then usage; fi; VERSION="$1" ;;
  esac
  shift
done
gh
git
'''
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("gh", "git"):
        command = bin_dir / name
        command.write_text(f"#!/bin/sh\ntouch '{called}'\n")
        command.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = subprocess.run(["bash", "-s", "--", "--resume", "--expected-head", "a" * 40, "9.9.9"],
                          input=old_parser, env=env, text=True, capture_output=True)
    assert proc.returncode == 2
    assert not called.exists()


@needs_bash
@pytest.mark.parametrize("scenario", ["moved", "expected_mismatch", "old_approval", "normal", "invalid_expected"])
def test_resume_pins_approved_head(tmp_path, scenario):
    head = "a" * 40
    log = tmp_path / "gh.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'''#!/bin/bash
printf '%s\\n' "$*" >> "{log}"
case "$*" in
  *"pr list"*) echo 7 ;;
  *"pr view"*) echo {head} ;;
  *"/reviews"*) echo '[{{"state":"APPROVED","commit_id":"{("b" * 40) if scenario == "old_approval" else head}","user":{{"login":"reviewers-pixel"}},"id":1}}]' ;;
  *"auth token"*) echo fake-token ;;
  *"pr merge"*)
    case "$*" in *"--match-head-commit {head}"*) ;; *) exit 90 ;; esac
    {"exit 91" if scenario == "moved" else "exit 92"} ;;
  *) exit 99 ;;
esac
''')
    gh.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    if scenario == "expected_mismatch":
        expected = "b" * 40
    elif scenario == "invalid_expected":
        expected = "invalid"
    else:
        expected = head
    proc = _run(["bash", str(RELEASE), "--resume", "--expected-head", expected, "9.9.9"], env)
    assert proc.returncode == {"moved": 91, "normal": 92, "expected_mismatch": 2,
                               "old_approval": 2, "invalid_expected": 2}[scenario]
    calls = log.read_text() if log.exists() else ""
    assert ("pr merge" in calls) == (scenario in {"moved", "normal"})


@needs_bash
@pytest.mark.parametrize("with_expected", [False, True], ids=["manual", "expected"])
def test_merge_pins_original_read_not_later_head(tmp_path, with_expected):
    first, later = "a" * 40, "b" * 40
    log, seen = tmp_path / "gh.log", tmp_path / "seen"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'''#!/bin/bash
printf '%s\\n' "$*" >> "{log}"
case "$*" in
  *"pr list"*) echo 7 ;;
  *"pr view"*) if [ -e "{seen}" ]; then echo {later}; else touch "{seen}"; echo {first}; fi ;;
  *"/reviews"*) echo '[{{"state":"APPROVED","commit_id":"{first}","user":{{"login":"reviewers-pixel"}},"id":1}}]' ;;
  *"auth token"*) echo fake-token ;;
  *"pr merge"*)
    if [ "$*" = "pr merge 7 --repo BrainSurfing-tech/swarph-cli --merge --match-head-commit {first}" ]; then exit 91; fi
    exit 92 ;;
  *) exit 99 ;;
esac
''')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    args = ["bash", str(RELEASE), "--resume"]
    if with_expected:
        args += ["--expected-head", first]
    proc = _run(args + ["9.9.9"], env)
    calls = log.read_text().splitlines()
    merges = [line for line in calls if line.startswith("pr merge ")]
    assert merges == [f"pr merge 7 --repo BrainSurfing-tech/swarph-cli --merge --match-head-commit {first}"]
    assert proc.returncode == 91


@needs_bash
def test_expected_head_cannot_replace_observed_head(tmp_path):
    observed, expected = "a" * 40, "b" * 40
    log = tmp_path / "gh.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f'''#!/bin/bash
printf '%s\\n' "$*" >> "{log}"
case "$*" in
  *"pr list"*) echo 7 ;;
  *"pr view"*) echo {observed} ;;
  *"/reviews"*) echo '[{{"state":"APPROVED","commit_id":"{expected}","user":{{"login":"reviewers-pixel"}},"id":1}}]' ;;
  *"auth token"*) echo fake-token ;;
  *"pr merge"*) exit 91 ;;
  *) exit 99 ;;
esac
''')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = _run(["bash", str(RELEASE), "--resume", "--expected-head", expected, "9.9.9"], env)
    calls = log.read_text().splitlines()
    assert not any(line.startswith("pr merge ") for line in calls)
    assert any(line.startswith("pr view ") for line in calls)
    assert proc.returncode == 2
    assert "differs from --expected-head" in proc.stderr


@needs_bash
@pytest.mark.parametrize("options", [[], ["--dry-run"], ["--restart-only"],
                                     ["--resume", "--dry-run"], ["--resume", "--restart-only"]])
def test_expected_head_requires_actual_resume(tmp_path, options):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "called"
    for name in ("gh", "git", "sudo", "pip", "curl", "systemctl"):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\nprintf '%s\\n' {name} >> '{called}'\nexit 99\n")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = _run(["bash", str(RELEASE), *options, "--expected-head", "a" * 40, "9.9.9"], env)
    assert not called.exists()
    assert proc.returncode == 2
    assert "usage:" in proc.stderr


@needs_bash
@pytest.mark.parametrize("expected", ["", "invalid", "A" * 40, "a" * 39, "a" * 41],
                         ids=["empty", "invalid", "uppercase", "short", "long"])
def test_invalid_expected_head_refuses_before_gh(tmp_path, expected):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "called"
    gh = bin_dir / "gh"
    gh.write_text(f'''#!/bin/bash
touch "{called}"
case "$*" in
  *"pr list"*) echo 7 ;;
  *"pr view"*) echo {"a" * 40} ;;
  *"/reviews"*) echo '[{{"state":"APPROVED","commit_id":"{"a" * 40}","user":{{"login":"reviewers-pixel"}},"id":1}}]' ;;
  *"auth token"*) echo fake-token ;;
  *"pr merge"*) exit 91 ;;
  *) exit 99 ;;
esac
''')
    gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    proc = _run(["bash", str(RELEASE), "--resume", "--expected-head", expected, "9.9.9"], env)
    assert not called.exists()
    assert proc.returncode == 2


def _tag_resume(tmp_path, git_body: str, pip_text: str | None = None, extra_env: dict | None = None):
    """Resume past an approval, with git answering the tag questions."""
    log = tmp_path / "gh.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    head = "a" * 40
    merge = "b" * 40
    (bin_dir / "gh").write_text(f"""#!/bin/bash
printf '%s\\n' "$*" >> {log}
case "$*" in
  *"auth token"*) echo test-token ;;
  *"pr list"*) echo 7 ;;
  *"mergeCommit"*) echo {merge} ;;
  *"pr view"*) echo {head} ;;
  *"/reviews"*)
    printf '%s' '[{{"state":"APPROVED","commit_id":"{head}","user":{{"login":"reviewers-pixel"}},"submitted_at":"t","id":1}}]'
    ;;
  *"pr merge"*) echo MERGED >> {log}; exit 0 ;;
  *) echo "unexpected gh: $*" >> {log}; exit 99 ;;
esac
""")
    (bin_dir / "git").write_text(_git_with_valid_merge_tree(git_body.format(log=log, merge=merge)))
    (bin_dir / "curl").write_text(
        "#!/bin/sh\npython3 -c 'import json,sys; json.dump({\"releases\":{\"9.9.9\":[]}}, sys.stdout)'\n"
    )
    (bin_dir / "sudo").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "systemctl").write_text("#!/bin/sh\necho 1\n")
    (bin_dir / "pip").write_text(pip_text or "#!/bin/sh\nexit 0\n")
    for name in ("gh", "git", "curl", "sudo", "systemctl", "pip"):
        (bin_dir / name).chmod(0o755)
    units = tmp_path / "units"
    units.write_text("swarph-monitor@cursor-lin.service\n")
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RELEASE_UNITS_FILE"] = str(units)
    env["RELEASE_VERIFY_CMD"] = "true"
    env["RELEASE_PYPI_SLEEP"] = "0"
    env["RELEASE_INSTALL_EPOCH"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = _run(["bash", str(RELEASE), "--resume", "9.9.9"], env)
    return proc, log.read_text(), head, merge


@needs_bash
def test_tag_targets_the_merge_commit_not_the_pr_head(tmp_path):
    git_body = """#!/bin/bash
echo "git $*" >> {log}
case "$1" in
  rev-parse) exit 1 ;;
  *) exit 0 ;;
esac
"""
    proc, text, head, merge = _tag_resume(tmp_path, git_body)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert f"fetch origin {merge}" in text
    assert f"tag -a v9.9.9 {merge}" in text
    assert f"tag -a v9.9.9 {head}" not in text
    assert text.count("tag -a v9.9.9") == 1


@needs_bash
def test_resume_continues_when_the_tag_already_points_at_the_merge_commit(tmp_path):
    git_body = """#!/bin/bash
echo "git $*" >> {log}
case "$1" in
  rev-parse) echo {merge}; exit 0 ;;
  *) exit 0 ;;
esac
"""
    proc, text, _head, merge = _tag_resume(tmp_path, git_body)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert f"already points at merge commit {merge}" in proc.stdout
    assert "tag -a" not in text


@needs_bash
def test_resume_stops_when_the_tag_points_at_a_different_commit(tmp_path):
    other = "c" * 40
    git_body = f"""#!/bin/bash
echo "git $*" >> {{log}}
case "$1" in
  rev-parse) echo {other}; exit 0 ;;
  *) exit 0 ;;
esac
"""
    proc, text, _head, merge = _tag_resume(tmp_path, git_body)
    assert proc.returncode != 0
    assert f"points at {other}, not merge commit {merge}" in proc.stderr
    assert "tag -a" not in text


@needs_bash
@pytest.mark.parametrize("stale_file", [None, "docs/stray version.txt", "README.md", "CHANGELOG.md",
                                       "docs/lookalike.txt", "git-grep-error", "missing-old-version", "crlf-version"],
                         ids=["all-manifests", "stray-refused", "readme-refused", "changelog-allowed",
                              "literal-match", "grep-error-refused", "unreadable-old-version", "crlf-version"])
def test_open_pr_bumps_all_plugin_manifests_and_stops(tmp_path, stale_file):
    src = tmp_path / "origin"
    src.mkdir()
    (src / "pyproject.toml").write_text('version = "0.0.1"\n')
    init = src / "src" / "swarph_cli"
    init.mkdir(parents=True)
    (init / "__init__.py").write_text('__version__ = "0.0.1"\n')
    plugin = src / "plugins" / "swarph" / ".claude-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"version": "0.0.1"}\n')
    manifests = ["plugins/swarph/.claude-plugin/plugin.json",
                 "plugins/swarph/.cursor-plugin/plugin.json",
                 "plugins/swarph/.future-plugin/plugin.json"]
    for manifest in manifests[1:]:
        path = src / manifest
        path.parent.mkdir(parents=True)
        path.write_text('{"version": "0.0.1"}\n')
    panel = src / "plugins/swarph-panel/.claude-plugin/plugin.json"
    panel.parent.mkdir(parents=True)
    panel.write_text('{"version": "0.1.0"}\n')
    if stale_file and stale_file not in {"git-grep-error", "missing-old-version", "crlf-version"}:
        path = src / stale_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("0x0x1\n" if stale_file == "docs/lookalike.txt" else "Old swarph version: 0.0.1\n")
    if stale_file == "missing-old-version":
        (src / "pyproject.toml").write_text('name = "fixture"\n')
    if stale_file == "crlf-version":
        (src / "pyproject.toml").write_bytes(b'version = "0.0.1"\r\n')
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
    if stale_file == "git-grep-error":
        real_git = shutil.which("git")
        assert real_git
        (bin_dir / "git").write_text(f'''#!/bin/bash
if [ "$1" = "-C" ] && [ "${{3:-}}" = "grep" ]; then
  echo 'fixture git grep error' >&2
  exit 2
fi
exec {shlex.quote(Path(real_git).as_posix())} "$@"
''')
        (bin_dir / "git").chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RELEASE_REMOTE"] = str(bare)
    proc = _run(["bash", str(RELEASE), "9.9.9"], env)
    if stale_file in {"docs/stray version.txt", "README.md", "git-grep-error", "missing-old-version"}:
        assert proc.returncode != 0
        if stale_file == "git-grep-error":
            assert proc.returncode == 2
            assert "could not check for old version" in proc.stderr
        elif stale_file == "missing-old-version":
            assert "cannot read old version" in proc.stderr
        else:
            assert "old version 0.0.1" in proc.stderr
            assert "outside CHANGELOG" in proc.stderr
            assert stale_file in proc.stdout + proc.stderr
        assert not log.exists(), "No gh command, especially pr create, is allowed"
        assert subprocess.run(
            ["git", "--git-dir", str(bare), "show-ref", "--verify", "refs/heads/release/9.9.9"],
            text=True, capture_output=True,
        ).returncode != 0, "Refusal must precede even the branch push"
        return
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
    for manifest in manifests:
        plugin_show = subprocess.run(
            ["git", "--git-dir", str(bare), "show", f"release/9.9.9:{manifest}"],
            text=True, capture_output=True, check=True,
        )
        assert '"version": "9.9.9"' in plugin_show.stdout, manifest
    panel_show = subprocess.run(
        ["git", "--git-dir", str(bare), "show", "release/9.9.9:plugins/swarph-panel/.claude-plugin/plugin.json"],
        text=True, capture_output=True, check=True,
    )
    assert '"version": "0.1.0"' in panel_show.stdout


@needs_bash
@pytest.mark.parametrize("scenario", ["clean", "second-merge-stale", "wrong-manifest", "old-readme",
                                     "changelog", "lookalike", "grep-error", "invalid-json", "no-manifests"])
def test_resume_checks_the_real_merge_tree_before_tagging(tmp_path, scenario):
    repo = tmp_path / "checkout"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=fixture",
                               "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                               *args], check=True, text=True, capture_output=True).stdout.strip()

    def write(path, text):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    files = {"pyproject.toml": 'version = "0.0.1"\n',
             "src/swarph_cli/__init__.py": '__version__ = "0.0.1"\n'}
    if scenario != "no-manifests":
        files.update({"plugins/swarph/.claude-plugin/plugin.json": '{"version": "0.0.1"}\n',
                      "plugins/swarph/.cursor-plugin/plugin.json": '{"version": "0.0.1"}\n'})
    git("init", "-b", "main")
    for path, text in files.items():
        write(path, text)
    write("plugins/swarph-panel/.claude-plugin/plugin.json", '{"version": "0.1.0"}\n')
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "release/9.9.9")
    for path, text in files.items():
        write(path, text.replace("0.0.1", "9.9.9"))
    git("add", ".")
    git("commit", "-qm", "release")
    head = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("checkout", "-qb", "concurrent")
    if scenario == "second-merge-stale":
        write("plugins/swarph/.new-plugin/plugin.json", '{"version": "0.0.1"}\n')
    elif scenario in {"wrong-manifest", "invalid-json"}:
        write("plugins/swarph/.new-plugin/plugin.json",
              '{"version": "8.8.8"}\n' if scenario == "wrong-manifest" else '{broken json\n')
    elif scenario in {"old-readme", "changelog", "lookalike"}:
        write("CHANGELOG.md" if scenario == "changelog" else "README.md",
              "0x0x1\n" if scenario == "lookalike" else "0.0.1\n")
    git("add", ".")
    git("commit", "--allow-empty", "-qm", "concurrent change")
    git("checkout", "-q", "main")
    git("merge", "--no-ff", "concurrent", "-m", "other PR merges first")
    git("merge", "--no-ff", "release/9.9.9", "-m", "release merge")
    merge = git("rev-parse", "HEAD")
    bare = tmp_path / "remote.git"
    git("clone", "--bare", str(repo), str(bare))
    git("remote", "add", "origin", str(bare))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(f'''#!/bin/bash
case "$*" in
  *"auth token"*) echo fixture-token ;;
  *"pr list"*) echo 7 ;;
  *"mergeCommit"*) echo {merge} ;;
  *"pr view"*) echo {head} ;;
  *"/reviews"*) echo '[{{"state":"APPROVED","commit_id":"{head}","user":{{"login":"reviewers-pixel"}},"id":1}}]' ;;
  *"pr merge"*) exit 0 ;;
  *) exit 99 ;;
esac
''')
    (bindir / "curl").write_text('#!/bin/sh\necho \'{"releases":{"9.9.9":[]}}\'\n')
    for name in ("pip", "sudo", "systemctl"):
        (bindir / name).write_text('#!/bin/sh\nexit 0\n')
    if scenario == "grep-error":
        real_git = shutil.which("git")
        (bindir / "git").write_text(f'#!/bin/bash\nif [ "${{3:-}}" = grep ]; then exit 2; fi\nexec {shlex.quote(real_git)} "$@"\n')
    for tool in bindir.iterdir():
        tool.chmod(0o755)
    units = tmp_path / "units"
    units.write_text("fixture.service\n")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", RELEASE_INSTALL_EPOCH="1",
               RELEASE_UNITS_FILE=str(units), RELEASE_VERIFY_CMD="true")
    # The working tree is deliberately repaired; only the stored merge tree is authoritative.
    if scenario in {"second-merge-stale", "wrong-manifest", "invalid-json"}:
        write("plugins/swarph/.new-plugin/plugin.json", '{"version": "9.9.9"}\n')
    proc = _run(["bash", str(RELEASE), "--resume", "9.9.9"], env, cwd=repo)
    allowed = scenario in {"clean", "changelog", "lookalike"}
    assert (proc.returncode == 0) == allowed, proc.stderr + proc.stdout
    for location in (repo / ".git", bare):
        tag = subprocess.run(["git", "--git-dir", str(location), "rev-parse", "--verify", "refs/tags/v9.9.9^{commit}"],
                             text=True, capture_output=True)
        assert (tag.returncode == 0) == allowed
        if allowed:
            assert tag.stdout.strip() == merge
    if scenario == "grep-error":
        assert proc.returncode == 2 and "could not check for old version" in proc.stderr
    elif scenario in {"second-merge-stale", "old-readme"}:
        assert "old version 0.0.1 remains outside CHANGELOG" in proc.stderr
    elif scenario == "wrong-manifest":
        assert "plugins/swarph/.new-plugin/plugin.json version does not equal 9.9.9" in proc.stderr
    elif scenario == "invalid-json":
        assert "JSONDecodeError" in proc.stderr
    elif scenario == "no-manifests":
        assert "no swarph plugin manifests" in proc.stderr


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


_GIT_NO_TAG = """#!/bin/bash
echo "git $*" >> {log}
case "$1" in
  rev-parse) exit 1 ;;
  *) exit 0 ;;
esac
"""


@needs_bash
def test_install_retries_until_pip_succeeds(tmp_path):
    log = tmp_path / "pip.log"
    count = tmp_path / "count"
    count.write_text("")
    pip_text = f"""#!/bin/bash
echo "pip $*" >> {log}
lines=$(wc -l < {count})
echo x >> {count}
if [ "$lines" -lt 2 ]; then exit 1; fi
exit 0
"""
    proc, _text, _head, _merge = _tag_resume(
        tmp_path, _GIT_NO_TAG, pip_text=pip_text, extra_env={"RELEASE_PIP_SLEEP": "0"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    lines = [ln for ln in log.read_text().splitlines() if ln]
    assert len(lines) == 3
    assert all("install --no-cache-dir --user --break-system-packages swarph-cli==9.9.9" in ln for ln in lines)


@needs_bash
def test_install_gives_up_after_thirty_tries(tmp_path):
    log = tmp_path / "pip.log"
    pip_text = f"""#!/bin/bash
echo "pip $*" >> {log}
exit 1
"""
    proc, _text, _head, _merge = _tag_resume(
        tmp_path, _GIT_NO_TAG, pip_text=pip_text, extra_env={"RELEASE_PIP_SLEEP": "0"},
    )
    assert proc.returncode != 0
    assert "never succeeded" in proc.stderr
    assert len([ln for ln in log.read_text().splitlines() if ln]) == 30


@needs_bash
def test_pip_args_come_from_RELEASE_PIP_ARGS(tmp_path):
    log = tmp_path / "pip.log"
    pip_text = f"""#!/bin/bash
echo "pip $*" >> {log}
exit 0
"""
    proc, _text, _head, _merge = _tag_resume(
        tmp_path, _GIT_NO_TAG, pip_text=pip_text,
        extra_env={"RELEASE_PIP_ARGS": "--no-cache-dir", "RELEASE_PIP_SLEEP": "0"},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    text = log.read_text()
    assert "pip install --no-cache-dir swarph-cli==9.9.9" in text
    assert "--break-system-packages" not in text
    assert "--user" not in text


def test_verify_retries_while_the_image_has_no_version():
    images = iter([b"", b"noise", b"swarph_cli-9.9.9.dist-info"])
    clock = {"t": 0.0}
    slept = []

    def sleeper(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    verify_until_loaded(
        100, 1, "9.9.9", "42", lambda: next(images),
        wait_s=15, sleep_s=2, sleep=sleeper, monotonic=lambda: clock["t"],
    )
    assert slept == [2, 2]


def test_verify_fails_immediately_on_a_wrong_version():
    slept = []
    with pytest.raises(SystemExit) as caught:
        verify_until_loaded(
            100, 1, "9.9.9", "42", lambda: b"swarph_cli-0.1.0.dist-info",
            wait_s=15, sleep_s=2, sleep=lambda s: slept.append(s), monotonic=lambda: 0.0,
        )
    assert "0.1.0" in str(caught.value.code)
    assert slept == []


def test_verify_fails_with_none_only_after_the_settle_window():
    clock = {"t": 0.0}

    def sleeper(seconds):
        clock["t"] += seconds

    with pytest.raises(SystemExit) as caught:
        verify_until_loaded(
            100, 1, "9.9.9", "42", lambda: b"",
            wait_s=15, sleep_s=2, sleep=sleeper, monotonic=lambda: clock["t"],
        )
    assert "none" in str(caught.value.code)
    assert clock["t"] >= 15


def test_restart_loop_is_one_unit_per_systemctl_call():
    text = RELEASE.read_text()
    assert 'sudo -n systemctl restart "$unit"' in text
    assert "systemctl restart " + '"$@"' not in text
    assert "systemctl restart $units" not in text
