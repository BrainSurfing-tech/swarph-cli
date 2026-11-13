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
        command, tmp_path, monkeypatch, capsys, require_execution=True,
        expected_decision="rewrite"):
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
    body = json.loads(output)["hookSpecificOutput"]
    if body.get("permissionDecision") == "deny":
        assert expected_decision in (None, "deny"), body
        return
    assert expected_decision in (None, "rewrite"), body
    rewritten = body["updatedInput"]["command"]
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


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("echo $(\\\n(bash <<< 'gh pr list'; echo $(case x in x) :;; esac)))", id="drop_round4_A_1"),
    pytest.param("x=$(\\\n(bash <<< 'gh pr list' >/dev/null; echo $(case x in x) echo 1;; esac))); echo $x", id="drop_round4_A_2"),
    pytest.param("echo $(\\\n(bash <<< 'gh pr list'; echo `case x in x) :;; esac`))", id="drop_round4_A_3"),
    pytest.param("echo $(\\\n(bash <<< 'gh pr list'; echo $(cat <<EOF\n)\nEOF\n)))", id="drop_round4_A_4"),
    pytest.param("bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="drop_round4_A_5"),
    pytest.param("bash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'", id="drop_round4_A_6"),
    pytest.param("bash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="drop_round4_A_7"),
    pytest.param("bash <<'EOF'\necho $(\\\n(bash <<< 'gh pr list'; echo $(case x in x) :;; esac)))\nEOF", id="drop_round4_A_8"),
    pytest.param("cat <<EOF\n$(bash <<EOF\necho a\nEOF\nX=\"$(echo \"$(echo \")\")\")\" gh pr list\necho x)\nEOF", id="drop_round4_D_9"),
    pytest.param("cat <<EOF\n$(bash <<EOF\necho a\nEOF\nX=\"$(echo \"$(echo \")\")\")\" gh pr comment 1 --body \"it's fine\"\necho x)\nEOF", id="drop_round4_D_10"),
    pytest.param("cat <<EOF\n$(bash <<EOF\necho a\nEOF\n\"gh\" pr list\necho x)\nEOF", id="drop_round4_D_11"),
    pytest.param("cat <<EOF\n$(bash <<EOF\necho a\nEOF\nX=')' gh pr list\necho x)\nEOF", id="drop_round4_D_12"),
    pytest.param("cat <<EOF\n$(bash <<EOF\necho a\nEOF\nX=\"$(echo \"$(echo \")\")\")\" gh pr list\nEOF", id="drop_round4_D_13"),
    pytest.param("cat <<EOF\n`bash <<EOF\necho a\nEOF\nX=\"$(echo \"$(echo \")\")\")\" gh pr list\necho x`\nEOF", id="drop_round4_D_14"),
    pytest.param("echo $(( 0 * $'$(gh pr list | wc -l)' ))", id="gpu_Z4_1"),
    pytest.param("(( x = $'$(gh pr list | wc -l)' ))", id="gpu_Z4_2"),
    pytest.param("echo $[ $'$(gh pr list | wc -l)' ]", id="gpu_Z4_3"),
])
def test_processed_arithmetic_and_first_heredoc_delimiter_reach_gh(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys, require_execution=sys.platform != "darwin")


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("(( y = 2 << 1 ))\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6_G1"),
    pytest.param("(( y = 2 << 1 ))\nbash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'", id="round6_G2"),
    pytest.param("(( y = 2 << 1 )); bash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6_G3"),
    pytest.param("(( y = 2 << 1 ))\nbash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6_G4"),
    pytest.param("(( y = 2 << 1 )); bash <<'EOF'\necho $(\\\n(bash <<< 'gh pr list'; echo $(case x in x) :;; esac)))\nEOF", id="round6_C1"),
    pytest.param("(( y = 2 << 1 ))\nbash <<'EOF'\necho $(\\\n(bash <<< 'gh pr list'; echo $(case x in x) :;; esac)))\nEOF", id="round6_C2"),
    pytest.param("bash <<'EOF'\necho $(\\\n(bash <<< 'gh pr list'; echo $(case x in x) :;; esac)))\nEOF\n(( y = 2 << 1 ))", id="round6_C3"),
    pytest.param("bash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF\n(( y = 2 << 1 ))", id="round6_C4"),
    pytest.param("bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'\n(( y = 2 << 1 ))", id="round6_C5"),
    pytest.param("bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6_C6"),
    pytest.param("bash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6_C7"),
    pytest.param("echo $((1 << 2))\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6_C8"),
])
def test_standalone_arithmetic_before_decoded_script_routes(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys, require_execution=sys.platform != "darwin")


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("prefix", [
    "(( y = 2 << 1 ))\n", "(( y = 2 << 1 )); ",
    "(( y = 2 >> 1 ))\n", "(( y = 2 << 1 )); (( z = 4 << 2 ))\n",
    "if (( y = 2 << 1 )); then :; fi\n",
    "{ (( y = 2 << 1 )); }\n", "( (( y = 2 << 1 )) )\n",
])
@pytest.mark.parametrize("script", [
    "bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'",
    "bash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'",
    "bash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF",
])
def test_standalone_arithmetic_compositions_keep_the_later_script(
        prefix, script, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        prefix + script, tmp_path, monkeypatch, capsys,
        require_execution=sys.platform != "darwin")


