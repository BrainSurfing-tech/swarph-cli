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


# --- #560/#561: the shipped unit's gateway posture --------------------------
#
# science-claude fixed lab-ovh's INSTALLED template on 2026-08-21 and correctly left
# the SHIPPED one alone: it carries <PEER>/<HOME>/<USER> placeholders and deploys to
# other boxes, so hardcoding this mesh's tailnet IP into it would be wrong.
#
# >>> BUT "RELIES ON THE CODE DEFAULT" AND "NOBODY THOUGHT ABOUT IT" LOOK IDENTICAL IN
# A FILE THAT SIMPLY HAS NO GATEWAY LINE. <<< Before #276 that same absence pointed
# every off-box cell at localhost:8788 -- itself. Present, resolvable, and wrong.
#
# #561 -- drop-on-meta-edge, review of PR #282, two findings, both taken:
#
#   1. ZERO can-fail cases. All three guards asserted against the real shipped unit,
#      which is currently correct, so NONE had ever been observed failing. By the
#      standard this repo already applies elsewhere -- a detector that has only seen
#      clean input is indistinguishable from one that matches nothing -- they were
#      three UNVERIFIED detectors. Every guard below now runs against a synthetic BAD
#      unit too.
#   2. Guard 1 was IPv4-ONLY while the property is "no address pinned to this mesh".
#      Measured against its own regex: it CAUGHT 100.107.222.72 and MISSED lab-ovh,
#      lab-ovh.tail288b3b.ts.net and [fd7a::1]. >>> A HOSTNAME IS THE *MORE* LIKELY
#      PIN, NOT THE LESS -- #546 weighed MagicDNS names against the IP explicitly, so
#      an editor reaching for the "nicer" form writes exactly the value the guard
#      could not see. <<< Now the property is tested directly: the value must BE the
#      placeholder, rather than enumerating the shapes a real address can take.

_MONITOR_UNIT = ROOT / "deploy" / "monitor" / "swarph-monitor.service"


def _monitor_unit() -> str:
    return _MONITOR_UNIT.read_text(encoding="utf-8")


def _join_continuations(text: str) -> str:
    """systemd directives may span lines with a trailing backslash.

    >>> AND deploy/sidecar/claude-tmux@.service ALREADY DOES (line 39-41). <<<
    (drop-on-meta-edge, review of PR #283.) A scanner that filters on
    `line.startswith("ExecStart=")` sees only the FIRST physical line, so anything on a
    continuation is invisible -- while being just as discarded by a drop-in, which is
    the exact failure the guard exists to catch. The blind spot is demonstrated by a
    file in this repo, not hypothesised.
    """
    return re.sub(r"\\\s*\n\s*", " ", text)


def _uncommented(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))


# --- the three properties, as predicates over unit TEXT so a synthetic bad unit can
#     be fed to exactly the code that guards the real one. -----------------------

def _pinned_gateway_values(text: str) -> list:
    """Every MESH_GATEWAY_URL= whose value is not the <GATEWAY> placeholder.

    Tests the PROPERTY (the value is a placeholder) rather than the SHAPES a real
    address can take -- IPv4, MagicDNS short name, MagicDNS FQDN, bracketed IPv6.
    Commented lines are included on purpose: the escape hatch is a comment, and a
    pinned address commented out is still the thing someone uncomments.
    """
    out = []
    for line in _join_continuations(text).splitlines():
        m = re.search(r"MESH_GATEWAY_URL=(\S*)", line)
        if not m:
            continue
        # systemd-LEGAL forms that are NOT pins (drop, review of PR #283):
        #   Environment="MESH_GATEWAY_URL=<GATEWAY>"   quoting is ordinary
        #   Environment=MESH_GATEWAY_URL=              an explicit UNSET
        # >>> A GUARD THAT REDS ON LEGITIMATE INPUT IS ONE THE NEXT PERSON DELETES. <<<
        # That is this repo's own reasoning from the #276 docstring case, arriving in
        # the guard built from that lesson. Neither form is in the shipped unit today,
        # so nothing was broken -- the cost was guard LONGEVITY, not correctness.
        value = m.group(1).strip('"\'')
        if value in ("", "<GATEWAY>"):
            continue
        out.append(line.strip())
    return out


