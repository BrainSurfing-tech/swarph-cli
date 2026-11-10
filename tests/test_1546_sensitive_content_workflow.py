"""Card #1546 — scope swarph-cli's secrets scan on pull_request events to the
PR's own commits (--log-opts="refs/remotes/origin/${GITHUB_BASE_REF}..HEAD"),
fail closed with git show-ref --verify --quiet before scanning, fail closed if
gitleaks logs any '[git]' stderr line via tee, and keep the full scan on push
to main.
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

SHOW_REF_LINE = (
    'git show-ref --verify --quiet "refs/remotes/origin/${GITHUB_BASE_REF}"'
)
LOG_OPTS_LINE = '--log-opts="refs/remotes/origin/${GITHUB_BASE_REF}..HEAD"'
TEE_LINE = "2>&1 | tee /tmp/gitleaks-pr.log"
GIT_ERR_CHECK_LINE = 'if grep -Fq "[git]" /tmp/gitleaks-pr.log; then'


def _find_bash() -> str | None:
    if sys.platform == "win32":
        return None
    return shutil.which("bash")


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no usable POSIX bash on win32")


def _load_workflow() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in doc and "on" not in doc:
        doc["on"] = doc.pop(True)
    return doc


def _gitleaks_step() -> dict:
    doc = _load_workflow()
    steps = doc["jobs"]["scan"]["steps"]
    for step in steps:
        if step.get("name") == "Scan for hardcoded secrets":
            return step
    raise AssertionError("Scan for hardcoded secrets step not found")


def _init_repo_with_remote_base(repo_dir: Path, base_ref: str | None) -> None:
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


def test_sensitive_content_workflow_preserves_version_config_redact_and_pins_guards() -> None:
    doc = _load_workflow()
    steps = doc["jobs"]["scan"]["steps"]
    checkout = next(s for s in steps if s.get("name") == "Checkout")
    assert checkout["with"]["fetch-depth"] == 0

    step = _gitleaks_step()
    assert step["env"]["GITLEAKS_VERSION"] == "8.28.0"
    run_script = step["run"]
    assert "--redact" in run_script
    assert "--no-banner" in run_script
    assert "--config .gitleaks.toml" in run_script
    assert "rev-parse" not in run_script
    assert SHOW_REF_LINE in run_script
    assert LOG_OPTS_LINE in run_script
    assert TEE_LINE in run_script
    assert GIT_ERR_CHECK_LINE in run_script
    assert (
        run_script.index(SHOW_REF_LINE)
        < run_script.index(LOG_OPTS_LINE)
        < run_script.index(TEE_LINE)
        < run_script.index(GIT_ERR_CHECK_LINE)
    )


@needs_bash
@pytest.mark.parametrize(
    "event_name,base_ref,expected_args",
    [
        (
            "pull_request",
            "main",
            [
                "detect",
                "--source",
                ".",
                "--redact",
                "--no-banner",
                "--config",
                ".gitleaks.toml",
                "--log-opts=refs/remotes/origin/main..HEAD",
            ],
        ),
        (
            "push",
            "",
            [
                "detect",
                "--source",
                ".",
                "--redact",
                "--no-banner",
                "--config",
                ".gitleaks.toml",
            ],
        ),
        (
            "workflow_dispatch",
            "",
            [
                "detect",
                "--source",
                ".",
                "--redact",
                "--no-banner",
                "--config",
                ".gitleaks.toml",
            ],
        ),
    ],
)
def test_sensitive_content_workflow_shell_dispatch(
    tmp_path: Path, event_name: str, base_ref: str, expected_args: list[str]
) -> None:
    """Execute the workflow's gitleaks invocation logic with shims for curl,
    tar, and /tmp/gitleaks (rewritten to a tmp_path directory) to verify the
    exact argv passed on pull_request vs push/workflow_dispatch and that
    pull_request scan output still shows on stdout via tee."""
    assert BASH is not None
    step = _gitleaks_step()
    repo_dir = tmp_path / "repo"
    _init_repo_with_remote_base(repo_dir, base_ref or None)
    fake_tmp = tmp_path / "fake_tmp"
    fake_tmp.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_log = tmp_path / "gitleaks_args.txt"

    for tool in ("curl", "tar"):
        shim = bin_dir / tool
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)

    gitleaks_shim = fake_tmp / "gitleaks"
    gitleaks_shim.write_text(
        f'#!/bin/sh\nif [ "$1" = "--version" ]; then exit 0; fi\n'
        f'printf "%s\\n" "$@" > "{args_log}"\n'
        f'echo "INF 1 commits scanned." >&2\n'
        f'echo "INF no leaks found" >&2\n',
        encoding="utf-8",
    )
    gitleaks_shim.chmod(0o755)

    script = step["run"].replace("/tmp", str(fake_tmp))
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["GITLEAKS_VERSION"] = step["env"]["GITLEAKS_VERSION"]
    env["GITHUB_EVENT_NAME"] = event_name
    if base_ref:
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
    assert res.returncode == 0, res.stderr
    recorded = args_log.read_text(encoding="utf-8").splitlines()
    assert recorded == expected_args
    if event_name == "pull_request":
        assert "INF no leaks found" in res.stdout
        assert (fake_tmp / "gitleaks-pr.log").exists()


@needs_bash
def test_sensitive_content_workflow_fails_closed_when_base_ref_missing_even_with_tag(
    tmp_path: Path,
) -> None:
    """When refs/remotes/origin/${GITHUB_BASE_REF} does not exist on a
    pull_request event — even if a tag named refs/remotes/origin/main exists —
    git show-ref --verify --quiet must fail closed before invoking gitleaks."""
    assert BASH is not None
    step = _gitleaks_step()
    repo_dir = tmp_path / "repo"
    _init_repo_with_remote_base(repo_dir, base_ref=None)
    subprocess.run(
        ["git", "tag", "refs/remotes/origin/main", "HEAD"],
        cwd=str(repo_dir),
        check=True,
    )
    fake_tmp = tmp_path / "fake_tmp"
    fake_tmp.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_log = tmp_path / "gitleaks_args.txt"

    for tool in ("curl", "tar"):
        shim = bin_dir / tool
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)

    gitleaks_shim = fake_tmp / "gitleaks"
    gitleaks_shim.write_text(
        f'#!/bin/sh\nif [ "$1" = "--version" ]; then exit 0; fi\n'
        f'printf "%s\\n" "$@" > "{args_log}"\n',
        encoding="utf-8",
    )
    gitleaks_shim.chmod(0o755)

    script = step["run"].replace("/tmp", str(fake_tmp))
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["GITLEAKS_VERSION"] = step["env"]["GITLEAKS_VERSION"]
    env["GITHUB_EVENT_NAME"] = "pull_request"
    env["GITHUB_BASE_REF"] = "main"

    res = subprocess.run(
        [BASH, "-c", script],
        cwd=str(repo_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode != 0
    assert "refs/remotes/origin/main" in res.stderr
    assert not args_log.exists()


@needs_bash
def test_sensitive_content_workflow_fails_closed_on_git_stderr_line(
    tmp_path: Path,
) -> None:
    """When gitleaks logs a '[git]' stderr warning line and exits 0, the tee +
    grep guard must still show the output and fail the step with exit code 1."""
    assert BASH is not None
    step = _gitleaks_step()
    repo_dir = tmp_path / "repo"
    _init_repo_with_remote_base(repo_dir, base_ref="main")
    fake_tmp = tmp_path / "fake_tmp"
    fake_tmp.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    for tool in ("curl", "tar"):
        shim = bin_dir / tool
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)

    gitleaks_shim = fake_tmp / "gitleaks"
    gitleaks_shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then exit 0; fi\n'
        "echo \"3:38AM ERR [git] warning: refname 'HEAD' is ambiguous.\" >&2\n"
        'echo "3:38AM INF 0 commits scanned." >&2\n'
        "exit 0\n",
        encoding="utf-8",
    )
    gitleaks_shim.chmod(0o755)

    script = step["run"].replace("/tmp", str(fake_tmp))
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["GITLEAKS_VERSION"] = step["env"]["GITLEAKS_VERSION"]
    env["GITHUB_EVENT_NAME"] = "pull_request"
    env["GITHUB_BASE_REF"] = "main"

    res = subprocess.run(
        [BASH, "-c", script],
        cwd=str(repo_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode == 1
    assert "[git] warning: refname 'HEAD' is ambiguous." in res.stdout
    assert "git stderr detected" in res.stderr
