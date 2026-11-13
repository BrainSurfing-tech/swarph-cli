"""Card #1588 follow-ups: compare routing with real Bash and a fixture gh."""
import json
import os
import shutil
import subprocess
import sys

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
        command, tmp_path, monkeypatch, capsys, require_execution=True):
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

    if not require_execution:
        # Prove PATH/binary/marker portability before accepting a Bash parse difference.
        probe = run("command -v gh; command gh __fixture_probe__")
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == str(fake_gh)
        assert marker.read_text().splitlines() == ["unset"]
        marker.unlink()
    raw = run(command)
    ran_gh = marker.exists()
    if ran_gh:
        assert marker.read_text().splitlines() == ["unset"]
        marker.unlink()
    else:
        assert not require_execution, (raw.returncode, raw.stderr)
        with capsys.disabled():
            print(f"Card1588 Bash no-gh parse: {command!r}, rc={raw.returncode}, stderr={raw.stderr!r}", flush=True)
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
    if ran_gh:
        assert marker.read_text().splitlines() == ["fixture-token"]
    else:
        assert not marker.exists()


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


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("echo $((cat <<'EOF'\nit's\nEOF\ngh pr list # that's\n) | (cat))", id="drop_round2_01"),
    pytest.param("echo $((cat <<EOF\n'\nEOF\ngh pr list # '\n); (true))", id="drop_round2_02"),
    pytest.param("echo $((cat <<EOF\n'\nEOF\ngh pr list # '\n); echo $((1+1)))", id="drop_round2_03"),
    pytest.param("echo $((cat <<EOF\n'\nEOF\ngh pr list # '\n); echo $(echo x))", id="drop_round2_04"),
    pytest.param("echo $((cat <<EOF\n\"\nEOF\ngh pr list # \"\n) | (cat))", id="drop_round2_05"),
    pytest.param("x=$((cat <<EOF\n'\nEOF\ngh pr list # '\n) && (true)); echo \"$x\"", id="drop_round2_06"),
    pytest.param("echo $((bash <<< 'gh pr list') | (cat))", id="drop_round2_07"),
    pytest.param("echo $((bash <<< 'gh pr list'); (true))", id="drop_round2_08"),
    pytest.param("x=$((sh <<< \"gh pr list\") | (cat)); echo \"$x\"", id="drop_round2_09"),
    pytest.param("echo $((bash <<< 'gh pr list'); echo $(echo x))", id="drop_round2_10"),
    pytest.param("bash -c \"echo \\$((bash <<< 'gh pr list') | (cat))\"", id="drop_round2_11"),
    pytest.param("X=$((bash <<< 'gh pr list') | (cat)) true", id="drop_round2_12"),
    pytest.param("((bash <<< 'gh pr list') | (cat))", id="drop_round2_13"),
    pytest.param("((bash <<< 'gh pr list'); (true))", id="drop_round2_14"),
    pytest.param("true && ((bash <<< 'gh pr list') | (cat))", id="drop_round2_15"),
    pytest.param("x=1; ((bash <<< 'gh pr list') | (cat)); echo done", id="drop_round2_16"),
    'X=$[ 1 + 2 ] gh pr list',
    "echo $(( 1 + $((bash <<< 'gh pr list') | (cat)) ))",
])
def test_round3_subshells_and_legacy_assignment_are_rewritten(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys)


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("echo $((bash <<< 'gh pr list') | (cat))", id="single_construct_01"),
    pytest.param("echo $(( (bash <<< 'gh pr list') ))", id="single_construct_02"),
    pytest.param("echo $(((bash <<< 'gh pr list')))", id="single_construct_03"),
    pytest.param("echo $((bash <<< 'gh pr list'))", id="single_construct_04"),
    pytest.param("echo $((bash <<< 'gh pr list') )", id="single_construct_05"),
    pytest.param("echo $((true) && (bash <<< 'gh pr list'))", id="single_construct_06"),
    pytest.param("echo $(( (true) && (bash <<< 'gh pr list') ))", id="single_construct_07"),
    pytest.param("echo $((bash <<< 'gh pr list'); (true))", id="single_construct_08"),
    pytest.param("echo $((bash <<< 'gh pr list'); echo $((1+1)))", id="single_construct_09"),
    pytest.param("echo $((bash <<< 'gh pr list'); echo $(echo x))", id="single_construct_10"),
    pytest.param("echo $((bash <<< 'gh pr list') | (cat) )", id="single_construct_11"),
    pytest.param("((bash <<< 'gh pr list') | (cat))", id="single_construct_12"),
    pytest.param("(( (bash <<< 'gh pr list') ))", id="single_construct_13"),
    pytest.param("(((bash <<< 'gh pr list')))", id="single_construct_14"),
    pytest.param("((bash <<< 'gh pr list'))", id="single_construct_15"),
    pytest.param("((bash <<< 'gh pr list') )", id="single_construct_16"),
    pytest.param("((true) && (bash <<< 'gh pr list'))", id="single_construct_17"),
    pytest.param("(( (true) && (bash <<< 'gh pr list') ))", id="single_construct_18"),
    pytest.param("((bash <<< 'gh pr list'); (true))", id="single_construct_19"),
    pytest.param("((bash <<< 'gh pr list') | (cat) )", id="single_construct_20"),
    pytest.param("true; ((bash <<< 'gh pr list') | (cat)); true", id="single_construct_21"),
    pytest.param("if ((true) && (bash <<< 'gh pr list')); then :; fi", id="single_construct_22"),
    pytest.param("((true); (bash <<< 'gh pr list') | (cat))", id="single_construct_23"),
    pytest.param("echo $((true); (bash <<< 'gh pr list') | (cat))", id="single_construct_24"),
])
def test_arithmetic_classification_agrees_with_bash(command, tmp_path):
    result, marker = _bash_with_fake_gh(command, tmp_path)
    outer = command.index("((")
    end = gh_identity._subst_end(command, outer)
    # Only the command interpretation runs gh in these single-construct fixtures.
    assert gh_identity._is_arithmetic(command, outer, end) == (not marker.exists())
    if marker.exists():
        assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("body", ["(", ")", "))", "((", "( )", "')(", '\"(()\"', "$(", "`(", "# )", "EOF)"])
