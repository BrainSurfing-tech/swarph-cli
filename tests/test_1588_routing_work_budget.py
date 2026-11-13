"""Card #1588: deterministic routing work must refuse before hook timeouts fail open."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from swarph_cli import gh_identity
from swarph_cli.commands import gh_route


def _parenthesized_heredoc(size, mild=False, pattern=None):
    lines, length, i = [], 0, 0
    while length < size:
        template = pattern or ("    x = f(a, b)" if mild else "    y = g(f(a), h(b))")
        line = template.format(i=i) + f"  # {i}\n"
        lines.append(line)
        length += len(line)
        i += 1
    return "cat > fixture.py <<'EOF'\n" + "".join(lines) + "EOF\npython3 fixture.py | tail -n 3\n"


@pytest.mark.parametrize(("command", "calls_gh"), [
    pytest.param("\n".join(f"echo $(( {i} * 2 ))" for i in range(64)), False, id="64-arithmetic-echoes"),
    pytest.param("for f in *.txt; do\n" + "\n".join(
        f"  t{i}=$(( t{i} + 1 ))" for i in range(64)) + "\ndone", False, id="64-loop-arithmetic-lines"),
    pytest.param("; ".join("x=$((x+1))" for _ in range(96)), False, id="96-arithmetic-assignments"),
    pytest.param("\n".join("(( i++ ))" for _ in range(128)), False, id="128-standalone-increments"),
    pytest.param("; ".join("x=$((x+1))" for _ in range(192)) + "; gh pr list", True,
                 id="192-assignments-before-gh"),
])
def test_dense_arithmetic_keeps_the_base_hook_decision(command, calls_gh, tmp_path, monkeypatch, capsys):
    if calls_gh:
        import shutil
        if os.name != "posix" or shutil.which("bash") is None:
            pytest.skip("Bash/POSIX token replay")
        from test_1588_shell_contexts import (
            test_nested_quoted_value_is_rewritten_and_token_reaches_gh as assert_token_reaches_gh,
        )
        assert_token_reaches_gh(command, tmp_path, monkeypatch, capsys)
    else:
        assert gh_route.run_hook(json.dumps({"tool_input": {"command": command}})) == 0
        assert capsys.readouterr().out == "", "base allows this ordinary non-gh command unchanged"


def _fresh_hook(command, tmp_path, timeout):
    mapping = tmp_path / "identities.json"
    mapping.write_text(json.dumps({"budget-fixture": "fixture-login"}))
    src = str(Path(gh_identity.__file__).resolve().parents[1])
    env = {**os.environ, "SWARPH_SELF": "budget-fixture", gh_identity.MAPPING_ENV: str(mapping),
           "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
           "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-c", "from swarph_cli.commands import gh_route; raise SystemExit(gh_route.run_hook())"],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=timeout, cwd=tmp_path, env=env,
    )
    return result, time.perf_counter() - started


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CI two-second hook bound")
@pytest.mark.parametrize("depth", [64, 128, 256, 512, 1024, 1536, 2048, 8192])
def test_nested_shift_budget_returns_a_real_hook_decision_within_two_seconds(depth, tmp_path):
    command = "echo $((1 << (" * depth + "1" + ")))" * depth
    result, elapsed = _fresh_hook(command, tmp_path, 2)
    assert elapsed < 2
    print("nested shift depth", depth, "hook seconds", elapsed)
    assert result.returncode == 0, result.stderr
    assert result.stdout, "a pathological command must not pass through unchanged"
    body = json.loads(result.stdout)["hookSpecificOutput"]
    if body.get("permissionDecision") == "deny":
        assert "command too complex to route" in body["permissionDecisionReason"]
    else:
        assert body["updatedInput"]["command"] != command


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CI five-second hook bound")
@pytest.mark.parametrize(("command", "must_refuse"), [
    pytest.param("((" * 100000 + "))" * 100000, True, id="400kb-parentheses"),
    pytest.param("; ".join("x=$((x+1))" for _ in range(2048)), False, id="2048-assignments"),
    pytest.param("echo " + "$(" * 20000 + "true" + ")" * 20000, True, id="20000-substitutions"),
    pytest.param('bash -c "' + "x" * 1_000_000 + '"', False, id="1m-quoted-data"),
    pytest.param("cat > f <<'EOF'\n" + "x" * 500_000 + "\nEOF\n", False, id="500kb-heredoc"),
    pytest.param(_parenthesized_heredoc(25000), False, id="25kb-parenthesized-heredoc"),
    pytest.param(_parenthesized_heredoc(250000), False, id="250kb-parenthesized-heredoc"),
    pytest.param(_parenthesized_heredoc(250000, pattern="    x = f((a, b))"), False,
                 id="250kb-double-paren-heredoc"),
    pytest.param(_parenthesized_heredoc(250000, pattern="    n=$(( {i} + 1 ))"), False,
                 id="250kb-arithmetic-heredoc"),
    pytest.param(_parenthesized_heredoc(300000, pattern="    n=$(( {i} + 1 ))"), True,
                 id="300kb-arithmetic-heredoc-over-budget"),
    pytest.param(_parenthesized_heredoc(25000, mild=True), False, id="25kb-mild-heredoc"),
])
def test_large_hook_inputs_decide_within_five_seconds(command, must_refuse, tmp_path):
    result, elapsed = _fresh_hook(command, tmp_path, 5)
    assert result.returncode == 0, result.stderr
    assert elapsed < 5
    print("input chars", len(command), "hook seconds", elapsed)
    if must_refuse:
        body = json.loads(result.stdout)["hookSpecificOutput"]
        assert body["permissionDecision"] == "deny"
    else:
        assert not result.stdout, "base allows these valid, non-gh data commands unchanged"


@pytest.mark.parametrize("command", [
    pytest.param("echo " + "$(echo a) " * 515, id="515-flat-substitutions"),
    pytest.param('echo ' + '-f k="$(date)" ' * 642, id="642-quoted-date-arguments"),
    pytest.param('echo ' + '"$(echo a)" ' * 1280, id="1280-quoted-substitutions"),
])
def test_flat_substitutions_keep_base_allow(command, capsys):
    assert gh_route.run_hook(json.dumps({"tool_input": {"command": command}})) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("depth", [64, 100, 128])
def test_nested_quoted_substitutions_keep_base_allow(depth, capsys):
    command = "a"
    for _ in range(depth):
        command = '"$(echo ' + command + ')"'
    assert gh_route.run_hook(json.dumps({"tool_input": {"command": "x=" + command}})) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("depth", [496, 512, 1024])
def test_pathological_quote_nesting_has_an_explicit_complexity_refusal(depth, capsys):
    command = "a"
    for _ in range(depth):
        command = '"$(echo ' + command + ')"'
    assert gh_route.run_hook(json.dumps({"tool_input": {"command": "x=" + command}})) == 0
    body = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny"
    assert "command too complex to route" in body["permissionDecisionReason"]
    assert "RecursionError" not in body["permissionDecisionReason"]
    assert gh_identity._ROUTING_WORK.get() is None
    assert gh_identity._QUOTE_NESTING.get() == 0


@pytest.mark.skipif(os.name != "posix", reason="Bash/POSIX token replay")
@pytest.mark.parametrize("command", [
    "echo " + "$(echo a) " * 515,
    'echo ' + '-f k="$(date)" ' * 642,
    'echo ' + '"$(echo a)" ' * 1280,
    _parenthesized_heredoc(25000),
    _parenthesized_heredoc(250000),
    _parenthesized_heredoc(250000, pattern="    x = f((a, b))"),
    _parenthesized_heredoc(250000, pattern="    n=$(( {i} + 1 ))"),
    _parenthesized_heredoc(25000, mild=True),
], ids=["515-flat-substitutions", "642-date-arguments", "1280-quoted-substitutions",
        "25kb-paren-heredoc", "250kb-paren-heredoc", "250kb-double-paren-heredoc",
        "250kb-arithmetic-heredoc", "25kb-mild-heredoc"])
def test_valid_density_controls_still_deliver_the_token_when_gh_follows(
        command, tmp_path, monkeypatch, capsys):
    import shutil
    if shutil.which("bash") is None:
        pytest.skip("Bash oracle")
    from test_1588_shell_contexts import (
        test_nested_quoted_value_is_rewritten_and_token_reaches_gh as assert_token_reaches_gh,
    )
    # Replay shell structure, not host date/Python startup (slow on macOS).
    providers = "date() { printf '%s\\n' fixture-date; }\npython3() { :; }\n"
    assert_token_reaches_gh(providers + command + "\ngh pr list", tmp_path, monkeypatch, capsys)


@pytest.mark.skipif(os.name != "posix", reason="Bash/POSIX token replay")
@pytest.mark.parametrize("depth", [64, 100, 128])
def test_nested_quoted_substitutions_deliver_the_token_when_gh_follows(
        depth, tmp_path, monkeypatch, capsys):
    import shutil
    if shutil.which("bash") is None:
        pytest.skip("Bash oracle")
    from test_1588_shell_contexts import (
        test_nested_quoted_value_is_rewritten_and_token_reaches_gh as assert_token_reaches_gh,
    )
    command = "a"
    for _ in range(depth):
        command = '"$(echo ' + command + ')"'
    assert_token_reaches_gh("x=" + command + "; gh pr list", tmp_path, monkeypatch, capsys)


@pytest.mark.parametrize(("text", "call"), [
    ('"x"', lambda value: gh_identity._quote_end(value, 0)),
    ("(x)", lambda value: gh_identity._subst_end(value, 0)),
    ("(x)", lambda value: gh_identity._subst_end_hd(value, 0)),
    ("((1))", lambda value: gh_identity._arithmetic_span(value, 0, mask_comments=True)),
])
def test_short_matchers_never_charge_the_unvisited_tail(text, call):
    observations = []
    for suffix in ("", "z" * 100000):
        work = gh_identity._RoutingWork(10 ** 9)
        token = gh_identity._ROUTING_WORK.set(work)
        try:
            result = call(text + suffix)
            observations.append((result, work.steps))
        finally:
            gh_identity._ROUTING_WORK.reset(token)
    assert observations[0] == observations[1]
    assert observations[0][1] > 0


def test_matching_a_registered_child_span_does_not_walk_it_again():
    work = gh_identity._RoutingWork(10 ** 9)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        text = "(a(b)c)"
        assert gh_identity._subst_end(text, 0) == 6
        child = gh_identity._subspan(text, 2, 5)
        before = work.steps
        assert gh_identity._subst_end(child, 0) == 2
        assert work.steps == before
        assert gh_identity._subst_end(gh_identity._subspan(text, 0, 4), 0) == 4
    finally:
        gh_identity._ROUTING_WORK.reset(token)


def test_completed_search_only_charges_the_consumed_range():
    work = gh_identity._RoutingWork(10 ** 9)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        assert gh_identity._scan_find("ab\"" + "x" * 100000, '"', 0) == 2
        assert work.steps == 3
        assert gh_identity._scan_find("abc", '"', 1) == -1
        assert work.steps == 5
    finally:
        gh_identity._ROUTING_WORK.reset(token)


def test_hook_payload_size_boundary_is_inclusive(monkeypatch, capsys):
    monkeypatch.setattr(gh_route, "_MAX_HOOK_INPUT_CHARACTERS", 128, raising=False)
    payload = json.dumps({"tool_input": {"command": "echo fixture"}})
    payload += " " * (128 - len(payload))
    assert gh_route.run_hook(payload) == 0
    assert capsys.readouterr().out == ""
    assert gh_route.run_hook(payload + " ") == 0
    body = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny"
    assert "command too long to route" in body["permissionDecisionReason"]


def test_hook_stdin_read_is_bounded_and_oversize_refuses_before_json(monkeypatch, capsys):
    monkeypatch.setattr(gh_route, "_MAX_HOOK_INPUT_CHARACTERS", 128, raising=False)
    def read(limit=None):
        assert limit == 129, "the hook must not read an unlimited payload before checking its size"
        return "x" * 129
    class Input:
        pass
    incoming = Input()
    incoming.read = read
    monkeypatch.setattr(sys, "stdin", incoming)
    loads = json.loads
    def no_parse(_):
        raise AssertionError("oversize input reached JSON decoding")
    monkeypatch.setattr(json, "loads", no_parse)
    assert gh_route.run_hook() == 0
    body = loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny"
    assert "command too long to route" in body["permissionDecisionReason"]


def test_budget_refusal_keeps_the_clear_fail_closed_reason(monkeypatch, capsys):
    def refuse(_):
        raise gh_identity.RouterRefusal("command too complex to route; split the command")

    monkeypatch.setattr(gh_identity, "targets_gh", refuse)
    assert gh_route.run_hook(json.dumps({"tool_input": {"command": "fixture"}})) == 0
    body = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny"
    assert "command too complex to route" in body["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    'bash -c "' + "x" * 1_000_000 + '"',
    "cat > fixture-file <<'EOF'\n" + "x" * 500_000 + "\nEOF\n",
])
def test_large_linear_data_is_not_refused(command, capsys):
    assert gh_route.run_hook(json.dumps({"tool_input": {"command": command}})) == 0
    raw = capsys.readouterr().out
    if raw:
        assert json.loads(raw)["hookSpecificOutput"].get("permissionDecision") != "deny"


def test_work_is_deterministic_and_resets_after_refusal(monkeypatch):
    observed = []
    factory = gh_identity._RoutingWork

    def record(limit):
        work = factory(limit)
        observed.append(work)
        return work

    monkeypatch.setattr(gh_identity, "_RoutingWork", record)
    command = "echo $((1 << (" * 64 + "1" + ")))" * 64
    for _ in range(2):
        with pytest.raises(gh_identity.RouterRefusal, match="command too complex to route"):
            gh_identity.targets_gh(command)
        assert gh_identity._ROUTING_WORK.get() is None
        assert gh_identity._STANDALONE_BODIES.get() is None
    assert observed[0].steps == observed[1].steps
    assert observed[0].limit == min(512 * len(command) + 65536, 14000000)
    assert observed[0].steps > observed[0].limit
    assert gh_identity.targets_gh("gh pr list")
    assert observed[-1].steps < observed[-1].limit


def test_budget_is_local_to_the_current_context():
    from contextvars import copy_context

    outer = gh_identity._RoutingWork(0)
    token = gh_identity._ROUTING_WORK.set(outer)
    try:
        assert copy_context().run(gh_identity.targets_gh, "gh pr list")
        assert outer.steps == 0
        assert gh_identity._ROUTING_WORK.get() is outer
    finally:
        gh_identity._ROUTING_WORK.reset(token)


@pytest.mark.parametrize("command", [
    'echo "plain (){} unicode é 😀"; gh pr list',
    'X="$(echo "$(echo "a )")")" gh pr list',
    'echo "a\\\"b\\$c`echo d`"; gh pr list',
    'bash -c "echo harmless; gh pr list"',
    "bash <<'EOF'\necho \"$(echo a)\"\ngh pr list\nEOF",
    'echo "$(( 1 << 2 ))"; gh pr list',
    'echo "unterminated $(echo a)',
])
def test_literal_run_batching_keeps_the_complete_scan(command, monkeypatch):
    expected = gh_identity._tokenize_bash(command)
    expected_target = gh_identity.targets_gh(command)
    monkeypatch.setattr(gh_identity, "_quote_run_end", lambda text, start: start)
    assert gh_identity._tokenize_bash(command) == expected
    assert gh_identity.targets_gh(command) == expected_target


def test_hook_reuses_normal_scan_and_releases_private_state(monkeypatch, capsys):
    command = 'X="$(echo data)" gh pr list'
    calls = []
    tokenize = gh_identity._tokenize_bash_uncached

    def counted(value, **options):
        calls.append((value, gh_identity._COMMANDS_READING.get(), options))
        return tokenize(value, **options)

    monkeypatch.setattr(gh_identity, "_tokenize_bash_uncached", counted)
    monkeypatch.setattr(gh_identity, "resolve", lambda peer: gh_identity.Resolution(
        "fixture", "fixture-login", "test mapping"))
    assert gh_route.run_hook(json.dumps({"tool_input": {"command": command}})) == 0
    result = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert result["updatedInput"]["command"] != command
    assert sum(value == command and not mode and all(v is None for v in options.values())
               for value, mode, options in calls) == 1
    assert gh_identity._HOOK_STATE.get() is None
    assert gh_identity._ROUTING_WORK.get() is None
    assert gh_route.run_hook("not JSON") == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert gh_identity._HOOK_STATE.get() is None


def test_each_public_target_scan_rediscovers_arithmetic_in_hook_context():
    state = {"scans": {}, "work": None}
    token = gh_identity._HOOK_STATE.set(state)
    try:
        for _ in range(2):
            assert not gh_identity.targets_gh("echo $((1 << 2))")
            assert any(key[-2] for key in state["scans"]), "commands reading must run on both calls"
    finally:
        gh_identity._HOOK_STATE.reset(token)


def test_post_target_policy_work_still_has_the_fail_closed_budget(monkeypatch, capsys):
    def over_budget(command):
        work = gh_identity._ROUTING_WORK.get()
        assert work is not None
        gh_identity._charge_scan(work.limit + 1)

    monkeypatch.setattr(gh_identity, "has_disallowed_gh_token_assignment", over_budget)
    assert gh_route.run_hook(json.dumps({"tool_input": {"command": "gh pr list"}})) == 0
    body = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny"
    assert "command too complex to route" in body["permissionDecisionReason"]
    assert gh_identity._ROUTING_WORK.get() is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CI five-second hook bound")
@pytest.mark.parametrize("shape", ["quoted", "heredoc", "gh-quoted"])
def test_exact_default_payload_cap_decides_within_five_seconds(shape, tmp_path):
    cap = gh_route._MAX_HOOK_INPUT_CHARACTERS
    assert cap == 1536000
    prefix, suffix = {
        "quoted": ('bash -c "', '"'),
        "heredoc": ("cat <<'EOF'\n", "\nEOF\n"),
        "gh-quoted": ('gh pr view --body "', '"'),
    }[shape]
    base_size = len(json.dumps({"tool_name": "Bash", "tool_input": {"command": prefix + suffix}}))
    command = prefix + "x" * (cap - base_size) + suffix
    result, elapsed = _fresh_hook(command, tmp_path, 5)
    assert elapsed < 5
    assert result.returncode == 0, result.stderr
    if shape == "gh-quoted":
        assert json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["command"] != command
    else:
        assert result.stdout == ""
