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


def _uncommented_arithmetic_heredoc():
    lines, length, i = [], 0, 0
    while length < 250000:
        line = f"    n=$(( {i} + 1 ))\n"
        lines.append(line)
        length += len(line)
        i += 1
    command = "cat > fixture.py <<'EOF'\n" + "".join(lines) + "EOF\npython3 fixture.py | tail -n 3\n"
    assert len(command) == 250069
    return command


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
    pytest.param("; ".join("x=$((x+1))" for _ in range(120000)), True, id="120000-assignments"),
    pytest.param("echo " + "$(" * 20000 + "true" + ")" * 20000, True, id="20000-substitutions"),
    pytest.param('bash -c "' + "x" * 1_000_000 + '"', False, id="1m-quoted-data"),
    pytest.param("cat > f <<'EOF'\n" + "x" * 500_000 + "\nEOF\n", False, id="500kb-heredoc"),
    pytest.param(_parenthesized_heredoc(25000), False, id="25kb-parenthesized-heredoc"),
    pytest.param(_parenthesized_heredoc(250000), False, id="250kb-parenthesized-heredoc"),
    pytest.param(_parenthesized_heredoc(250000, pattern="    x = f((a, b))"), False,
                 id="250kb-double-paren-heredoc"),
    pytest.param(_parenthesized_heredoc(250000, pattern="    n=$(( {i} + 1 ))"), False,
                 id="250kb-arithmetic-heredoc"),
    pytest.param(_uncommented_arithmetic_heredoc(), False, id="exact-250069-arithmetic-heredoc"),
    pytest.param(_parenthesized_heredoc(300000, pattern="    n=$(( {i} + 1 ))"), False,
                 id="300kb-arithmetic-heredoc"),
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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CI five-second hook bound")
@pytest.mark.parametrize("command", [
    pytest.param("\n".join('cat <<< "x"' for _ in range(102396)), id="102396-here-strings"),
    pytest.param("\n".join(f"cat <<E{i}\nx\nE{i}" for i in range(62326)), id="62326-short-heredocs"),
    pytest.param("cat " + " ".join(f"<<A{i}" for i in range(164705)) + "\n",
                 id="164705-heredoc-operators"),
])
def test_heredoc_density_always_gets_a_fresh_hook_decision(command, tmp_path):
    assert len(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})) <= 1536000
    result, elapsed = _fresh_hook(command, tmp_path, 5)
    assert elapsed < 5
    assert result.returncode == 0, result.stderr
    if result.stdout:
        body = json.loads(result.stdout)["hookSpecificOutput"]
        assert body["permissionDecision"] == "deny"


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
    assert gh_identity._PARSER_NESTING.get() == 0


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
    _uncommented_arithmetic_heredoc(),
], ids=["515-flat-substitutions", "642-date-arguments", "1280-quoted-substitutions",
        "25kb-paren-heredoc", "250kb-paren-heredoc", "250kb-double-paren-heredoc",
        "250kb-arithmetic-heredoc", "25kb-mild-heredoc", "exact-250069-arithmetic-heredoc"])
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
        assert work.steps - before == gh_identity._CACHE_HIT_WORK
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


