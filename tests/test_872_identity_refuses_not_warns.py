"""No caller identity => REFUSE. The package no longer guesses `lab-ovh`.

WHAT WAS THERE (removed by this change):

    DEFAULT_CALLER_CELL = "lab-ovh"
    self_name = os.environ.get("SWARPH_SELF") or DEFAULT_CALLER_CELL

MEASURED 2026-09-18 (science-claude, mesh 43183/43186/43188; lab-ovh 43185/43187).
The gateway was never bypassed and nothing was spoofed: `_query_via_gateway` sends
NO caller field, so the server binds identity from the bearer token, correctly.
What the default decided was WHICH REAL TOKEN got presented — `_resolve_token`
reads the file the name points at. A cell querying on lab-ovh authenticated AS
lab-ovh, with lab-ovh's genuine credential, and received a full, plausible,
correctly-authorised answer belonging to someone else.

>>> A WRONG-IDENTITY WRITE ANNOUNCES ITSELF: a DM arrives signed by the wrong
cell and someone notices. A WRONG-IDENTITY READ ANNOUNCES NOTHING. <<< It surfaced
only because a repo its owner KNEW they owned was missing from their own results.

WHY REFUSE AND NOT WARN (commander, 04:44Z). A warning printed above a
correct-looking result is a line of text over an answer the caller already
believes. The cost was measured before the decision, not after: of 8 executable
`swarph` invocations in systemd + cron on lab-ovh, 7 already pass `--as` or
`--token-file`, and the 8th (`monitor reexec-on-change`) has no --gateway,
--token-file or --as argument at all, so it cannot authenticate. SCHEDULED WORK
BREAKS IN ZERO PLACES.

$SWARPH_SELF and $SWARPH_CELL still work and are the intended path — they are
DECLARED identities. What is refused is the UNDECLARED case, where a
package-level guess stood in for one.

Run: <venv>/python -m pytest tests/test_872_identity_refuses_not_warns.py -v
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from swarph_cli.commands import codegraph  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_identity(monkeypatch):
    """Both identity vars cleared. The fallback under test fires ONLY when
    neither is set, so a test that inherited the developer's own SWARPH_SELF
    would pass against the un-fixed code."""
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.delenv("SWARPH_CELL", raising=False)


# ── THE CAN-FAIL CASE ────────────────────────────────────────────────────────

def test_undeclared_identity_refuses():
    with pytest.raises(codegraph.IdentityNotDeclared) as e:
        codegraph.require_caller_cell(verb="codegraph")
    msg = str(e.value)
    assert "NO CALLER IDENTITY DECLARED" in msg
    assert "--caller-cell" in msg and "SWARPH_SELF" in msg and "SWARPH_CELL" in msg
    assert "codegraph" in msg, "the refusal must name the verb that refused"


def test_the_removed_default_is_really_gone():
    """Pins the ABSENCE. A later 'restore the default for convenience' would
    reintroduce the exact defect and every other test here would still pass."""
    assert not hasattr(codegraph, "DEFAULT_CALLER_CELL")
    # CODE ONLY. The first version of this assertion read the whole file and
    # failed on the module comment that QUOTES the removed line — a check that
    # cannot tell an explanation of a defect from the defect. Comments stripped,
    # string literals kept, same rule as pack_unserved_registry's _code_text.
    src = (Path(__file__).parent.parent / "src" / "swarph_cli" / "commands"
           / "codegraph.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert 'DEFAULT_CALLER_CELL = "lab-ovh"' not in code
    assert "or DEFAULT_CALLER_CELL" not in code


def test_it_never_silently_becomes_lab_ovh():
    """The specific failure, named. Any future fallback that resolves to a
    hardcoded cell fails here even if it is spelled differently."""
    try:
        got = codegraph.require_caller_cell(verb="codegraph")
    except codegraph.IdentityNotDeclared:
        return
    pytest.fail(f"undeclared identity resolved to {got!r} instead of refusing")


# ── the declared paths must still work, in priority order ────────────────────

def test_explicit_flag_wins(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    assert codegraph.require_caller_cell("science-claude", verb="codegraph") == "science-claude"


def test_swarph_self_is_honoured(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "drop-on-meta-edge")
    assert codegraph.require_caller_cell(verb="codegraph") == "drop-on-meta-edge"


def test_swarph_cell_is_the_mcp_fallback(monkeypatch):
    monkeypatch.setenv("SWARPH_CELL", "cursor-win")
    assert codegraph.require_caller_cell(verb="codegraph") == "cursor-win"


def test_swarph_self_beats_swarph_cell(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("SWARPH_CELL", "cursor-win")
    assert codegraph.require_caller_cell(verb="codegraph") == "lab-ovh"


def test_the_argparse_sentinel_is_not_a_cell_name(monkeypatch):
    """`--caller-cell` defaults to IDENTITY_UNSET, not None and not a string.
    Passing the sentinel through must fall to the env, never stringify into a
    cell named '<object object at 0x...>'."""
    monkeypatch.setenv("SWARPH_SELF", "gpu-wsl")
    assert codegraph.require_caller_cell(codegraph.IDENTITY_UNSET, verb="codegraph") == "gpu-wsl"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_explicit_falls_through_rather_than_becoming_a_cell(blank, monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "watchtower")
    assert codegraph.require_caller_cell(blank, verb="codegraph") == "watchtower"


def test_blank_everything_still_refuses():
    with pytest.raises(codegraph.IdentityNotDeclared):
        codegraph.require_caller_cell("   ", verb="codegraph")
