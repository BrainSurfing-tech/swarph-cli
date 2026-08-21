"""Every flag our SHIPPED systemd units prescribe must exist in the CLI.

droplet's mechanism, from the 0.39.1 post-mortem (DM #8616), one level up from
"add a fixture":

    DERIVE CI FIXTURES FROM THE SHIPPED DEPLOYMENT ARTIFACTS RATHER THAN
    HAND-WRITING THEM.

His root cause for all three defects that week: *CI tested the inputs the
authors imagined, not the inputs the shipped deployment artifacts prescribe.*
The `--token-file` crash is the cleanest case — the unit file, the README and a
peer's CLAUDE.md all documented an env-style token file with comments, while
every test fixture was a bare single-line token, because that is what a test
author naturally writes. **The docs and the tests disagreed and nothing
compared them.** Two representations of one fact, diverging silently in the
region nobody looks at.

WHAT THIS TEST DOES AND DOES NOT CATCH -- stated so nobody over-trusts it:
  DOES: a shipped unit invoking a verb or flag the CLI no longer accepts. That
        breaks every deployment using it, and is invisible to a test suite that
        only ever calls the CLI the way its authors call it.
  DOES NOT: wrong VALUES behind correct flags. The token-file bug lived in the
        file's CONTENT, not its flag, and is caught by
        tests/test_token_file_one_parser.py instead.

THE RESIDUAL, recorded by droplet as a limit rather than as work (DM #8618):
this scan proves every unit's verb and flags EXIST and are ACCEPTED. It does
not prove the invocation RUNS against realistic inputs -- that coverage comes
from the token fixtures beside it. So a FUTURE flag pointing at a NEW file
shape would pass this scan and could still break on contact. THE TWO HALVES
MUST BE KEPT IN STEP: whenever a shipped unit gains a flag that names a FILE,
add a fixture of that file's real deployed shape next door. Do not read a green
deployment scan as an end-to-end guarantee.

Run: venv/bin/python -m pytest tests/test_shipped_units_match_the_cli.py -v
"""
import argparse
import pathlib
import re
import shlex

import pytest

from swarph_cli.main import _VERB_HANDLERS

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Placeholders the units use for install-time substitution.
_PLACEHOLDER = re.compile(r"<[A-Z_]+>|%[hniIft]|\$\{?\w+\}?")


def _unit_files():
    return sorted(
        list((ROOT / "deploy").rglob("*.service"))
        + list((ROOT / "src").rglob("*.service"))
    )


def _swarph_invocations():
    """(unit_name, verb, [flags]) for every `swarph <verb> ...` in an ExecStart."""
    found = []
    for unit in _unit_files():
        for m in re.finditer(r"^ExecStart=(.*)$", unit.read_text(), re.M):
            line = m.group(1)
            idx = line.find("swarph ")
            if idx == -1:
                continue
            tail = line[idx + len("swarph "):].replace("\\", " ")
            try:
                parts = shlex.split(tail)
            except ValueError:
                parts = tail.split()
            parts = [p for p in parts if p and not _PLACEHOLDER.fullmatch(p)]
            if not parts:
                continue
            verb, rest = parts[0], parts[1:]
            flags = [p for p in rest if p.startswith("--")]
            found.append((unit.name, verb, flags))
    return found


def test_the_scan_actually_finds_units():
    """A guard that greens on an empty scan measures nothing (card #116's lesson)."""
    units = _unit_files()
    assert len(units) >= 3, f"expected shipped units, found {len(units)}"
    assert _swarph_invocations(), "no swarph invocations parsed — the extractor is broken"


@pytest.mark.parametrize("unit,verb,flags", _swarph_invocations(),
                         ids=lambda v: v if isinstance(v, str) else None)
def test_shipped_unit_invokes_a_real_verb(unit, verb, flags):
    assert verb in _VERB_HANDLERS, (
        f"{unit} ExecStart invokes `swarph {verb}`, which is not a registered "
        f"verb. Every deployment using this unit would fail at boot."
    )


