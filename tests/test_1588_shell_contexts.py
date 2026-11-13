"""Card #1588 follow-ups: compare routing with real Bash and a fixture gh."""
import json
import os
import shutil
import subprocess

import pytest

from swarph_cli import gh_identity
from swarph_cli.commands import gh_route


def _bash_with_fake_gh(command, tmp_path):
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
    return result, marker


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
    result, marker = _bash_with_fake_gh(command, tmp_path)
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == "called\n"
    assert gh_identity.targets_gh(command) is True


@pytest.mark.parametrize("command", [
    "echo $((1 << 2))", "x=$((1<<3)); echo $x", 'echo "$((1 << 2))"',
    "(( x = 1 << 2 ))", "echo $[1<<2]",
])
def test_arithmetic_shift_is_not_a_heredoc_or_gh_call(command, tmp_path, capsys):
    assert gh_identity.has_unterminated_heredoc(command) is False
    assert gh_identity.targets_gh(command) is False
    assert gh_route.run_hook(json.dumps({
        "tool_name": "Bash", "tool_input": {"command": command},
    })) == 0
    assert capsys.readouterr().out == ""
    if os.name == "posix" and shutil.which("bash"):
        result, marker = _bash_with_fake_gh(command, tmp_path)
        assert result.returncode == 0, result.stderr
        expected = "" if command.startswith("((") else "8" if "1<<3" in command else "4"
        assert result.stdout.strip() == expected
        assert not marker.exists()


@pytest.mark.parametrize("command", [
    "echo 'echo harmless' | bash",
    'echo "$CMD" | bash',
    "echo 'gh pr list' | cat",
    "echo 'gh pr list' | bash -c 'echo harmless'",
])
def test_pipeline_scope_does_not_expand_to_data_or_dynamic_text(command):
    assert gh_identity.targets_gh(command) is False


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param('X="$(echo "$(echo "a )")")" gh pr list', id="gpu_regression_right_paren"),
    pytest.param('X="$(echo "$(echo "(")")" gh pr list', id="gpu_regression_left_paren"),
    pytest.param('X="$(echo "`echo "a )"`")" gh pr list', id="gpu_regression_backtick"),
    pytest.param('X="$(echo "a" "$(echo "b )")")" gh pr list', id="gpu_regression_sibling"),
    pytest.param('X="$(echo "x$(echo "a )")y")" gh pr list', id="gpu_regression_around"),
    pytest.param('X="$(echo "$(echo "a )")")" bash -c \'gh pr list\'',
                 id="gpu_regression_bash_c"),
    'X="`echo "a )"`" gh pr list',
    'X="$(echo "a")" gh pr list',
    'X="$(echo "a )")" gh pr list',
    'X="$(echo "$(echo "a")")" gh pr list',
    'X="$(echo "$(echo a)")" gh pr list',
    'X="$(echo "$(echo "a" "b")")" gh pr list',
    'X="$(echo "$(echo "$(echo "a )")")")" gh pr list',
    'X="$(echo "$(echo "a )")")"; gh pr list',
    'echo "$(echo "$(echo "a )")")"; gh pr list',
    'X="$(echo "$(echo "a )")")"\ngh pr list',
    "echo $((cat <<'EOF'\nit's\nEOF\ngh pr list # that's\n) | cat)",
    "echo $((cat <<EOF\n'\nEOF\ngh pr list # '\n) )",
    'echo $((cat <<EOF\n"\nEOF\ngh pr list # "\n) )',
    "echo $((cat <<EOF\n'\nEOF\ngh pr list; echo \\') )",
    'x=$((cat <<EOF\n\'\nEOF\ngh pr list # \'\n) ); echo "$x"',
    "X=$((cat <<EOF\n'\nEOF\ngh pr list # '\n) ) true",
    'bash -c "echo \\$((cat <<EOF\n\'\nEOF\ngh pr list # \'\n) )"',
    "echo $((1 << 2)) $((cat <<EOF\n'\nEOF\ngh pr list # '\n) )",
    "echo $(( 1 + $((cat <<EOF\n'\nEOF\ngh pr list >/dev/null # '\n) ; echo 1) ))",
    'echo $((cat <<\'EOF\'\nX="\nEOF\ngh pr list # "\n) | cat)',
    "echo 'gh pr list' | bash > /dev/null",
    "echo 'gh pr list' | bash 2>&1",
    "echo 'gh pr list' | bash # note",
])
def test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        '#!/bin/sh\nif [ "$1 $2" = "auth token" ]; then\n'
        '  printf "fixture-token\\n"\nelse\n'
        '  printf "%s\\n" "${GH_TOKEN:-unset}" >> "$GH_PROBE"\nfi\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    marker = tmp_path / "calls"

    def run(script):
        return subprocess.run(
            ["bash", "--noprofile", "--norc"], input=script,
            text=True, capture_output=True, timeout=3, cwd=tmp_path,
            env={"PATH": str(fake_bin) + os.pathsep + os.defpath,
                 "GH_PROBE": str(marker)},
        )

    raw = run(command)
    assert marker.read_text().splitlines() == ["unset"]
    marker.unlink()
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"mapped-fixture": "fixture-login"}), encoding="utf-8")
    monkeypatch.setenv(gh_identity.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "mapped-fixture")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert gh_route.run_hook(json.dumps({
        "tool_name": "Bash", "tool_input": {"command": command},
    })) == 0
    output = capsys.readouterr().out
    assert output, "gh ran, so the real hook must rewrite, not allow unchanged"
    rewritten = json.loads(output)["hookSpecificOutput"]["updatedInput"]["command"]
    routed = run(rewritten)
    # Expansion can execute gh before Bash reports an arithmetic error.
    assert routed.returncode == raw.returncode, routed.stderr
    assert marker.read_text().splitlines() == ["fixture-token"]


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("depth", [4, 8, 16])
def test_quoted_substitution_matching_is_not_depth_one_only(
        depth, tmp_path, monkeypatch, capsys):
    inner = 'echo "a )"'
    for _ in range(depth):
        inner = 'echo "$(' + inner + ')"'
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        'X="$(' + inner + ')" gh pr list', tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("limit", [
    "printf", "echo -e", "echo -n", "bash -x", "bash -e", "bash -",
    "env bash", "sh -s --", "tee", "here-string", "xargs",
])
def test_known_limits_names_unsupported_constant_pipeline_forms(limit):
    assert limit in gh_identity.__doc__