@pytest.mark.parametrize("wrapper", ["substitution", "standalone", "assignment"])
def test_heredoc_parentheses_follow_bash_execution(
        body, wrapper, tmp_path, monkeypatch, capsys):
    inner = "cat <<'HD'\n" + body + "\nHD\ngh pr list\n"
    if wrapper == "substitution":
        command = "echo $((" + inner + ") | (cat))"
    elif wrapper == "standalone":
        command = "((" + inner + "); (true))"
    else:
        command = 'x=$(( ' + inner + ') && (true)); echo "$x"'
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw, marker = _bash_with_fake_gh(command, raw_dir)
    if body == "# )" and wrapper == "standalone":
        version = subprocess.run(["bash", "--version"], check=True,
                                 capture_output=True, text=True).stdout.splitlines()[0]
        with capsys.disabled():
            print(f"Card1588 Bash oracle: {shutil.which('bash')}: {version}", flush=True)
    if marker.exists():
        assert marker.read_text().splitlines() == ["called"]
        routed_dir = tmp_path / "routed"
        routed_dir.mkdir()
        test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
            command, routed_dir, monkeypatch, capsys)
    else:
        # Bash versions differ on no-gh exit status; execution is the oracle.
        assert not marker.exists()


@pytest.mark.parametrize("limit", [
    "printf", "echo -e", "echo -n", "bash -x", "bash -e", "bash -",
    "env bash", "sh -s --", "tee", "here-string", "xargs",
])
def test_known_limits_names_unsupported_constant_pipeline_forms(limit):
    assert limit in gh_identity.__doc__


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("echo $((bash <<< 'gh pr list'; echo $(case x in x) :;; esac)))", id="drop_round3_1"),
    pytest.param("echo $((bash <<< 'gh pr list'; echo `case x in x) :;; esac`))", id="drop_round3_2"),
    pytest.param("x=$((bash <<< 'gh pr list' >/dev/null; echo $(case x in x) echo 1;; esac))); echo $x", id="drop_round3_3"),
    pytest.param("echo $((echo `case x in x) :;; esac`; bash <<< 'gh pr list'))", id="drop_round3_4"),
    pytest.param("echo $((bash <<< 'gh pr list'; echo $(case x in (x) :;; esac)))", id="drop_round3_5"),
    pytest.param("echo $((bash <<< 'gh pr list'; echo $(cat <<EOF\n)\nEOF\n)))", id="drop_round3_6"),
    pytest.param("echo $((bash <<< 'gh pr list'; echo `cat <<EOF\n)\nEOF\n`))", id="drop_round3_7"),
    pytest.param("((cat <<'EOF'\nit's\nEOF\n$(gh pr list) # '))", id="gpu_5317_1"),
    pytest.param("((cat <<EOF\nit's\nEOF\n$(gh pr list) # '))", id="gpu_5317_2"),
    pytest.param("echo $((cat <<'EOF'\nit's\nEOF\n$(gh pr list) # '))", id="gpu_5317_3"),
    pytest.param("echo $(((cd /tmp) | cat <<'EOF'\nit's\nEOF\n) | ((echo $(gh pr list)) && gh pr list # '\n))", id="gpu_5317_4"),
    pytest.param("echo $(( '$(gh pr list)' ))", id="gpu_5317_5"),
    pytest.param("(( x = '$(gh pr list)' ))", id="gpu_5317_6"),
    pytest.param("echo $[ '$(gh pr list)' ]", id="gpu_5317_7"),
    "echo $(( 0 * '$(gh pr list)' ))",
    "echo $(( 1 + ' `gh pr list | wc -l` ' ))",
    "echo $(( 1 + '${x:-$(gh pr list)}' ))",
    "echo $(( 1 + '$(g\\\nh pr list)' ))",
])
def test_arithmetic_union_and_apostrophe_expansions_reach_gh(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys, require_execution=sys.platform != "darwin")