def _has_execstart_gateway_flag(text: str) -> bool:
    # >>> UNCOMMENT FIRST, THEN JOIN — THE OTHER ORDER FAILS *SILENT*. <<<
    # (drop-on-meta-edge, review of PR #283, battery re-run here before taking it.)
    # Joining first lets a COMMENT ending in a backslash swallow the real directive
    # beneath it: the two lines become one starting with '#', and the uncomment pass
    # then deletes the ExecStart along with the comment.
    #
    #     # a note ending in a backslash \
    #     ExecStart=/x/swarph monitor --gateway http://x    <- guard returned False
    #
    # Findings 1 and 2 on this file were a guard that MISSED a shape and one that
    # FIRED on legal input. This one goes QUIET ON A REAL PIN — the only failure of
    # the three that is invisible. Reachability is highest in THIS file, which now
    # carries a fourteen-line comment block and a commented `# Environment=` example;
    # a commented-out multi-line ExecStart example is the natural next addition.
    #
    # Uncommenting first is safe for the continuation case: a real multi-line
    # ExecStart contains no '#' lines, and a commented-out block has '#' on every
    # line, so removing them leaves nothing to join.
    return any("--gateway" in l
               for l in _join_continuations(_uncommented(text)).splitlines()
               if l.startswith("ExecStart="))


def _absence_is_explained(text: str) -> bool:
    return "MESH_GATEWAY_URL" in text and "#276" in text


# --- against the REAL shipped unit -------------------------------------------

def test_shipped_unit_pins_no_gateway_address():
    bad = _pinned_gateway_values(_monitor_unit())
    assert not bad, (
        f"the shipped unit pins a gateway {bad} — it installs on other boxes, where "
        "any address from this mesh (IP or MagicDNS name) is a wrong-by-name pointer "
        "of the class #276 removed"
    )


def test_the_gateway_absence_is_documented_as_a_choice():
    assert _absence_is_explained(_monitor_unit())


def test_the_escape_hatch_is_environment_not_an_execstart_flag():
    """>>> MEASURED, NOT PREFERRED. <<< science-claude on lab-ovh, confirmed by drop
    against the DEPLOYED units: every drop-in overrides ExecStart (`ExecStart=` to
    clear, then its own line), so a --gateway in the template's ExecStart is DISCARDED
    by exactly the cells that use drop-ins — protecting only the cells that needed no
    protection. `systemctl show` on cursor-lin: ExecStart = the drop-in's line,
    Environment = still carries MESH_GATEWAY_URL. The installed template already uses
    Environment= for this, so the shipped file is catching up to deployment rather
    than proposing a design."""
    u = _monitor_unit()
    assert "# Environment=MESH_GATEWAY_URL=<GATEWAY>" in u
    assert not _has_execstart_gateway_flag(u)


# --- PROVE EACH ONE FIRES -----------------------------------------------------

_PINNED = [
    "http://100.107.222.72:8788",          # the IPv4 the old guard caught
    "http://lab-ovh:8788",                 # MagicDNS short name — MISSED before
    "http://lab-ovh.tail288b3b.ts.net:8788",  # MagicDNS FQDN — MISSED before
    "http://[fd7a::1]:8788",               # bracketed IPv6 — MISSED before
]


@pytest.mark.parametrize("addr", _PINNED)
def test_the_pin_guard_fires_on_every_address_shape(addr):
    """The IPv4-only version passed on three of these four."""
    unit = f"[Service]\nEnvironment=MESH_GATEWAY_URL={addr}\nExecStart=/x/swarph monitor\n"
    assert _pinned_gateway_values(unit), f"guard blind to {addr!r}"


def test_the_pin_guard_fires_on_a_COMMENTED_pin():
    """A pinned address commented out is still the line someone uncomments."""
    assert _pinned_gateway_values("# Environment=MESH_GATEWAY_URL=http://lab-ovh:8788\n")