@pytest.mark.parametrize("command,expected", [
    ("(( y = 2 << 1 ))", False),
    ("$((y = 2 << 1))", False),
    ("((bash <<< 'gh pr list') | (cat))", False),
])
def test_bounded_reading_still_reads_standalone_bodies_as_commands(command, expected):
    token = gh_identity._COMMANDS_READING.set(True)
    bounded_token = gh_identity._BOUNDED_COMMANDS.set(True)
    try:
        start = int(command.startswith("$"))
        end = gh_identity._subst_end(command, start)
        assert gh_identity._is_arithmetic(command, start, end) is expected
    finally:
        gh_identity._BOUNDED_COMMANDS.reset(bounded_token)
        gh_identity._COMMANDS_READING.reset(token)


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("(( y = 2 << 1 ))\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6b_s0_later0"),
    pytest.param("(( y = 2 << 1 ))\nbash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'", id="round6b_s0_later1"),
    pytest.param("(( y = 2 << 1 ))\nbash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6b_s0_later2"),
    pytest.param("(( y = 2 << 1 ))\nbash -c \"echo \\$((bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\"", id="round6b_s0_later4"),
    pytest.param("for ((i=1; i<4; i<<=1)); do :; done\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6b_s3_later0"),
    pytest.param("for ((i=1; i<4; i<<=1)); do :; done\nbash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'", id="round6b_s3_later1"),
    pytest.param("for ((i=1; i<4; i<<=1)); do :; done\nbash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6b_s3_later2"),
    pytest.param("for ((i=1; i<4; i<<=1)); do :; done\nbash -c \"echo \\$((bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\"", id="round6b_s3_later4"),
    pytest.param("if (( 1 << 2 )); then :; fi\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6b_s5_later0"),
    pytest.param("if (( 1 << 2 )); then :; fi\nbash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'", id="round6b_s5_later1"),
    pytest.param("if (( 1 << 2 )); then :; fi\nbash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6b_s5_later2"),
    pytest.param("if (( 1 << 2 )); then :; fi\nbash -c \"echo \\$((bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\"", id="round6b_s5_later4"),
    pytest.param("(( y = 2 << 1 )); echo hi\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6b_s6_later0"),
    pytest.param("(( y = 2 << 1 )); echo hi\nbash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'", id="round6b_s6_later1"),
    pytest.param("(( y = 2 << 1 )); echo hi\nbash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6b_s6_later2"),
    pytest.param("(( y = 2 << 1 )); echo hi\nbash -c \"echo \\$((bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\"", id="round6b_s6_later4"),
    pytest.param("while (( 0 << 1 )); do :; done\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", id="round6b_s9_later0"),
    pytest.param("while (( 0 << 1 )); do :; done\nbash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'", id="round6b_s9_later1"),
    pytest.param("while (( 0 << 1 )); do :; done\nbash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF", id="round6b_s9_later2"),
    pytest.param("while (( 0 << 1 )); do :; done\nbash -c \"echo \\$((bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\"", id="round6b_s9_later4"),
    pytest.param("(( y = 2 << 1 ))\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'\n1", id="round6b_phantom0"),
    pytest.param("(( y = 2 << 1 ))\nbash -c $'echo $(\\050bash <<< \"gh pr list\"; echo $(case x in x) :;; esac)))'\n1", id="round6b_phantom1"),
    pytest.param("(( y = 2 << 1 ))\nbash <<EOF\necho \\$(\\\n(bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\nEOF\n1", id="round6b_phantom2"),
    pytest.param("(( y = 2 << 1 ))\nbash -c \"echo \\$((bash <<< 'gh pr list'; echo \\$(case x in x) :;; esac)))\"\n1", id="round6b_phantom4"),
])
def test_bounded_fallback_keeps_for_if_while_and_phantom_delimiter_tails(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys, require_execution=sys.platform != "darwin")


