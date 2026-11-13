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


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CI two-second hook bound")
@pytest.mark.parametrize("depth", [64, 128, 256, 512])
def test_nested_shift_budget_returns_a_real_hook_decision_within_two_seconds(depth, tmp_path):
    command = "echo $((1 << (" * depth + "1" + ")))" * depth
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
        capture_output=True, text=True, timeout=2, cwd=tmp_path, env=env,
    )
    assert time.perf_counter() - started < 2
    print("nested shift depth", depth, "hook seconds", time.perf_counter() - started)
    assert result.returncode == 0, result.stderr
    assert result.stdout, "a pathological command must not pass through unchanged"
    body = json.loads(result.stdout)["hookSpecificOutput"]
    if body.get("permissionDecision") == "deny":
        assert "command too complex to route" in body["permissionDecisionReason"]
    else:
        assert body["updatedInput"]["command"] != command


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
    assert observed[0].limit == 128 * len(command) + 65536
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
