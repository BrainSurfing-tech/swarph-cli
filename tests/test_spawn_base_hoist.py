"""Provider-non-discriminatory launch: BOTH Windows steps live in the BASE
ProviderMembrane.pre_launch — the named-tmux session (#129) and the Windows
Terminal rescue (#314) — so every membrane gets both when spawned with a name.
Claude now keeps NO extra; its override was deleted when the WT rescue hoisted. These tests pin the membrane DISPATCH by mocking _launch_via_tmux and
_relaunch_in_windows_terminal — they do NOT exercise _launch_via_tmux internals
(that's tests/test_spawn_tmux_session.py)."""
from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import MagicMock

from swarph_cli.commands import spawn

BIN = "/usr/bin/claude"
ARGV = ["claude", "--name", "lab"]


def _cell():
    return types.SimpleNamespace(cwd=Path("/home/ubuntu/lab"))


def _call(membrane, monkeypatch, *, session_name, tmux_ok=True, wt_ok=False):
    launch = MagicMock(return_value=tmux_ok)
    wt = MagicMock(return_value=wt_ok)
    monkeypatch.setattr(spawn, "_launch_via_tmux", launch)
    monkeypatch.setattr(spawn, "_relaunch_in_windows_terminal", wt)
    rc = membrane.pre_launch(_cell(), BIN, ARGV, no_banner=True, session_name=session_name)
    return rc, launch, wt


def test_base_launches_tmux_when_named(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
    assert rc == 0
    launch.assert_called_once_with(BIN, ARGV, Path("/home/ubuntu/lab"), "lab")


def test_base_returns_none_when_unnamed(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name=None)
    assert rc is None
    launch.assert_not_called()


def test_base_returns_none_when_tmux_declines(monkeypatch):
    m = spawn.ProviderMembrane()
    rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=False)
    assert rc is None
    launch.assert_called_once()


def test_claude_takes_base_tmux_without_reaching_wt(monkeypatch):
    m = spawn.MEMBRANES["claude"]
    rc, launch, wt = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
    assert rc == 0
    launch.assert_called_once()
    wt.assert_not_called()          # base took over → Claude's WT path not reached


def test_claude_reaches_wt_when_base_declines(monkeypatch):
    m = spawn.MEMBRANES["claude"]
    rc, launch, wt = _call(m, monkeypatch, session_name="lab", tmux_ok=False, wt_ok=True)
    assert rc == 0                  # WT relaunch took over
    wt.assert_called_once()


def test_grok_has_no_own_pre_launch():
    # Grok's override is deleted → resolves to the base implementation.
    assert spawn.GrokMembrane.pre_launch is spawn.ProviderMembrane.pre_launch


def test_codex_and_antigravity_inherit_base_tmux(monkeypatch):
    for key in ("codex", "antigravity"):
        m = spawn.MEMBRANES[key]
        assert type(m).pre_launch is spawn.ProviderMembrane.pre_launch
        rc, launch, _ = _call(m, monkeypatch, session_name="lab", tmux_ok=True)
        assert rc == 0, key
        launch.assert_called_once()


# --- #314: the Windows-Terminal rescue hoisted to base -----------------------
# Until 2026-08-05 the WT relaunch lived in ClaudeMembrane.pre_launch. Nothing in
# its body was claude-specific — only the call site was — so codex and antigravity
# (the two cells the commander launched by hand for months) never got the rescue.

def test_every_membrane_reaches_wt_when_base_tmux_declines(monkeypatch):
    """THE PAYLOAD OF THE HOIST. Every provider, not just claude.

    Written as a loop over MEMBRANES rather than a list of named cases on purpose:
    a provider added later is covered the day it is registered. A per-provider test
    would have passed for years while codex sat uncovered — which is exactly what
    happened.
    """
    assert set(spawn.MEMBRANES) >= {"claude", "codex", "antigravity", "grok", "vibe"}
    for key, m in spawn.MEMBRANES.items():
        rc, _launch, wt = _call(
            m, monkeypatch, session_name="lab", tmux_ok=False, wt_ok=True
        )
        assert rc == 0, f"{key}: WT rescue did not take over"
        wt.assert_called_once_with(BIN, ARGV, Path("/home/ubuntu/lab"))


def test_every_membrane_declines_when_neither_tmux_nor_wt(monkeypatch):
    """The negative branch. Without this, a pre_launch that returned 0
    unconditionally would pass the test above and short-circuit every spawn."""
    for key, m in spawn.MEMBRANES.items():
        rc, _launch, wt = _call(
            m, monkeypatch, session_name="lab", tmux_ok=False, wt_ok=False
        )
        assert rc is None, f"{key}: should fall through to launch(), got {rc}"
        wt.assert_called_once()


def test_claude_has_no_own_pre_launch():
    """Collapse guard, mirroring test_grok_has_no_own_pre_launch.

    An override that only calls super() is how a provider-generic step gets
    re-narrowed later: the next person to add a claude-ism puts it in the override
    and nobody notices the other four stopped matching."""
    assert spawn.ClaudeMembrane.pre_launch is spawn.ProviderMembrane.pre_launch


# --- #314: every membrane defines its working directory the same way ---------