def test_bounded_fallback_context_resets_after_exception(monkeypatch):
    modes = []

    def fail_when_bounded(command, depth):
        modes.append((gh_identity._COMMANDS_READING.get(), gh_identity._BOUNDED_COMMANDS.get()))
        if modes[-1][1]:
            raise RuntimeError("fixture bounded failure")
        if not modes[-1][0]:
            assert gh_identity._is_arithmetic("((1 << 2))", 0, 9)
        return False

    monkeypatch.setattr(gh_identity, "_targets_gh", fail_when_bounded)
    with pytest.raises(RuntimeError, match="fixture bounded failure"):
        gh_identity.targets_gh("echo $((1 << 2))")
    assert modes == [(False, False), (True, False), (True, True)]
    assert gh_identity._COMMANDS_READING.get() is False
    assert gh_identity._BOUNDED_COMMANDS.get() is False
    assert gh_identity._ARITHMETIC_SEEN.get() is None
    assert gh_identity._STANDALONE_BODIES.get() is None


@pytest.mark.parametrize("command,body,arithmetic", [
    ("(( y = 2 << 1 ))", "y = 2 << 1", False),
    ("echo $[1 << 2]", "1 << 2", True),
])
def test_bounded_standalone_body_is_commands_not_an_arithmetic_grammar_rule(
        command, body, arithmetic, monkeypatch):
    modes = {"fold": [], "scan": []}

    def record(name, original):
        def wrapped(text, *args, **kwargs):
            if text.strip() == body:
                modes[name].append(kwargs.get("arithmetic", False))
            return original(text, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(gh_identity, "_fold_bash_continuations",
                        record("fold", gh_identity._fold_bash_continuations))
    monkeypatch.setattr(gh_identity, "_scan_folded", record("scan", gh_identity._scan_folded))
    token = gh_identity._COMMANDS_READING.set(True)
    bounded_token = gh_identity._BOUNDED_COMMANDS.set(True)
    try:
        gh_identity._tokenize_bash(command)
    finally:
        gh_identity._BOUNDED_COMMANDS.reset(bounded_token)
        gh_identity._COMMANDS_READING.reset(token)
    assert modes == {"fold": [arithmetic], "scan": [arithmetic]}


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("((cat <<EOF\na $((1 << 2)) b\nEOF; bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))\necho done", id="round7_gating_1"),
    pytest.param("((cat <<EOF\na $((1 + 2)) b\nEOF; bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))\necho done", id="round7_gating_2"),
    pytest.param("((cat <<EOF\na $((1 << 2)) b\nEOF; bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))", id="round7_gating_3"),
    pytest.param("((cat <<EOF\na b\nEOF; bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))\necho done", id="round7_gating_4"),
    pytest.param("((cat <<EOF\na b\nEOF; bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))", id="round7_gating_5"),
])
def test_ansi_c_pairing_cannot_allow_the_gating_command(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys,
        require_execution=sys.platform != "darwin", expected_decision=None)


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command,decision", [
    pytest.param("((true; bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))", "rewrite", id="round7_neighbour_1"),
    pytest.param("((bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))", "rewrite", id="round7_neighbour_2"),
    pytest.param("(bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))')", "rewrite", id="round7_neighbour_3"),
    pytest.param("echo $((bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))", "rewrite", id="round7_neighbour_4"),
    pytest.param("((bash -c $'gh pr list') | (cat))", "rewrite", id="round7_neighbour_5"),
    pytest.param("((bash -c $'echo \\x28; gh pr list') | (cat))", "rewrite", id="round7_neighbour_6"),
    pytest.param("((echo $'a)'; gh pr list) | (cat))", "rewrite", id="round7_neighbour_7"),
    pytest.param("((echo $'it\\'s)'; gh pr list) | (cat))", "rewrite", id="round7_neighbour_8"),
    pytest.param("((echo $'it\\'s'; gh pr list) | (cat))", "rewrite", id="round7_neighbour_9"),
    pytest.param("echo $((echo $'it\\'s)'; gh pr list) | (cat))", "rewrite", id="round7_neighbour_10"),
    pytest.param("((echo 'a)'; gh pr list) | (cat))", "rewrite", id="round7_neighbour_11"),
    pytest.param("((echo $'it\\'s))'; gh pr list) | (cat))", "rewrite", id="round7_neighbour_12"),
    pytest.param("echo $((echo $'it\\'s))'; gh pr list) | (cat))", "rewrite", id="round7_neighbour_13"),
    pytest.param("((echo $'it\\'s))'; bash -c 'gh pr list') | (cat))", "rewrite", id="round7_neighbour_14"),
    pytest.param("echo $'it\\'s))'; gh pr list", "rewrite", id="round7_neighbour_15"),
    pytest.param("(echo $'it\\'s))'; gh pr list)", "rewrite", id="round7_neighbour_16"),
    pytest.param("echo $(echo $'it\\'s))'; gh pr list)", "rewrite", id="round7_neighbour_17"),
    # Lab97442: v18 may become DENY; real Bash EOF warning + original4d DENY verified.
    pytest.param("((cat <<EOF\na b\nEOF; echo $'it\\'s))'; gh pr list) | (cat))", None, id="round7_neighbour_18"),
    pytest.param("((echo $'it\\'s))') | (cat))\ngh pr list", "rewrite", id="round7_neighbour_19"),
    pytest.param("echo $((echo $'it\\'s))') | (cat))\ngh pr list", "rewrite", id="round7_neighbour_20"),
    pytest.param("((echo $'it\\'s))') | (cat))\n\"gh\" pr list", "rewrite", id="round7_neighbour_21"),
    pytest.param("bash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))'", "rewrite", id="round7_neighbour_22"),
    pytest.param("((cat <<EOF\na b\nEOF\nbash -c $'echo $(\\x28bash <<< \\'gh pr list\\'; echo $(case x in x) :;; esac)))') | (cat))", "rewrite", id="round7_neighbour_23"),
])
def test_ansi_c_pairing_neighbours_keep_their_decision(
        command, decision, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys,
        require_execution=False, expected_decision=decision)


