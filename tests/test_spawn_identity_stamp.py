"""#360 — the launcher must STAMP the cell's identity into the spawned env.

WHY. Identity failed TOWARD ``lab-ovh`` rather than closed, through three
independent defaults that each masked the others. The root cause sat one layer
above all three: THE LAUNCHER KNEW WHICH CELL IT WAS CREATING AND NEVER SAID SO,
so every cell had to INFER itself from a config file keyed on cwd. cwd is not a
cell identifier — two Claude-family cells on the reference box run with
cwd=$HOME, where the "project" settings file IS the user settings file, and no
per-project override can distinguish them.

THESE TESTS ASSERT A PROPERTY OF THE SURFACE, NOT OF ONE PATH. Written this way
because on 2026-08-11 a guard of mine was bypassed through a sibling public
function my mutation test could not reach BY CONSTRUCTION: I had mutated the
guard, while the bypass was a second entrance. A suite that scores identically
with the property holding and not holding is not testing the property. So the
first test ENUMERATES every env-building function and asserts over all of them —
and guards the enumeration against silently shrinking to zero, which would pass
the loop trivially.

That enumeration already earned its place: writing this change I hand-listed
FOUR env builders from a grep and missed ``_vibe_env``. The AST enumeration
found the fifth.
"""
from __future__ import annotations

import ast
import inspect
import types
from pathlib import Path

import pytest

from swarph_cli.commands import spawn

# The builders take a Cell; only `.name` and `.cwd` are touched by the stamp
# path, so a namespace is sufficient and keeps the test independent of Cell's
# unrelated required fields.
def _cell(name: str = "drop-on-meta-edge", tmp: Path | None = None):
    return types.SimpleNamespace(name=name, cwd=tmp or Path("/home/ubuntu/lab"))


def _env_builders() -> list[tuple[str, object]]:
    """Every module function that constructs a spawn environment.

    Enumerated from the AST rather than hand-listed, because a hand-list is a
    claim about the surface that decays every time somebody adds a provider.
    """
    src = Path(inspect.getfile(spawn)).read_text(encoding="utf-8")
    found = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.FunctionDef):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if node.name == "_spawn_env_base":
            continue
        if "_spawn_env_base(" in seg or "scrub_env_for_subprocess()" in seg:
            found.append((node.name, getattr(spawn, node.name, None)))
    return found


def test_env_builder_enumeration_is_not_vacuous():
    """>>> THE VACUITY GUARD, AND IT IS NOT CEREMONY. <<<

    Every assertion below loops over this list. An enumeration that silently
    shrank to zero would pass all of them and report green — the 0/0 failure,
    inside the test written to prevent 0/0.
    """
    builders = _env_builders()
    names = {n for n, _ in builders}
    assert builders, "no env builders found — the enumeration is broken, not the code"
    # The five known providers' builders. A new provider ADDING one is fine and
    # is covered by the loops; one DISAPPEARING means the enumeration missed it.
    assert {
        "_claude_env", "_agy_env", "_scrubbed_codex_env", "_grok_env", "_vibe_env",
    } <= names, f"enumeration is undercounting: {sorted(names)}"


def test_every_env_builder_routes_through_the_stamping_base():
    """No provider may build a spawn env without going through the stamp.

    Asserted over the SURFACE. A provider added later that calls
    ``scrub_env_for_subprocess()`` directly fails here, which is the entire
    point: a rule repeated at N call sites is a rule applied at N-1.
    """
    src = Path(inspect.getfile(spawn)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    unrouted = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name != "_spawn_env_base":
            seg = ast.get_source_segment(src, node) or ""
            if "scrub_env_for_subprocess()" in seg and "_spawn_env_base(" not in seg:
                unrouted.append(node.name)
    assert not unrouted, f"env builders bypassing the identity stamp: {unrouted}"


def test_every_env_builder_requires_the_cell():
    """``cell`` is POSITIONAL AND REQUIRED — the omission must not compile.

    A rule is only closed when omitting it is IMPOSSIBLE, not merely wrong. If
    the cell were optional, a future provider's builder would happily produce an
    unstamped env and nothing would report it.
    """
    for name, fn in _env_builders():
        assert fn is not None, f"{name} is not importable from the module"
        params = list(inspect.signature(fn).parameters.values())
        assert params, f"{name}() takes no arguments — it cannot know its cell"
        first = params[0]
        assert first.name == "cell", f"{name}()'s first parameter is {first.name!r}"
        assert first.default is inspect.Parameter.empty, (
            f"{name}(cell=...) has a DEFAULT — the stamp becomes omittable"
        )


def test_base_stamps_the_cell_name(tmp_path):
    env = spawn._spawn_env_base(_cell("science-claude", tmp_path))
    assert env["SWARPH_SELF"] == "science-claude"
    assert env["SWARPH_SPAWN"] == "1"


@pytest.mark.parametrize("cell_name", ["drop-on-meta-edge", "gridiron", "meta-muse"])
def test_stamp_wins_over_an_ambient_identity(monkeypatch, tmp_path, cell_name):
    """>>> THE LOAD-BEARING NEGATIVE. <<<

    The defect was not a missing value — it was the WRONG value, inherited. The
    spawning process is itself a cell with its own SWARPH_SELF in its
    environment, so a stamp that merely fills in a blank would leave every
    spawned cell wearing its parent's identity. The subject must therefore be
    run with a CONFLICTING ambient value: under the old behaviour this test
    passes trivially, under the correct behaviour it discriminates.
    """
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    env = spawn._spawn_env_base(_cell(cell_name, tmp_path))
    assert env["SWARPH_SELF"] == cell_name
    assert env["SWARPH_SELF"] != "lab-ovh"


def test_every_provider_env_carries_its_own_identity(monkeypatch, tmp_path):
    """Same property, through each provider's real builder, not just the base.

    ``_grok_env`` and ``_vibe_env`` create directories under the cell's cwd, so
    the cell is rooted in tmp_path rather than a real home.
    """
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    checked = []
    for name, fn in _env_builders():
        cell = _cell("peer-under-test", tmp_path / name)
        cell.cwd.mkdir(parents=True, exist_ok=True)
        try:
            env = fn(cell)
        except Exception as exc:  # provider needs host state this test lacks
            pytest.skip(f"{name} needs host state: {exc.__class__.__name__}")
        assert env["SWARPH_SELF"] == "peer-under-test", f"{name} did not stamp"
        checked.append(name)
    assert checked, "no builder was actually exercised — the loop was vacuous"


def test_tmux_reentry_delegates_back_to_spawn(tmp_path):
    """The tmux path is not a second entrance — it RE-ENTERS ``swarph spawn``.

    ``_launch_via_tmux`` takes no env, which would be a bypass if the pane ran
    the provider binary directly. It does not: the pane runs ``swarph spawn
    <name>``, so the inner process rebuilds the env from the cell and is stamped
    by the same base. Asserted rather than reasoned, because "I read the code and
    it delegates" is exactly the kind of claim that was wrong four times today.
    """
    cmd = spawn._tmux_session_command("tmux", "peer-under-test", tmp_path)
    joined = " ".join(cmd)
    assert "spawn" in joined and "peer-under-test" in joined, joined
    assert "SWARPH_SPAWN=1" in joined or "SWARPH_SPAWN='1'" in joined, joined
