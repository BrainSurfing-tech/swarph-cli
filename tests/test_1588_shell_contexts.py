"""Card #1588 follow-ups: compare routing with real Bash and a fixture gh."""
import os
import shutil
import subprocess

import pytest

from swarph_cli import gh_identity


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("FOO=$(bash -c 'gh pr list') true", id="assignment-bash"),
    pytest.param("FOO=`bash -c 'gh pr list'` true", id="assignment-backticks"),
    pytest.param('FOO=$(eval "gh pr list") true', id="assignment-eval"),
    pytest.param("export FOO=$(bash -c 'gh pr list')", id="export-bash"),
    pytest.param("FOO=$(echo 'gh pr list' | bash) true", id="assignment-piped-bash"),
    pytest.param("echo 'gh pr list' | bash", id="constant-pipe-bash"),
    pytest.param('echo "gh pr list" | sh -s', id="constant-pipe-sh"),
    pytest.param("bash <<'EOF'\necho x \\\ny\nEOF\nbash -c 'gh pr list'",
                 id="shell-heredoc-continuation"),
    pytest.param("bash <<'EOF'\necho x \\\ny\nbash -c 'gh pr list'\nEOF",
                 id="inside-shell-heredoc-continuation"),
    pytest.param('X="$(echo "a )")" gh pr list', id="nested-quoted-parenthesis"),
    pytest.param("gh pr list", id="plain-control"),
    pytest.param("bash -c 'gh pr list'", id="bash-control"),
    pytest.param('eval "gh pr list"', id="eval-control"),
])
def test_bash_gh_execution_always_requires_routing(command, tmp_path):
    marker = tmp_path / "calls"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text('#!/bin/sh\nprintf "called\\n" >> "$GH_PROBE"\n', encoding="utf-8")
    fake_gh.chmod(0o700)
    result = subprocess.run(
        ["bash", "--noprofile", "--norc"], input=command,
        text=True, capture_output=True, timeout=3, cwd=tmp_path,
        env={"PATH": str(fake_bin) + os.pathsep + os.defpath,
             "GH_PROBE": str(marker)},
    )
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "called\n"
    assert gh_identity.targets_gh(command) is True


@pytest.mark.parametrize("command", [
    "echo $((1 << 2))", "x=$((1<<3)); echo $x", 'echo "$((1 << 2))"',
])
def test_arithmetic_shift_is_not_a_heredoc_or_gh_call(command):
    assert gh_identity.has_unterminated_heredoc(command) is False
    assert gh_identity.targets_gh(command) is False


@pytest.mark.parametrize("command", [
    "echo 'echo harmless' | bash",
    'echo "$CMD" | bash',
    "echo 'gh pr list' | cat",
    "echo 'gh pr list' | bash -c 'echo harmless'",
])
def test_pipeline_scope_does_not_expand_to_data_or_dynamic_text(command):
    assert gh_identity.targets_gh(command) is False