def test_no_execution_still_requires_a_hook_rewrite(tmp_path, monkeypatch, capsys):
    if os.name != "posix" or shutil.which("bash") is None:
        pytest.skip("Bash/POSIX oracle")
    monkeypatch.setattr(gh_route, "run_hook", lambda _: 0)
    with pytest.raises(AssertionError, match="real hook must rewrite"):
        test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
            "((cat <<'EOF'\nit's\nEOF\ngh pr list # '))",
            tmp_path, monkeypatch, capsys, require_execution=False)


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command,ran_gh,rewrites", [
    pytest.param("((cat <<'EOF'\nits\nEOF\ngh pr list # '))", False, True, id="gpu_control_1"),
    pytest.param("((cat <<'EOF'\nsay \"hi\nEOF\n$(gh pr list) # \"))", True, True, id="gpu_control_2"),
    pytest.param("( (cat <<'EOF'\nit's\nEOF\ngh pr list # '))", False, True, id="gpu_control_3"),
    pytest.param("((cat <<'EOF'\nit's\nEOF\ngh pr list # '))", False, None, id="gpu_control_4"),
    pytest.param("((cat <<< \"it's\"\ngh pr list # '))", False, True, id="gpu_control_5"),
])
def test_arithmetic_union_controls(command, ran_gh, rewrites, tmp_path, monkeypatch, capsys):
    _, marker = _bash_with_fake_gh(command, tmp_path)
    assert marker.exists() is ran_gh
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"mapped-fixture": "fixture-login"}), encoding="utf-8")
    monkeypatch.setenv(gh_identity.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "mapped-fixture")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    gh_route.run_hook(json.dumps({"tool_input": {"command": command}}))
    output = capsys.readouterr().out
    if output:
        assert "updatedInput" in json.loads(output)["hookSpecificOutput"]
    if rewrites is not None:
        assert bool(output) is rewrites


@pytest.mark.parametrize("prefix,expected", [
    ("", "rewrite"),
    ("GH_TOKEN=fixture-denied; ", "deny"),
    ("export GH_TOKEN=$(gh auth token --user fixture-login); ", "rewrite"),
    (None, "explicit"),
])
def test_speculative_target_keeps_normal_token_policy(prefix, expected, tmp_path, monkeypatch, capsys):
    command = "echo $((bash <<< 'gh pr list'; echo $(case x in (x) :;; esac)))"
    command = (prefix + command if prefix is not None else
               "GH_TOKEN=$(gh auth token --user fixture-login) gh pr list")
    mapping = tmp_path / "gh-identities.json"
    mapping.write_text(json.dumps({"mapped-fixture": "fixture-login"}), encoding="utf-8")
    monkeypatch.setenv(gh_identity.MAPPING_ENV, str(mapping))
    monkeypatch.setenv("SWARPH_SELF", "mapped-fixture")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    modes = []
    original_strip = gh_identity._strip_allowlisted_gh_token

    def strip(command):
        modes.append(gh_identity._COMMANDS_READING.get())
        return original_strip(command)

    monkeypatch.setattr(gh_identity, "_strip_allowlisted_gh_token", strip)
    gh_route.run_hook(json.dumps({"tool_input": {"command": command}}))
    output = capsys.readouterr().out
    assert modes and not any(modes)
    assert gh_identity._COMMANDS_READING.get() is False
    if expected == "explicit":
        assert not output
    else:
        body = json.loads(output)["hookSpecificOutput"]
        if expected == "deny":
            assert body["permissionDecision"] == "deny"
            assert "updatedInput" not in body
        else:
            assert body["updatedInput"]["command"].startswith(
                "export GH_TOKEN=$(gh auth token --user fixture-login); ")


def test_speculative_reading_restores_context_after_exception(monkeypatch):
    modes = []

    def fail_in_commands_reading(command, depth):
        modes.append(gh_identity._COMMANDS_READING.get())
        if modes[-1]:
            raise RuntimeError("fixture commands-reading failure")
        return False

    monkeypatch.setattr(gh_identity, "_targets_gh", fail_in_commands_reading)
    with pytest.raises(RuntimeError, match="fixture commands-reading failure"):
        gh_identity.targets_gh("echo $((1 << 2))")
    assert modes == [False, True]
    assert gh_identity._COMMANDS_READING.get() is False


def test_speculative_reading_cannot_change_another_threads_denial_flags(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    original = gh_identity._tokenize_bash

    def pause_commands_reading(command, **kwargs):
        if gh_identity._COMMANDS_READING.get():
            entered.set()
            assert release.wait(3)
        return original(command, **kwargs)

    monkeypatch.setattr(gh_identity, "_tokenize_bash", pause_commands_reading)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(gh_identity.targets_gh, "echo $((1 << 2))")
        try:
            assert entered.wait(3)
            assert gh_identity._COMMANDS_READING.get() is False
            assert not gh_identity.has_unterminated_heredoc("echo $((1 << 2))")
        finally:
            release.set()
        assert future.result(timeout=3) is False