@pytest.mark.parametrize("word", [
    "$'it\\'s))('", "$'a\\\\))('", "$'a\\x27))('", "$'a\\047))('",
    "$'a\\u0027))('", "$'a\\U00000027))('",
    "$'a)('", "$'it\\'s)\n('",
])
def test_parenthesis_pairing_skips_ansi_c_escapes(word):
    command = "(echo " + word + "; echo after)tail"
    assert gh_identity._subst_end(command, 0) == command.index(")tail")


def test_unclosed_ansi_c_word_cannot_supply_a_closing_parenthesis():
    command = "(echo $'it\\'s)"
    assert gh_identity._subst_end(command, 0) == len(command)


def test_control_escape_does_not_consume_the_word_closing_quote():
    # #5352 corrects the old expectation that backslash-c consumes this quote.
    word = "$'a\\c'))('"
    command = "(echo " + word + "; echo after)tail"
    assert gh_identity._subst_end(command, 0) == command.index("))(")
    assert gh_identity._unescape_ansi_c(word, 2) == ("a\\c", 6, True)


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX ANSI-C word oracle")
@pytest.mark.parametrize("word", [r"$'\c'", r"$'a\c'", r"$'\cx'", r"$'\c)'", r"$'\c\''", r"$'\cA'", r"$'\c\n'"])
def test_control_escape_word_boundary_and_value_match_real_bash(word):
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", "printf %s " + word + "__end__"],
                            capture_output=True, timeout=3, env={"PATH": os.defpath})
    value, end, closed = gh_identity._unescape_ansi_c(word + "__end__", 2)
    assert result.returncode == 0, result.stderr
    assert (end, closed) == (len(word), True)
    assert result.stdout == value.encode() + b"__end__"


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param('((cat <<EOF\na b\nEOF; sh <<< "gh pr list"; echo $(case x in x) :;; esac)) | (cat))', id="round8_h1"),
    pytest.param("((cat <<EOF\na b\nEOF; bash <<< 'gh pr list'; echo $(case x in x) :;; esac)) | (cat))", id="round8_h2"),
    pytest.param("((cat <<'EOF'\na b\nEOF; bash <<< 'gh pr list'; echo $(case x in x) :;; esac)) | (cat))", id="round8_h3"),
    pytest.param("((cat <<EOF\na b\nEOF; cat <<< x; bash <<< 'gh pr list'; echo $(case x in x) :;; esac)) | (cat))", id="round8_h4"),
    pytest.param("((cat <<-EOF\n\ta b\n\tEOF; bash <<< 'gh pr list'; echo $(case x in x) :;; esac)) | (cat))", id="round8_h5"),
    pytest.param("((cat <<EOF\na $((1 << 2)) b\nEOF; bash <<< 'gh pr list'; echo $(case x in x) :;; esac)) | (cat))", id="round8_h6"),
    pytest.param("((cat <<EOF\na b\nEOF; true\nbash <<< 'gh pr list'; echo $(case x in x) :;; esac)) | (cat))", id="round8_h8"),
])
def test_standalone_heredoc_cannot_hide_the_rest_of_its_body(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys,
        require_execution=sys.platform != "darwin", expected_decision=None)


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    "gh pr list", "bash -c 'gh pr list'", 'eval "gh pr list"',
    'sh <<< "gh pr list"', "echo $(gh pr list)", '"gh" pr list',
    "X=1 gh pr list", "echo `gh pr list`",
])
def test_standalone_heredoc_payload_controls_stay_rewritten(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize("command", [
    "(( y = 2 << 1 ))", "echo $((1 << 2))", "x=$((1<<3))",
    "for ((i=1; i<4; i<<=1)); do :; done", "echo $[1 << 2]",
    "if (( 1 << 2 )); then :; fi", "(( y = 2 << 1 )); echo hi",
    'echo "$((1 << 2))"', ": $((1 << 2))", "while (( 0 << 1 )); do :; done",
])
def test_standalone_heredoc_fallback_does_not_route_shift_controls(command, capsys):
    assert not gh_identity.targets_gh(command)
    assert not gh_identity.has_unterminated_heredoc(command)
    assert gh_route.run_hook(json.dumps({
        "tool_name": "Bash", "tool_input": {"command": command},
    })) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("operator", ["<<EOF", "<< EOF", "<<-EOF", "<<'EOF'", '<<"EOF"'])
def test_inner_heredoc_operator_removal_keeps_here_strings_and_numeric_shifts(operator):
    body = "cat " + operator + "; bash <<< 'gh pr list'; echo $((1 << 2))"
    assert gh_identity._INNER_HEREDOC_OP.sub("", body) == (
        "cat ; bash <<< 'gh pr list'; echo $((1 << 2))")


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
@pytest.mark.parametrize("command", [
    pytest.param("X=\"$($'\\c')\" gh pr list", id="ansi_c_M1"),
    pytest.param("(e=\"$($'\\c')\" gh pr list)", id="ansi_c_M2"),
    pytest.param("X=\"$($'\\c\\'')\" gh pr list", id="ansi_c_M3"),
    pytest.param("x=$(echo $'\\\\'; X=\"$(echo $'\\c'; echo \")\")\" gh pr list)", id="ansi_c_M4"),
    pytest.param("echo $($'\\c'); gh pr list", id="ansi_c_K1"),
    pytest.param("echo $(echo $'\\c')\ngh pr list", id="ansi_c_K2"),
    pytest.param("X=\"$($'\\cx')\" gh pr list", id="ansi_c_K3"),
    pytest.param("X=\"$($'a')\" gh pr list", id="ansi_c_K4"),
    pytest.param("X=\"$(echo $'\\c)')\" gh pr list", id="ansi_c_K5"),
    pytest.param("((echo $'\\c'; echo x) | (cat))\ngh pr list", id="ansi_c_K6"),
    pytest.param("x=$(echo $'\\\\'; X=\"$(echo $'\\c\\\\'; \")\")\" gh pr list)", id="ansi_c_F1"),
    pytest.param("x=$(echo $'\\\\'; X=\"$(echo echo $'\\c'; \")\")\" gh pr list)", id="ansi_c_F2"),
    pytest.param("x=$(echo $'\\\\'; X=\"$(echo $'\\c'; echo \")\")\" gh pr list)", id="ansi_c_F3"),
    pytest.param("x=$(echo $'\\\\'; X=\"$(echo $'\\c\\\\'; echo \")\")\" gh pr list)", id="ansi_c_F4"),
    pytest.param("cat <<-EOF\nx\n`bash <<EOF\necho a\nEOF\nX=\"$($'\\c\\\\'; echo \"$(echo \")\")\")\" gh pr list\n; echo x)\nEOF", id="ansi_c_F5"),
    pytest.param("cat <<-EOF\nx\n`bash <<EOF\necho a\nEOF\nX=\"$($'\\c\\''; echo \"$(echo \")\")\")\" gh pr list\n; echo x)\nEOF", id="ansi_c_F6"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<EOF\necho a\nEOF\nX=\"$($'\\c\\\\'; echo \"$(echo \")\")\")\" gh pr list\nEOF", id="ansi_c_F7"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<-EOF\ntrue\nEOF\nX=\"$(echo $'\\c\\''; \"$(echo \")\")\")\" gh pr list\n)\nEOF", id="ansi_c_F8"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<-EOF\ntrue\nEOF\nX=\"$(echo $'\\c\\\\'; \"$(echo \")\")\")\" gh pr list\n)\nEOF", id="ansi_c_F9"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<EOF\ntrue\nEOF\nX=\"$(echo $'\\c'; echo \"$(echo \")\")\")\" gh pr list\nEOF", id="ansi_c_F10"),
    pytest.param("cat <<-EOF\nx\n`bash <<EOF\necho a\nEOF\nX=\"$(echo $'\\c'; echo \"$(echo \")\")\")\" gh pr list\n; echo x)\nEOF", id="ansi_c_F11"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<EOF\necho a\nEOF\nX=\"$(echo echo $'\\c'; \"$(echo \")\")\")\" gh pr list\nEOF", id="ansi_c_F12"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<EOF\ntrue\nEOF\nX=\"$(echo $'\\c\\''; echo \"$(echo \")\")\")\" gh pr list\nEOF", id="ansi_c_F13"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<EOF\necho a\nEOF\nX=\"$(echo $'\\c\\\\'; echo \"$(echo \")\")\")\" gh pr list\nEOF", id="ansi_c_F14"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<EOF\necho a\nEOF\nX=\"$(echo $'\\c\\''; echo \"$(echo \")\")\")\" gh pr list\nEOF", id="ansi_c_F15"),
    pytest.param("cat <<EOF >/dev/null\nx\n$(bash <<-EOF\ntrue\nEOF\nX=\"$(echo echo $'\\c\\''; \"$(echo \")\")\")\" gh pr list\n)\nEOF", id="ansi_c_F16"),
    pytest.param("true && ((echo $'\\c''; $(gh pr list)\ncat); (cat <<'EOF'\nit's\nEOF\n))", id="ansi_c_I1"),
    pytest.param("echo $(( $(echo $'\\c'; $'\\08)'; \"gh\" pr list) ))", id="ansi_c_I2"),
    pytest.param("echo $(( $($'\\c'; echo $'\\08)'; \"gh\" pr list) ))", id="ansi_c_I3"),
])
def test_ansi_c_control_escape_boundaries_route_real_gh(
        command, tmp_path, monkeypatch, capsys):
    test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
        command, tmp_path, monkeypatch, capsys, require_execution=sys.platform != "darwin")