def test_all_membranes_chdir_before_exec(monkeypatch, tmp_path):
    """codex and antigravity never chdir'd; the other three always did.

    BOTH compensated by putting the cwd in argv — codex via `-C <abs>`, antigravity
    via `--add-dir <abs>` — and an absolute Windows path with spaces re-splits
    crossing the exec boundary (measured: "unexpected argument 'REDACTED_SENSITIVE_IDENTIFIER'").
    An earlier draft of this docstring said antigravity "carries no path, the
    quieter half of the defect". That was false, from a source-grep that misfiled
    `--add-dir` under claude; the argv-measuring test below refuted it in seconds.
    """
    class _Exec(Exception):
        pass

    for key, m in spawn.MEMBRANES.items():
        seen: list = []
        monkeypatch.setattr(spawn.os, "chdir", lambda p, _s=seen: _s.append(p))
        monkeypatch.setattr(
            spawn.os, "execve", lambda *a, **k: (_ for _ in ()).throw(_Exec())
        )
        cell = types.SimpleNamespace(
            cwd=tmp_path, name=key, provider=key, role=key, git_identity=None,
        )
        try:
            m.launch(cell, f"/usr/bin/{key}", [key])
        except _Exec:
            pass
        except Exception:
            pass  # provider-specific env prep may need fixtures; chdir is the assert
        assert seen and seen[0] == tmp_path, f"{key}: did not chdir to cell.cwd"


def test_codex_windows_launch_blocks_until_codex_exits(monkeypatch, tmp_path):
    cell = types.SimpleNamespace(cwd=tmp_path, name="codex", git_identity=None)
    run = MagicMock(return_value=types.SimpleNamespace(returncode=17))
    monkeypatch.setattr(spawn.sys, "platform", "win32")
    monkeypatch.setattr(spawn.os, "chdir", lambda _cwd: None)
    monkeypatch.setattr(spawn.subprocess, "run", run)
    execve = MagicMock()
    monkeypatch.setattr(spawn.os, "execve", execve)

    result = spawn.MEMBRANES["codex"].launch(
        cell, "C:/bin/codex.exe", ["codex", "-C", "."],
    )

    assert result == 17
    run.assert_called_once()
    assert run.call_args.args[0] == ["C:/bin/codex.exe", "-C", "."]
    execve.assert_not_called()


def test_argv_path_embedders_measured_from_ACTUAL_ARGV(tmp_path):
    """DELIBERATE SCOPE LIMIT, PINNED SO IT CANNOT GROW SILENTLY.

    ONLY grok (`--cwd <abs>`) still puts an absolute path in argv. It ALSO chdirs, so
    the argument is redundant rather than load-bearing — removable, not harmless.
    Left alone in #314 to keep the diff to the two reported providers.

    THE EXPECTED SET WAS WRONG WHEN FIRST WRITTEN: it said {claude, grok}. That came
    from grepping source text for "str(cell.cwd)" against guessed builder names,
    which misfiled `--add-dir` under claude when it belongs to ANTIGRAVITY — so the
    proxy both invented a claude defect and hid a real antigravity one. Antigravity
    is now fixed (`--add-dir .`); claude never embedded a path at all.

    >>> MEASURED FROM THE ARGV EACH MEMBRANE ACTUALLY BUILDS, NOT FROM GREPPING ITS
    SOURCE. The first version of this test grepped `inspect.getsource` for
    "str(cell.cwd)" against GUESSED builder-function names, and silently missed
    claude — a proxy measurement, inside the test written to catch proxies. The
    argv is the artifact; the source is a projection of it. <<<
    """
    cell = types.SimpleNamespace(
        cwd=tmp_path, name="probe", provider="claude", role="probe",
        session_id=None, starter_prompt_path=None, sandbox=None, extra={},
        lineage=None, assisted_memory=None, schema_version="v1",
        source_path=tmp_path / "probe.yaml", yaml={},
    )
    embedders, unbuildable = set(), []
    for key, m in spawn.MEMBRANES.items():
        cell.provider = key
        # Ask the membrane its own contract rather than special-casing claude:
        # ClaudeMembrane.build_argv asserts session_id is not None, and
        # uses_pinned_session() is the declared way to know that. A hardcoded
        # `if key == "claude"` here would need editing for the next pinned provider.
        sid = "00000000-0000-4000-8000-000000000000" if m.uses_pinned_session() else None
        try:
            argv = m.build_argv(
                cell, session_id=sid, no_starter=True, passthrough=[],
                effective_role=None,
            )
        except Exception as exc:                      # noqa: BLE001
            unbuildable.append((key, type(exc).__name__))
            continue
        if any(str(tmp_path) in str(a) for a in argv):
            embedders.add(key)
    # A membrane whose argv could not be built is NOT evidence of absence. Say so
    # out loud rather than letting it silently shrink the set being asserted.
    assert not unbuildable, f"could not build argv for {unbuildable} — test is blind to these"
    assert embedders == {"grok"}, (
        f"argv path-embedders changed: {embedders}. codex AND antigravity were "
        "fixed in #314; a new entry is a new instance of the Windows re-split bug."
    )