def test_separator_index_preserves_rfind_bounds_and_does_not_rescan_prefixes():
    root = "before;" + "x" * 100000 + "\na(b)&tail\nend"
    work = gh_identity._RoutingWork(10 ** 9)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        for text in (root, gh_identity._subspan(root, 7, len(root) - 2)):
            for chars in ("\n", ";\n&|(){}", ";\n&|({"):
                for stop in (0, 1, len(text) // 2, len(text) - 1, len(text)):
                    assert gh_identity._previous_separator(text, stop, chars) == max(
                        text.rfind(c, 0, stop) for c in chars)
                    assert gh_identity._next_separator(text, stop, chars) == min(
                        (pos for c in chars if (pos := text.find(c, stop)) >= 0), default=-1)
        before = work.steps
        for _ in range(100):
            gh_identity._previous_separator(root, len(root), ";\n&|(){}")
        assert work.steps - before == 100 * gh_identity._CACHE_WORK
    finally:
        gh_identity._ROUTING_WORK.reset(token)


@pytest.mark.parametrize("text", [
    "(echo $(echo $(echo x)))",
    "(${x:-$(echo ) x)})",
    "(case x in (x) echo $(echo z);; esac)",
    "(cat <<EOF\ntext )\nEOF\n$(echo x))",
    "(cat <<EOF $(echo x)\na\nEOF\n)",
    "((echo $(case x in x) :;; esac)))",
    '(echo "$(echo "$(echo y)")")',
])
def test_nested_pair_memo_matches_uncached_parser_in_every_mode(text, monkeypatch):
    for commands in (False, True):
        for bounded in (False, True):
            work = gh_identity._RoutingWork(10 ** 9)
            tokens = [
                (gh_identity._ROUTING_WORK, gh_identity._ROUTING_WORK.set(work)),
                (gh_identity._COMMANDS_READING, gh_identity._COMMANDS_READING.set(commands)),
                (gh_identity._BOUNDED_COMMANDS, gh_identity._BOUNDED_COMMANDS.set(bounded)),
            ]
            try:
                positions = [i for i, c in enumerate(text) if c == "("]
                expected = [gh_identity._subst_end_hd(text, i) for i in positions]
                with monkeypatch.context() as patch:
                    patch.setattr(gh_identity, "_match_lookup", lambda *args: (None, 0, None))
                    assert [gh_identity._subst_end_hd(text, i) for i in positions] == expected
            finally:
                for variable, token in reversed(tokens):
                    variable.reset(token)


def test_write_then_execute_stops_lookahead_at_the_first_match(monkeypatch):
    prefix = "cat > script <<'EOF'\necho fixture\nEOF\n"
    text = prefix + "bash script\n" + "echo unrelated\n" * 10000
    observed = []
    original = gh_identity._iter_top_level_segments
    def record(value, start=0):
        for row in original(value, start):
            observed.append(row[2])
            yield row
    monkeypatch.setattr(gh_identity, "_iter_top_level_segments", record)
    assert gh_identity._heredoc_writes_executed_file(text, text.index("<<"), len(prefix))
    assert observed == ["cat > script <<'EOF'", "bash script"]


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
    assert observed[0].limit == min(1280 * len(command) + 65536, 120000000)
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


def _mixed_file_heredocs(size):
    fragment = r"""grep -n "error" log.txt | sort -u | head -n 5; echo "at $(date -u +%H:%M)" >> 'run.log'
cd /tmp/work && python3 - <<'PY'
import json, sys
rows = [{"id": i, "name": "row-%d" % i, 'tags': ["a", "b"]} for i in range(3)]
for r in rows:
    print((r["id"], r["name"]), len(str((r['tags'], 1))))
PY
echo "done: $(date -u +%H:%M)" | tee -a 'run.log'
grep -n "error" log.txt | sort -u | head -n 5; echo "at $(date -u +%H:%M)" >> 'run.log'
python3 - <<'PY'
import json, sys
rows = [{"id": i, "name": "row-%d" % i, 'tags': ["a", "b"]} for i in range(3)]
for r in rows:
    print(json.dumps(r), file=sys.stderr)
PY
grep -n "error" log.txt | sort -u | head -n 5; echo "at $(date -u +%H:%M)" >> 'run.log'
cd /tmp/work && python3 - <<'PY'
import json, sys
rows = [{"id": i, "name": "row-%d" % i, 'tags': ["a", "b"]} for i in range(3)]
for r in rows:
    print((r["id"], r["name"]), len(str((r['tags'], 1))))
PY
gh pr list --json number,title | jq -r '.[] | "\(.number) \(.title)"'
i=$((i+1)); total=$(( total + i * 2 )); (( i > 10 )) && echo "big"
cat > notes.txt <<'EOF'
first line of the note
second line, with a "quote"
EOF"""
    return "\n".join([fragment] * max(1, -(-size // (len(fragment) + 1))))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CI five-second hook bound")
def test_mixed_file_writing_heredocs_get_the_base_rewrite_within_five_seconds(tmp_path):
    command = _mixed_file_heredocs(250000)
    result, elapsed = _fresh_hook(command, tmp_path, 5)
    assert elapsed < 5
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout)["hookSpecificOutput"]
    assert body["updatedInput"]["command"].endswith(command)
    assert body["updatedInput"]["command"] != command


def test_file_heredoc_suffix_work_is_linear(monkeypatch):
    factory = gh_identity._RoutingWork
    observed = []
    def capture(limit):
        work = factory(10 ** 12)
        observed.append(work)
        return work
    monkeypatch.setattr(gh_identity, "_RoutingWork", capture)
    fragment = "cat > notes.txt <<'EOF'\nfixture line\nEOF"
    for count in (100, 400):
        assert not gh_identity.targets_gh("\n".join([fragment] * count))
    assert observed[1].steps < 6 * observed[0].steps


def test_execution_index_only_reuses_known_parser_boundaries():
    text = 'echo "first\nbash hidden\n"\nbash visible\npython3 second\n'
    work = gh_identity._RoutingWork(10 ** 9)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        assert not gh_identity._later_executes_paths(text, 0, {"absent"})
        for start, paths in [(0, {"visible"}), (text.index("bash hidden"), {"hidden"}),
                             (text.index("python3"), {"second"}), (len(text), {"visible"})]:
            cached = gh_identity._later_executes_paths(text, start, paths)
            nested = gh_identity._ROUTING_WORK.set(None)
            try:
                assert gh_identity._later_executes_paths(text, start, paths) == cached
            finally:
                gh_identity._ROUTING_WORK.reset(nested)
    finally:
        gh_identity._ROUTING_WORK.reset(token)


def test_cached_heredoc_paths_do_not_cross_parser_modes(monkeypatch):
    text = "cat > target <<'EOF'\nfixture\nEOF\nbash script\n"
    after = text.index("bash script")
    original = gh_identity._extract_simple_cmd_words_and_redirs
    def mode_sensitive(segment):
        if segment.startswith("cat"):
            normal = not gh_identity._COMMANDS_READING.get() and not gh_identity._BOUNDED_COMMANDS.get()
            return ["cat"], {"script" if normal else "other"}
        return original(segment)
    monkeypatch.setattr(gh_identity, "_extract_simple_cmd_words_and_redirs", mode_sensitive)
    work_token = gh_identity._ROUTING_WORK.set(gh_identity._RoutingWork(10 ** 9))
    try:
        for commands in (False, True):
            for bounded in (False, True):
                command_token = gh_identity._COMMANDS_READING.set(commands)
                bounded_token = gh_identity._BOUNDED_COMMANDS.set(bounded)
                try:
                    assert gh_identity._heredoc_writes_executed_file(text, text.index("<<"), after) == (
                        not commands and not bounded)
                finally:
                    gh_identity._BOUNDED_COMMANDS.reset(bounded_token)
                    gh_identity._COMMANDS_READING.reset(command_token)
    finally:
        gh_identity._ROUTING_WORK.reset(work_token)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CI five-second hook bound")
@pytest.mark.parametrize(("command", "must_target"), [
    pytest.param("; ".join("x=$((x+1))" for _ in range(63995)), False, id="chain-half-cap"),
    pytest.param("; ".join("x=$((x+1))" for _ in range(127995)), False, id="chain-cap"),
    pytest.param("\n".join("x=$((x+1))" for _ in range(63995)), False, id="arithmetic-lines-half-cap"),
    pytest.param("\n".join("x=$((x+1))" for _ in range(127995)), False, id="arithmetic-lines-cap"),
    pytest.param("; ".join("x=$((x+1))" for _ in range(127994)) + "; gh pr list", True,
                 id="chain-before-gh"),
    pytest.param("gh pr list; " + "; ".join("x=$((x+1))" for _ in range(127994)), True,
                 id="gh-before-chain"),
    pytest.param("\n".join('if [[ "$a" == "b" ]] && (( c > 1 )); then echo "d"; fi'
                          for _ in range(12386)), False, id="if-lines-half-cap"),
    pytest.param("\n".join('if [[ "$a" == "b" ]] && (( c > 1 )); then echo "d"; fi'
                          for _ in range(24773)), False, id="if-lines-cap"),
    pytest.param("\n".join(f'printf "%s %s\\n" "$(( {i} + 1 ))" "$(( {i} * 2 ))"'
                          for i in range(12542)), False, id="printf-half-cap"),
    pytest.param("\n".join(f'printf "%s %s\\n" "$(( {i} + 1 ))" "$(( {i} * 2 ))"'
                          for i in range(24732)), False, id="printf-cap"),
    pytest.param("cat " + " ".join(f"<<A{i}" for i in range(86561)) + "\n", False,
                 id="heredoc-operators-half-cap"),
    pytest.param("((" * 383987 + "))" * 383987, False, id="double-parens-cap"),
    pytest.param("(" * 767972 + "true" + ")" * 767972, False, id="nested-parens-cap"),
    pytest.param("eval " * 76787 + "gh pr list", True, id="eval-quarter-cap"),
    pytest.param("eval " * 307187 + "gh pr list", True, id="eval-cap"),
])
def test_pre_review_slow_rows_return_safe_fresh_hook_decisions(command, must_target, tmp_path):
    assert len(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})) <= 1536000
    result, elapsed = _fresh_hook(command, tmp_path, 5)
    assert elapsed < 5
    assert result.returncode == 0, result.stderr
    if must_target:
        assert result.stdout, "gh must not run on an unchanged command"
    if result.stdout:
        body = json.loads(result.stdout)["hookSpecificOutput"]
        if body.get("permissionDecision") == "deny":
            assert any(reason in body["permissionDecisionReason"] for reason in (
                "command too complex to route", "unterminated or unclassifiable heredoc"))
        else:
            assert body["updatedInput"]["command"] != command


@pytest.mark.parametrize("command", [
    "echo harmless", "echo 'gh pr list' | bash", "env X=x /bin/zsh -c 'gh pr list'",
    "eval 'echo harmless'", "watch -n 2 'gh pr list'", "xargs bash -c 'gh pr list'",
    "echo 'echo harmless' | sh", "echo 'текст' | bash", "echo # eval bash watch xargs",
])
def test_literal_search_prefilters_preserve_every_shell_extract(command):
    scan = gh_identity._tokenize_bash(command)
    expected = []
    for pattern, parse in [
        (gh_identity._SHELL_AT, gh_identity._c_argument),
        (gh_identity._EVAL_AT, gh_identity._joined_words),
        (gh_identity._WATCH_AT, gh_identity._watch_command),
        (gh_identity._XARGS_AT, gh_identity._xargs_shell),
        (gh_identity._ECHO_AT, gh_identity._echo_pipeline_text),
    ]:
        for match in pattern.finditer(scan.targets_visible):
            inner = parse(scan.folded, match.end())
            if inner:
                expected.append(inner)
    assert gh_identity._shell_texts(scan.folded, scan.targets_visible) == expected


def test_relative_script_prefix_cleanup_does_not_copy_each_remaining_suffix():
    work = gh_identity._RoutingWork(10 ** 9)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        assert gh_identity._normalize_script_path(" " + "./" * 100000 + "script ") == "script"
        assert work.steps == 100000 * gh_identity._CURSOR_WORK
    finally:
        gh_identity._ROUTING_WORK.reset(token)


def test_plain_joined_words_match_the_original_token_reader():
    def slow(command, i):
        parts = []
        while True:
            token = gh_identity._next_token(command, i)
            if token is None or token[0] == "sep":
                break
            i = token[2]
            if parts or token[3] != "--":
                parts.append(token[3])
        return " ".join(parts) if parts else None
    excluded = "\n\t$`[]{}<>|&;*?!~#'\"\\é"
    commands = ["", "  ", "-- --", "  -- -- gh pr list", "eval   gh pr list", "foo-- -- bar"]
    commands += [prefix + char + suffix for char in excluded
                 for prefix, suffix in [("", "gh pr list"), ("echo ", " gh pr list"), ("gh pr list", "")]]
    for command in commands:
        for start in (0, min(1, len(command)), len(command)):
            assert gh_identity._joined_words(command, start) == slow(command, start)


def _quoted_body_fragment(variant):
    header = {
        "apostrophe": "cat > p.txt", "double-quote": "cat > p.txt",
        "quoted-target": 'cat > "$P"', "append": "cat >> p.txt",
        "brace": "{ cat > p.txt", "pipe": "true | cat > p.txt",
        "plain-control": "cat > p.txt",
    }[variant]
    body = ('it is a note' if variant == "plain-control" else
            'say "hi' if variant == "double-quote" else "it's a note")
    return header + " <<'EOF'\n" + body + "\nEOF" + ("\n}" if variant == "brace" else "")


@pytest.mark.parametrize("copies", [96, 192, 768, 3072])
@pytest.mark.parametrize("position", ["none", "first", "last"])
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux fresh-hook five-second bound")
def test_apostrophe_heredocs_keep_the_base_fresh_hook_decision(copies, position, tmp_path):
    command = "\n".join([_quoted_body_fragment("apostrophe")] * copies)
    if position != "none":
        command = "gh pr list\n" + command if position == "first" else command + "\ngh pr list"
    run, elapsed = _fresh_hook(command, tmp_path, 5)
    assert run.returncode == 0, run.stderr
    assert elapsed < 5
    if position == "none":
        assert not run.stdout
    else:
        assert json.loads(run.stdout)["hookSpecificOutput"]["updatedInput"]["command"] != command


@pytest.mark.parametrize("variant", ["apostrophe", "double-quote", "quoted-target", "append", "brace", "pipe", "plain-control"])
def test_quoted_body_lookahead_keeps_every_boundary_index_and_linear_work(variant, monkeypatch):
    factory = gh_identity._RoutingWork
    works = []
    def capture(limit):
        work = factory(10 ** 12)
        works.append(work)
        return work
    monkeypatch.setattr(gh_identity, "_RoutingWork", capture)
    ratios = []
    for copies in (3, 12, 48, 192, 768, 3072):
        command = "\n".join([_quoted_body_fragment(variant)] * copies)
        assert not gh_identity.targets_gh(command)
        ratios.append(works[-1].steps / len(command))
        assert ratios[-1] < 3 * ratios[0]
    print(variant, "copies3..3072 weighted units/char", ratios)


@pytest.mark.skipif(os.name != "posix", reason="Bash/POSIX token replay")
@pytest.mark.parametrize("variant", ["apostrophe", "double-quote", "quoted-target", "append", "brace", "pipe", "plain-control"])
def test_quoted_body_variants_still_deliver_the_token(variant, tmp_path, monkeypatch, capsys):
    from test_1588_shell_contexts import (
        test_nested_quoted_value_is_rewritten_and_token_reaches_gh as replay,
    )
    command = "\n".join([_quoted_body_fragment(variant)] * 192)
    replay("P=p.txt\n" + command + "\ngh pr list", tmp_path, monkeypatch, capsys)


def test_suffix_index_queries_match_fresh_parsing_across_quote_states():
    text = "\n".join([_quoted_body_fragment(v) for v in (
        "apostrophe", "double-quote", "quoted-target", "brace", "append", "pipe")]) + "\nbash p.txt"
    starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
    expected = {(start, path): gh_identity._later_executes_paths(text, start, {path})
                for start in starts for path in ("p.txt", "absent", "cat")}
    work = gh_identity._RoutingWork(10 ** 12)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        for start in starts[::2] + starts[1::2] + list(reversed(starts)):
            for path in ("p.txt", "absent", "cat"):
                assert gh_identity._later_executes_paths(text, start, {path}) == expected[start, path]
    finally:
        gh_identity._ROUTING_WORK.reset(token)


def test_literal_runs_and_native_candidates_preserve_the_complete_scan(monkeypatch):
    import re
    commands = [
        "then g'h' pr list", 'then g"h" pr list', "then g\\h pr list",
        "  a=\"$(gh pr list)\" gh pr list", "x=${a:-b} gh pr list",
        "x=one y='two three' gh pr list", "cat <<EOF\ntext\nEOF\ngh pr list",
        "(( y=1+2 )); bash -c 'gh pr list'", "a=(1 2); echo $((1<<2))",
        _quoted_body_fragment("apostrophe"), "echo ${x:-a # b}; gh pr list",
    ]
    excluded = "\n\t$`[]{}<>|&;*?!~#'\"\\é"
    commands += ["then word" + c + "gh pr list" for c in excluded]
    commands += ["x=a" + c + " y=b; gh pr list" for c in excluded]
    contexts = [(cmd, mc, fcc, reading, bounded) for cmd in commands
                for mc in (False, True) for fcc in (False, True)
                for reading in (False, True) for bounded in (False, True)]
    def scan(entry):
        command, mc, fcc, reading, bounded = entry
        a = gh_identity._COMMANDS_READING.set(reading)
        b = gh_identity._BOUNDED_COMMANDS.set(bounded)
        try:
            return gh_identity._tokenize_bash(command, mask_comments=mc,
                fold_continuations_in_comments=fcc)
        finally:
            gh_identity._BOUNDED_COMMANDS.reset(b)
            gh_identity._COMMANDS_READING.reset(a)
    fast = [scan(entry) for entry in contexts]
    def old_candidate(text, start):
        for i in range(start, len(text)):
            if i == 0 or text[i - 1] in " \t\n;|&({":
                match = gh_identity._ASSIGN_NAME_AT.match(text, i)
                if match is not None:
                    return match
        return None
    from types import SimpleNamespace
    monkeypatch.setattr(gh_identity, "_ASSIGN_AT", SimpleNamespace(search=old_candidate))
    monkeypatch.setattr(gh_identity, "_WORD_QUOTING", re.compile(""))
    monkeypatch.setattr(gh_identity, "_PLAIN_BASH", re.compile(r"(?!)"))
    monkeypatch.setattr(gh_identity, "_LITERAL_WORD_RUN", re.compile(r"(?!)"))
    monkeypatch.setattr(gh_identity, "_SPACES_RUN", re.compile(r"[ \t]"))
    assert [scan(entry) for entry in contexts] == fast


def test_native_target_search_charges_only_completed_ranges():
    import re
    work = gh_identity._RoutingWork(10 ** 9)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        assert gh_identity._target_search(re.compile("gh"), "gh" + "x" * 100000)
        assert work.steps == 2 * gh_identity._SEARCH_WORK
        assert gh_identity._target_search(re.compile("gh"), "no match") is None
        assert work.steps == 10 * gh_identity._SEARCH_WORK
    finally:
        gh_identity._ROUTING_WORK.reset(token)


@pytest.mark.parametrize("arithmetic", [False, True])
def test_repeated_arithmetic_leaves_reuse_only_offset_free_results(arithmetic):
    text = "(x+1)"
    expected = gh_identity._scan_folded(text, base=900, arithmetic=arithmetic)
    work = gh_identity._RoutingWork(10 ** 9)
    token = gh_identity._ROUTING_WORK.set(work)
    try:
        assert gh_identity._scan_folded(text, base=0, arithmetic=arithmetic) == expected
        before = work.steps
        assert gh_identity._scan_folded(text, base=900, arithmetic=arithmetic) == expected
        assert work.steps - before == gh_identity._CACHE_HIT_WORK
        with_substitution = "$(gh pr list)"
        gh_identity._scan_folded(with_substitution, arithmetic=True)
        assert not any(key[0] == "offset-free-leaf" and key[1] == with_substitution
                       for key in work.cache)
    finally:
        gh_identity._ROUTING_WORK.reset(token)


def test_native_quote_search_matches_the_original_character_walk(monkeypatch):
    def slow(command, visible):
        if "'" not in command and '"' not in command:
            return False
        at_word = gh_identity._AT_WORD if gh_identity._ENABLE_BANG_AND_ENV_ASSIGN else gh_identity._AT_WORD_LEGACY
        i = 0
        while i < len(command):
            if ((gh_identity._RESOLVE_ANSI_C and command.startswith("$'", i))
                    or command.startswith('$"', i)):
                body, end = gh_identity._quoted_body(command, i)
                if gh_identity._GH_EXACT.match(body) and at_word.search(visible[:i]) is not None:
                    return True
                i = end if end > i else i + 2
                continue
            if command[i] not in "'\"":
                i += 1
                continue
            body, end = gh_identity._quoted_body(command, i)
            if gh_identity._GH_EXACT.match(body) and at_word.search(visible[:i]) is not None:
                return True
            i = end if end > i else i + 1
        return False
    commands = ["'gh' pr list", '"gh" pr list', "$'gh' pr list", '$"gh" pr list',
                'echo "gh"', "then g'h' pr list", "echo \\" + '"gh"',
                "echo $'\\x67h' pr list", "x='literal' gh pr list",
                _quoted_body_fragment("apostrophe")]
    for resolve in (False, True):
        for modern in (False, True):
            monkeypatch.setattr(gh_identity, "_RESOLVE_ANSI_C", resolve)
            monkeypatch.setattr(gh_identity, "_ENABLE_BANG_AND_ENV_ASSIGN", modern)
            for command in commands:
                visible = gh_identity._tokenize_bash(command).targets_visible
                assert gh_identity._quoted_gh_command_word(command, visible) == slow(command, visible)


def test_leaf_memo_never_hides_arithmetic_reading_discovery():
    work = gh_identity._RoutingWork(10 ** 9)
    work_token = gh_identity._ROUTING_WORK.set(work)
    seen_token = gh_identity._ARITHMETIC_SEEN.set(False)
    body_token = gh_identity._STANDALONE_BODIES.set({})
    try:
        for _ in range(2):
            gh_identity._ARITHMETIC_SEEN.set(False)
            gh_identity._STANDALONE_BODIES.get().clear()
            gh_identity._scan_folded("((1<<2))")
            assert gh_identity._ARITHMETIC_SEEN.get()
            assert gh_identity._STANDALONE_BODIES.get()
        assert not any(key[0] == "offset-free-leaf" and key[1] == "((1<<2))"
                       for key in work.cache)
    finally:
        gh_identity._STANDALONE_BODIES.reset(body_token)
        gh_identity._ARITHMETIC_SEEN.reset(seen_token)
        gh_identity._ROUTING_WORK.reset(work_token)