def test_inner_heredoc_fallback_is_context_local_and_additive(monkeypatch):
    command = "((cat <<EOF\na b\nEOF; bash <<< 'gh pr list'; echo $(case x in x) :;; esac)) | (cat))"
    original = gh_identity._targets_gh
    seen = []

    def record(text, depth):
        seen.append((text, gh_identity._COMMANDS_READING.get(), gh_identity._BOUNDED_COMMANDS.get()))
        return original(text, depth)

    monkeypatch.setattr(gh_identity, "_targets_gh", record)
    assert gh_identity.targets_gh(command)
    assert seen[0] == (command, False, False)
    assert any("<<EOF" not in text and "bash <<<" in text and cmds and bounded
               for text, cmds, bounded in seen)
    assert gh_identity._STANDALONE_BODIES.get() is None
    assert gh_identity._COMMANDS_READING.get() is False
    assert gh_identity._BOUNDED_COMMANDS.get() is False
    seen.clear()
    assert gh_identity.targets_gh("gh pr list")
    assert seen == [("gh pr list", False, False)]


@pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None,
                    reason="Bash/POSIX oracle")
def test_denial_is_not_accepted_by_the_existing_rewrite_probes(tmp_path, monkeypatch, capsys):
    def deny(_):
        print(json.dumps({"hookSpecificOutput": {"permissionDecision": "deny"}}))
        return 0

    monkeypatch.setattr(gh_route, "run_hook", deny)
    with pytest.raises(AssertionError):
        test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
            "gh pr list", tmp_path, monkeypatch, capsys)