def test_the_pin_guard_accepts_the_placeholder():
    """Without this the guard could pass by matching nothing, and a green suite would
    say the same thing either way."""
    assert _pinned_gateway_values("# Environment=MESH_GATEWAY_URL=<GATEWAY>\n") == []


def test_the_documentation_guard_fires_on_a_bare_unit():
    """A unit with no gateway line and no explanation — the exact state before #560."""
    assert not _absence_is_explained(
        "[Service]\nEnvironment=SWARPH_SELF=<PEER>\nExecStart=/x/swarph monitor\n")


def test_the_execstart_guard_fires_on_a_flag_in_execstart():
    assert _has_execstart_gateway_flag(
        "[Service]\nExecStart=/x/swarph monitor start --gateway http://x:8788\n")


def test_the_execstart_guard_ignores_a_flag_inside_a_COMMENT():
    """A comment may NAME the defect it forbids without committing it — the whole
    #560 comment block discusses --gateway, and a guard that reds on prose gets
    deleted by the next person, which is how the property is lost."""
    assert not _has_execstart_gateway_flag(
        "# do not put --gateway on ExecStart\nExecStart=/x/swarph monitor\n")


# --- #283 review, drop-on-meta-edge: three boundary cases ---------------------

def test_the_execstart_guard_sees_continuation_lines():
    """>>> DEMONSTRATED, NOT HYPOTHESISED: deploy/sidecar/claude-tmux@.service ships a
    multi-line ExecStart today. <<< A --gateway on line 2 of such a directive is just
    as discarded by a drop-in as one on line 1, so a guard blind to it is blind to the
    failure it exists to catch."""
    unit = ("[Service]\n"
            "ExecStart=/bin/sh -lc 'x' \\\n"
            "  --gateway http://lab-ovh:8788\n")
    assert _has_execstart_gateway_flag(unit)


def test_the_real_repo_has_a_multiline_execstart():
    """The premise above is a fact about this repo. If it stops being true the test
    above is testing a shape nothing uses, and someone should know."""
    sidecar = (ROOT / "deploy" / "sidecar" / "claude-tmux@.service")
    if not sidecar.exists():
        pytest.skip("sidecar unit not present")
    body = sidecar.read_text(encoding="utf-8")
    assert "\\\n" in body, "no continuation left in deploy/ — the blind spot is now theoretical"


@pytest.mark.parametrize("line", [
    'Environment="MESH_GATEWAY_URL=<GATEWAY>"',   # quoting is ordinary systemd
    "Environment=MESH_GATEWAY_URL=",              # an explicit unset
])
def test_the_pin_guard_does_not_red_on_legal_non_pins(line):
    """A guard that fires on legitimate input gets deleted, and then the property is
    gone entirely. The direction of this class of bug is the safe one -- it costs
    longevity, not correctness -- which is exactly why it survives review."""
    assert _pinned_gateway_values(line + "\n") == []


def test_a_quoted_REAL_pin_is_still_caught():
    """Stripping quotes must not become a hole: the quoted form of a real address is
    still a pin."""
    assert _pinned_gateway_values('Environment="MESH_GATEWAY_URL=http://lab-ovh:8788"\n')


def test_a_comment_ending_in_a_backslash_does_not_swallow_the_next_directive():
    """>>> THE ONLY ONE OF THE THREE #283 FINDINGS WHOSE FAILURE IS SILENT. <<<

    A missed shape and a false positive both announce themselves eventually — one on
    the next review, one on the next red run. A guard that goes QUIET on a real pin
    announces nothing at all, and is indistinguishable from a clean file.
    """
    unit = ("# a note ending in a backslash \\\n"
            "ExecStart=/x/swarph monitor --gateway http://lab-ovh:8788\n")
    assert _has_execstart_gateway_flag(unit)


def test_a_commented_out_multiline_execstart_is_still_ignored():
    """The reorder must not turn commented EXAMPLES into failures — the unit carries
    prose about --gateway on purpose, and a guard that reds on it gets deleted."""
    unit = ("# ExecStart=/x/swarph \\\n"
            "#   --gateway http://x\n")
    assert not _has_execstart_gateway_flag(unit)