@pytest.mark.parametrize("unit,verb,flags", _swarph_invocations(),
                         ids=lambda v: v if isinstance(v, str) else None)
def test_shipped_unit_flags_are_accepted_by_the_cli(unit, verb, flags):
    """Parse the verb's own parser and confirm it declares every flag shipped."""
    if not flags:
        pytest.skip(f"{unit}: no long flags on this invocation")

    import importlib
    module_path, func_name = _VERB_HANDLERS[verb].rsplit(".", 1)
    mod = importlib.import_module(module_path)

    declared = set()
    for obj in vars(mod).values():
        if isinstance(obj, argparse.ArgumentParser):
            declared |= {s for a in obj._actions for s in a.option_strings}
    # Parsers are usually built inside the run_* function, so fall back to the
    # module SOURCE for the declared option strings rather than guessing.
    if not declared:
        import inspect
        src = inspect.getsource(mod)
        declared = set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', src))

    missing = [f for f in flags if f not in declared]
    assert not missing, (
        f"{unit} ExecStart passes {missing} to `swarph {verb}`, which does not "
        f"declare them. This unit is shipped in this repo — a rename here breaks "
        f"every box running it, silently, at next restart."
    )


# --- #560: the shipped unit's gateway posture -------------------------------
#
# science-claude fixed lab-ovh's INSTALLED template on 2026-08-21 and correctly left
# the SHIPPED one alone: it carries <PEER>/<HOME>/<USER> placeholders and deploys to
# other boxes, so hardcoding this mesh's tailnet IP into it would be wrong.
#
# >>> BUT "RELIES ON THE CODE DEFAULT" AND "NOBODY THOUGHT ABOUT IT" LOOK IDENTICAL IN
# A FILE THAT SIMPLY HAS NO GATEWAY LINE. <<< Before #276 that same absence pointed
# every off-box cell at localhost:8788 -- itself. Present, resolvable, and wrong.
# These tests make the silence a stated decision, so the next reader does not "fix" it
# by pasting an address in.

def _monitor_unit() -> str:
    return (ROOT / "deploy" / "monitor" / "swarph-monitor.service").read_text(
        encoding="utf-8")


def test_shipped_unit_has_no_hardcoded_gateway_address():
    """It deploys to boxes that are not on this tailnet."""
    import re
    body = "\n".join(l for l in _monitor_unit().splitlines()
                     if not l.strip().startswith("#"))
    hits = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", body)
    assert not hits, (
        f"the shipped unit pins an IP {hits} — it installs on other boxes, where that "
        "address is a wrong-by-name pointer of the same class #276 removed"
    )


def test_the_gateway_absence_is_documented_as_a_choice():
    """A missing line cannot distinguish 'decided' from 'forgotten'. The comment is
    what carries the difference, so it is the thing under test."""
    u = _monitor_unit()
    assert "MESH_GATEWAY_URL" in u, "no off-mesh escape hatch is offered anywhere"
    assert "#276" in u, "the comment must name the code default it relies on"


def test_the_escape_hatch_is_environment_not_an_execstart_flag():
    """>>> MEASURED, NOT PREFERRED (science-claude, lab-ovh, 2026-08-21). <<< Every
    drop-in on a multi-cell box overrides ExecStart wholesale, so a --gateway baked
    into the template's ExecStart is DISCARDED by exactly the cells that use drop-ins
    — protecting only the cells that needed no protection. Environment= survives an
    ExecStart override."""
    u = _monitor_unit()
    assert "# Environment=MESH_GATEWAY_URL=<GATEWAY>" in u, (
        "the escape hatch must be a commented Environment= line"
    )
    exec_lines = [l for l in u.splitlines() if l.startswith("ExecStart=")]
    assert exec_lines, "no ExecStart in the unit"
    assert not any("--gateway" in l for l in exec_lines), (
        "a --gateway on ExecStart is discarded by any drop-in that overrides ExecStart"
    )