@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("bash") is None,
                    reason="Linux Bash EOF-warning proof for Lab ruling97442")
def test_v18_permitted_denial_is_an_unterminated_heredoc(tmp_path, capsys):
    command = "((cat <<EOF\na b\nEOF; echo $'it\\'s))'; gh pr list) | (cat))"
    result, _ = _bash_with_fake_gh(command, tmp_path)
    assert "here-document" in result.stderr
    assert "delimited by end-of-file" in result.stderr
    with capsys.disabled():
        print(f"v18 EOF proof rc={result.returncode}, stderr={result.stderr!r}")


def test_no_execution_still_requires_a_hook_rewrite(tmp_path, monkeypatch, capsys):
    if os.name != "posix" or shutil.which("bash") is None:
        pytest.skip("Bash/POSIX oracle")
    monkeypatch.setattr(gh_route, "run_hook", lambda _: 0)
    with pytest.raises(AssertionError, match="real hook must rewrite"):
        test_nested_quoted_value_is_rewritten_and_token_reaches_gh(
            "((cat <<'EOF'\nit's\nEOF\ngh pr list # '))",
            tmp_path, monkeypatch, capsys, require_execution=False)


@pytest.mark.parametrize("program", ["bash", "cat"])
@pytest.mark.parametrize("body", ["", "a", "a\n", "a\nb", "a\nb\n", "a\nEOF"])
def test_truncated_heredoc_preserves_source_offsets(program, body):
    command = program + " <<EOF\n" + body
    scan = gh_identity._tokenize_bash(command)
    assert scan.folded == command
    assert len(scan.visible) == len(command)
    assert len(scan.targets_visible) == len(command)


