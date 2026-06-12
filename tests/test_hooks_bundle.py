"""T2 — HookBundle model + bundled ``cell-resilience`` throttle-detector hook.

Tests the model dataclasses, the builtin resolver, and (critically) that the
bundled shell script is syntactically valid sh AND functionally writes the
``idle_since.json`` state file with the right ``reason`` for both a throttle
(StopFailure / rate_limit) and a normal (Stop) payload — exercised hermetically
via ``env -i`` so the jq-present and jq-absent paths both hold.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_POSIX_HOOK_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bundled cell-resilience hook is POSIX /bin/sh; Windows cell-hook execution is a separate product gap (needs a cross-platform hook)",
)

from swarph_cli.commands.hooks import (
    BUILTIN_HOOKS,
    HookBinding,
    HookBundle,
    resolve_builtin,
)


# --------------------------------------------------------------------------- #
# Model + resolver
# --------------------------------------------------------------------------- #


def test_resolve_builtin_returns_cell_resilience_bundle():
    bundle = resolve_builtin("cell-resilience")
    assert isinstance(bundle, HookBundle)
    assert bundle.name == "cell-resilience"
    assert bundle.publisher == "swarph-builtin"
    assert bundle.trust == "builtin"
    assert bundle.script_name == "cell-resilience.sh"
    assert bundle.script_body  # non-empty


def test_resolve_builtin_has_exactly_two_bindings():
    bundle = resolve_builtin("cell-resilience")
    assert isinstance(bundle.bindings, tuple)
    assert len(bundle.bindings) == 2
    pairs = {(b.event, b.matcher) for b in bundle.bindings}
    assert pairs == {("StopFailure", "rate_limit"), ("Stop", "")}
    for b in bundle.bindings:
        assert isinstance(b, HookBinding)


def test_builtin_hooks_registry_contains_cell_resilience():
    assert "cell-resilience" in BUILTIN_HOOKS
    assert BUILTIN_HOOKS["cell-resilience"] is resolve_builtin("cell-resilience")


def test_resolve_builtin_unknown_names_available():
    with pytest.raises((KeyError, ValueError)) as exc:
        resolve_builtin("does-not-exist")
    # The error must name what IS available so the user can self-correct.
    assert "cell-resilience" in str(exc.value)


# --------------------------------------------------------------------------- #
# Script body content assertions
# --------------------------------------------------------------------------- #


def test_script_body_content_markers():
    body = resolve_builtin("cell-resilience").script_body
    assert body.startswith("#!"), "script must be a #!-headed script"
    assert "XDG_STATE_HOME" in body
    assert "idle_since.json" in body
    assert "throttle" in body
    assert "normal" in body


# --------------------------------------------------------------------------- #
# Syntactic validity: sh -n
# --------------------------------------------------------------------------- #


def test_script_is_valid_sh_syntax(tmp_path):
    body = resolve_builtin("cell-resilience").script_body
    script = tmp_path / "cell-resilience.sh"
    script.write_text(body, encoding="utf-8")
    sh = shutil.which("sh")
    if sh is None:  # pragma: no cover - sh present in CI
        # Degraded fallback: balanced structure heuristic.
        assert body.count("if ") <= body.count("fi") + body.count("then")
        return
    proc = subprocess.run([sh, "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"sh -n failed: {proc.stderr}"


# --------------------------------------------------------------------------- #
# Functional smoke — throttle + normal, hermetic env
# --------------------------------------------------------------------------- #


def _run_hook(tmp_path: Path, payload: str) -> Path:
    """Run the bundled script hermetically; return the state dir Path."""
    body = resolve_builtin("cell-resilience").script_body
    script = tmp_path / "cell-resilience.sh"
    script.write_text(body, encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)

    sh = shutil.which("sh") or "/bin/sh"
    # Pass through the real PATH so jq is used when present; the fallback path
    # is exercised whenever jq is absent. Either way the assertions hold.
    path = os.environ.get("PATH", "/usr/bin:/bin")
    env = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state),
        "PATH": path,
    }
    proc = subprocess.run(
        ["env", "-i", *(f"{k}={v}" for k, v in env.items()), sh, str(script)],
        input=payload,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"hook exited non-zero: {proc.stderr}"
    return state / "swarph"


def _read_idle(state_swarph: Path) -> dict:
    idle = state_swarph / "idle_since.json"
    assert idle.exists(), f"idle_since.json not written under {state_swarph}"
    return json.loads(idle.read_text(encoding="utf-8"))


@_POSIX_HOOK_SKIP
def test_functional_throttle_payload(tmp_path):
    payload = (
        '{"session_id":"abc","hook_event_name":"StopFailure",'
        '"error_type":"rate_limit"}'
    )
    state_swarph = _run_hook(tmp_path, payload)
    data = _read_idle(state_swarph)
    assert data["reason"] == "throttle"
    assert data["session"] == "abc"


@_POSIX_HOOK_SKIP
def test_functional_normal_payload(tmp_path):
    payload = '{"session_id":"abc","hook_event_name":"Stop"}'
    state_swarph = _run_hook(tmp_path, payload)
    data = _read_idle(state_swarph)
    assert data["reason"] == "normal"
    assert data["session"] == "abc"


@_POSIX_HOOK_SKIP
def test_functional_without_jq(tmp_path, monkeypatch):
    """Force the no-jq fallback by giving a PATH with no jq on it."""
    # Build a sandbox bin dir containing only the binaries the script needs
    # EXCEPT jq, so the degraded printf/sed/grep path is exercised.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    for tool in (
        "sh", "date", "mkdir", "printf", "cat", "sed", "grep", "tr", "head", "env"
    ):
        src = shutil.which(tool)
        if src:
            (fakebin / tool).symlink_to(src)
    # Sanity: jq must NOT be reachable on this PATH.
    assert shutil.which("jq", path=str(fakebin)) is None

    body = resolve_builtin("cell-resilience").script_body
    script = tmp_path / "cell-resilience.sh"
    script.write_text(body, encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)

    sh = shutil.which("sh") or "/bin/sh"
    env = {
        "HOME": str(home),
        "XDG_STATE_HOME": str(state),
        "PATH": str(fakebin),
    }
    payload = (
        '{"session_id":"xyz","hook_event_name":"StopFailure",'
        '"error_type":"rate_limit"}'
    )
    proc = subprocess.run(
        ["env", "-i", *(f"{k}={v}" for k, v in env.items()), sh, str(script)],
        input=payload,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"no-jq hook exited non-zero: {proc.stderr}"
    data = _read_idle(state / "swarph")
    assert data["reason"] == "throttle"
    assert data["session"] == "xyz"
