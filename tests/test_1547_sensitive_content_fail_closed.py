"""Card #1547 — close the 4 secrets-scan fail-open paths in
.github/workflows/sensitive-content.yml:
(1) fail-closed '[git]' stderr check on the push/workflow_dispatch full scan;
(2) defeat PR-added .gitattributes '-diff' in both pull_request and push scans
    via .git/info/attributes ('* diff') and GIT_ATTR_SOURCE=<empty-tree-oid>;
(3) fail closed on gitleaks 'partial scan' / 'failed to scan Git repository'
    non-git errors in both modes;
(4) pin 'set -euo pipefail' both statically and behaviorally by running each
    scan mode with a stub scanner that exits 1 and asserting the step exits 1.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "sensitive-content.yml"

PIPEFAIL_LINE = "set -euo pipefail"
INFO_ATTR_LINE = "printf '* diff\\n' > .git/info/attributes"
EMPTY_TREE_ATTR_SOURCE = (
    "GIT_ATTR_SOURCE=4b825dc642cb6eb9a060e54bf8d69288fbee4904"
)
PR_TEE_LINE = "2>&1 | tee /tmp/gitleaks-pr.log"
PR_GIT_ERR_LINE = 'if grep -Fq "[git]" /tmp/gitleaks-pr.log; then'
PR_PARTIAL_LINE = (
    'if grep -Eq "partial scan|failed to scan Git repository" /tmp/gitleaks-pr.log; then'
)
PUSH_TEE_LINE = "2>&1 | tee /tmp/gitleaks-push.log"
PUSH_GIT_ERR_LINE = 'if grep -Fq "[git]" /tmp/gitleaks-push.log; then'
PUSH_PARTIAL_LINE = (
    'if grep -Eq "partial scan|failed to scan Git repository" /tmp/gitleaks-push.log; then'
)


def _find_bash() -> str | None:
    if sys.platform == "win32":
        return None
    return shutil.which("bash")


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no usable POSIX bash on win32")


def _gitleaks_step() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["scan"]["steps"]:
        if step.get("name") == "Scan for hardcoded secrets":
            return step
    raise AssertionError("Scan for hardcoded secrets step not found")


def _init_repo(repo_dir: Path, base_ref: str | None = "main") -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo_dir), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_dir),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo_dir),
        check=True,
    )
    (repo_dir / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=str(repo_dir), check=True)
    if base_ref:
        subprocess.run(
            ["git", "update-ref", f"refs/remotes/origin/{base_ref}", "HEAD"],
            cwd=str(repo_dir),
            check=True,
        )


def _run_workflow_step(
    tmp_path: Path,
    event_name: str,
    gitleaks_body: str,
    base_ref: str = "main",
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    assert BASH is not None
    step = _gitleaks_step()
    repo_dir = tmp_path / f"repo_{event_name}"
    _init_repo(repo_dir, base_ref if event_name == "pull_request" else None)
    fake_tmp = tmp_path / f"fake_tmp_{event_name}"
    fake_tmp.mkdir()
    bin_dir = tmp_path / f"bin_{event_name}"
    bin_dir.mkdir()

    for tool in ("curl", "tar"):
        shim = bin_dir / tool
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)

    gitleaks_shim = fake_tmp / "gitleaks"
    gitleaks_shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then exit 0; fi\n'
        f"{gitleaks_body}\n",
        encoding="utf-8",
    )
    gitleaks_shim.chmod(0o755)

    script = step["run"].replace("/tmp", str(fake_tmp))
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["GITLEAKS_VERSION"] = step["env"]["GITLEAKS_VERSION"]
    env["GITHUB_EVENT_NAME"] = event_name
    if event_name == "pull_request":
        env["GITHUB_BASE_REF"] = base_ref
    else:
        env.pop("GITHUB_BASE_REF", None)

    res = subprocess.run(
        [BASH, "-c", script],
        cwd=str(repo_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return res, repo_dir, fake_tmp


def test_card_1547_static_pins_for_pipefail_gitattributes_and_both_scan_guards() -> None:
    step = _gitleaks_step()
    run_script = step["run"]

    assert PIPEFAIL_LINE in run_script
    assert INFO_ATTR_LINE in run_script
    assert run_script.count(EMPTY_TREE_ATTR_SOURCE) == 2

    assert PR_TEE_LINE in run_script
    assert PR_GIT_ERR_LINE in run_script
    assert PR_PARTIAL_LINE in run_script

    assert PUSH_TEE_LINE in run_script
    assert PUSH_GIT_ERR_LINE in run_script
    assert PUSH_PARTIAL_LINE in run_script

    assert (
        run_script.index(PIPEFAIL_LINE)
        < run_script.index(INFO_ATTR_LINE)
        < run_script.index(PR_TEE_LINE)
        < run_script.index(PR_GIT_ERR_LINE)
        < run_script.index(PR_PARTIAL_LINE)
        < run_script.index(PUSH_TEE_LINE)
        < run_script.index(PUSH_GIT_ERR_LINE)
        < run_script.index(PUSH_PARTIAL_LINE)
    )


@needs_bash
@pytest.mark.parametrize("event_name", ["pull_request", "push", "workflow_dispatch"])
def test_card_1547_pipefail_makes_each_scan_step_exit_1_when_scanner_exits_1(
    tmp_path: Path, event_name: str
) -> None:
    """Requirement (4): run each scan mode with a stub scanner that exits 1
    (without printing '[git]' or 'partial scan') and assert the step exits 1
    because 'set -euo pipefail' propagates the scanner exit code across tee."""
    res, _repo_dir, _fake_tmp = _run_workflow_step(
        tmp_path,
        event_name,
        gitleaks_body=(
            'echo "6:00AM INF 1 commits scanned." >&2\n'
            'echo "6:00AM WRN leaks found: 1" >&2\n'
            "exit 1"
        ),
    )
    assert res.returncode == 1
    assert "WRN leaks found: 1" in res.stdout


@needs_bash
@pytest.mark.parametrize("event_name", ["push", "workflow_dispatch"])
def test_card_1547_push_scan_fails_closed_on_git_stderr_line(
    tmp_path: Path, event_name: str
) -> None:
    """Requirement (1): when gitleaks logs a '[git]' stderr warning line and
    exits 0 during a push or workflow_dispatch full scan, tee + grep must fail
    closed with exit code 1."""
    res, _repo_dir, fake_tmp = _run_workflow_step(
        tmp_path,
        event_name,
        gitleaks_body=(
            "echo \"6:00AM ERR [git] warning: refname 'HEAD' is ambiguous.\" >&2\n"
            'echo "6:00AM INF 0 commits scanned." >&2\n'
            "exit 0"
        ),
    )
    assert res.returncode == 1
    assert "[git] warning: refname 'HEAD' is ambiguous." in res.stdout
    assert "git stderr detected during gitleaks push scan" in res.stderr
    assert (fake_tmp / "gitleaks-push.log").exists()


@needs_bash
@pytest.mark.parametrize("event_name", ["pull_request", "push", "workflow_dispatch"])
def test_card_1547_both_scans_fail_closed_on_partial_scan_error(
    tmp_path: Path, event_name: str
) -> None:
    """Requirement (3): when gitleaks logs a 'partial scan' / 'failed to scan
    Git repository' error (cmd/git.go:91-96, cmd/root.go:439-447) and exits 0,
    both pull_request and push/workflow_dispatch scans must exit 1."""
    res, _repo_dir, _fake_tmp = _run_workflow_step(
        tmp_path,
        event_name,
        gitleaks_body=(
            'echo "6:00AM ERR failed to scan Git repository" >&2\n'
            'echo "6:00AM WRN partial scan completed in 15ms" >&2\n'
            'echo "6:00AM WRN no leaks found in partial scan" >&2\n'
            "exit 0"
        ),
    )
    assert res.returncode == 1
    assert "no leaks found in partial scan" in res.stdout
    assert "partial scan detected" in res.stderr


@needs_bash
@pytest.mark.parametrize("event_name", ["pull_request", "push"])
def test_card_1547_gitattributes_neg_diff_cannot_hide_files_in_either_scan(
    tmp_path: Path, event_name: str
) -> None:
    """Requirement (2): verify that the workflow step configures both
    .git/info/attributes ('* diff') and GIT_ATTR_SOURCE=4b825dc642cb6eb9a060e54bf8d69288fbee4904
    so a PR-added .gitattributes marking files '-diff' or 'binary' cannot hide
    added lines from git log -p -U0 in either pull_request or push mode."""
    res, repo_dir, _fake_tmp = _run_workflow_step(
        tmp_path,
        event_name,
        gitleaks_body=(
            'printf "GIT_ATTR_SOURCE=%s\\n" "${GIT_ATTR_SOURCE:-}"\n'
            'echo "INF 1 commits scanned." >&2\n'
            'echo "INF no leaks found" >&2\n'
            "exit 0"
        ),
    )
    assert res.returncode == 0, res.stderr
    assert (
        "GIT_ATTR_SOURCE=4b825dc642cb6eb9a060e54bf8d69288fbee4904" in res.stdout
    )
    info_attr = repo_dir / ".git" / "info" / "attributes"
    assert info_attr.read_text(encoding="utf-8") == "* diff\n"

    # Commit a .gitattributes marking everything -diff and a secret file, and
    # verify git log -p -U0 emits the added line instead of 'Binary files differ'.
    (repo_dir / ".gitattributes").write_text("* -diff binary\n", encoding="utf-8")
    canary = "CANARY_LINE_BEHIND_NEG_DIFF"
    (repo_dir / "hidden.txt").write_text(f"{canary}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitattributes", "hidden.txt"],
        cwd=str(repo_dir),
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "add hidden file"],
        cwd=str(repo_dir),
        check=True,
    )

    env = os.environ.copy()
    env["GIT_ATTR_SOURCE"] = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    if event_name == "pull_request":
        git_cmd = [
            "git",
            "-C",
            ".",
            "log",
            "-p",
            "-U0",
            "refs/remotes/origin/main..HEAD",
        ]
    else:
        git_cmd = [
            "git",
            "-C",
            ".",
            "log",
            "-p",
            "-U0",
            "--full-history",
            "--all",
            "--diff-filter=tuxdb",
        ]
    patch_out = subprocess.check_output(
        git_cmd, cwd=str(repo_dir), env=env, text=True
    )
    assert f"+{canary}" in patch_out
    assert "Binary files" not in patch_out