def test_commands_reading_follows_classification_not_raw_text(monkeypatch):
    modes = []
    original = gh_identity._targets_gh

    def record(command, depth):
        modes.append(gh_identity._COMMANDS_READING.get())
        return original(command, depth)

    monkeypatch.setattr(gh_identity, "_targets_gh", record)
    assert not gh_identity.targets_gh("echo harmless")
    assert modes == [False]
    modes.clear()
    command = "echo $(\\\n(1 << 2))"
    assert "((" not in command
    assert not gh_identity.targets_gh(command)
    assert modes == [False, True, True]
    assert gh_identity._ARITHMETIC_SEEN.get() is None
    gh_identity._tokenize_bash("echo $((1 << 2))")
    assert gh_identity._ARITHMETIC_SEEN.get() is None


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
        assert gh_identity._is_arithmetic("((1 << 2))", 0, 9)
        return False

    monkeypatch.setattr(gh_identity, "_targets_gh", fail_in_commands_reading)
    with pytest.raises(RuntimeError, match="fixture commands-reading failure"):
        gh_identity.targets_gh("echo $((1 << 2))")
    assert modes == [False, True]
    assert gh_identity._COMMANDS_READING.get() is False
    assert gh_identity._ARITHMETIC_SEEN.get() is None


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


@pytest.mark.parametrize("command", [
    pytest.param("gh pr list", id="simple-call"),
    pytest.param("eval " * 1000 + "gh pr list", id="deep-bare-eval"),
    pytest.param("ordinary_word " * 1000, id="long-bare-words"),
])
def test_plain_shell_text_does_not_walk_the_full_tokenizer(command, monkeypatch):
    def unexpected_walk(*args, **kwargs):
        raise AssertionError("plain text walked the full tokenizer")

    monkeypatch.setattr(gh_identity, "_fold_bash_continuations", unexpected_walk)
    assert gh_identity._tokenize_bash(command).visible == command


@pytest.mark.parametrize("mask_comments", [False, True])
def test_plain_fast_path_matches_every_full_scan_field(mask_comments):
    import random

    commands = [
        "", "gh pr list", "eval gh pr list", "x=ordinary gh pr list",
        "echo 'gh pr list'", 'echo "$(gh pr list)"', "echo `gh pr list`",
        "echo $((1 << 2))", "(( x = '$(gh pr list)' ))", "echo $[1<<2]",
        "cat <<EOF\n$(gh pr list)\nEOF", "gh pr list # gh pr view",
        "g\\\nh pr list", "echo 'unterminated", "X=1; gh pr list",
        "(gh pr list)", "{ gh pr list; }", "! gh pr list", "echo hi | gh pr list",
    ]
    rng = random.Random(5319)
    commands.extend("".join(rng.choices("ab gh./0123_- \t\n", k=rng.randrange(100)))
                    for _ in range(2000))
    for command in commands:
        folded = gh_identity._fold_bash_continuations(
            command, mask_comments=mask_comments,
            fold_continuations_in_comments=gh_identity._FOLD_CONTINUATIONS_IN_COMMENTS)
        expected = gh_identity._scan_folded(folded, mask_comments=mask_comments)
        assert gh_identity._tokenize_bash(command, mask_comments=mask_comments) == expected


@pytest.mark.parametrize("mask_comments", [False, True])
def test_plain_allowlist_complement_always_uses_full_scan(mask_comments, monkeypatch):
    import string

    allowed = set(string.ascii_letters + string.digits + " -_./:,@+")
    excluded = [chr(code) for code in range(128) if chr(code) not in allowed]
    excluded += ["\u0085", "\u00a0", "\u00e9", "\u200b", "\u2028", "\U0001f600"]
    original_fold = gh_identity._fold_bash_continuations
    walks = []

    def record_walk(*args, **kwargs):
        walks.append(True)
        return original_fold(*args, **kwargs)

    monkeypatch.setattr(gh_identity, "_fold_bash_continuations", record_walk)
    for char in excluded:
        for command in (char + "gh pr list", "gh" + char + " pr list", "gh pr list" + char):
            folded = original_fold(
                command, mask_comments=mask_comments,
                fold_continuations_in_comments=gh_identity._FOLD_CONTINUATIONS_IN_COMMENTS)
            expected = gh_identity._scan_folded(folded, mask_comments=mask_comments)
            walks.clear()
            actual = gh_identity._tokenize_bash(command, mask_comments=mask_comments)
            assert walks, repr(command)
            assert actual == expected
