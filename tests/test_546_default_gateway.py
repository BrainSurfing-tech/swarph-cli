"""Every default-gateway constant must agree, and none may be localhost.

WHY THIS TEST EXISTS (2026-08-21): the mesh-gateway binds HOST=100.107.222.72 only.
localhost has never been bound. Four separate constants defaulted to
http://localhost:8788 across three modules, and the cost was invisible both times it
bit:

  * swarph-card-corpus-export dialed localhost and failed 792 CONSECUTIVE times over
    8 days (2026-08-12 -> 2026-08-21) with nobody told. Board cards stopped reaching
    the gbrain recall corpus for the whole period.
  * science-claude's monitor WORKED hand-started (the hand-start passed --gateway) and
    went DEAF the moment it was moved to a systemd unit, because the unit passes no
    --gateway and fell through to this constant. Supervising the cell is what broke it,
    and systemd reported `active` throughout.

>>> THE DEFECT WAS NEVER ONE CONSTANT. IT WAS FOUR CONSTANTS THAT NOBODY COMPARED. <<<
Fixing one and missing three is precisely how this shape survives, so the guard asserts
AGREEMENT, not correctness of any single value.
"""
import json
import pytest
import os
import subprocess
import sys

# (module path, attribute name) for every default-gateway constant in this package.
# ADD A ROW HERE when a new one appears — that is the whole point of this test.
SITES = [
    ("swarph_cli.commands.mesh", "_DEFAULT_GATEWAY"),
    ("swarph_cli.commands.cell_selfcheck", "_RESOLVER_DEFAULT_GATEWAY"),
    ("swarph_cli.commands.watchdog", "_DEFAULT_GATEWAY_URL"),
    ("swarph_cli.commands.init", "_DEFAULT_GATEWAY"),
]


def _read(env_value):
    """Read every site in a CLEAN SUBPROCESS.

    NOT importlib.reload: reloading these modules re-runs module-level setup and
    explodes on partially-initialised globals (_RESOLVER). A subprocess is the only
    way to observe the constant under a controlled environment without importing
    the package twice into one interpreter.
    """
    env = dict(os.environ)
    env.pop("MESH_GATEWAY_URL", None)
    if env_value is not None:
        env["MESH_GATEWAY_URL"] = env_value
    prog = (
        "import importlib, json; "
        "print(json.dumps({f'{m}.{a}': getattr(importlib.import_module(m), a) "
        "for m, a in " + repr(SITES) + "}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", prog], env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"probe failed: {out.stderr[-800:]}"
    return json.loads(out.stdout)


def test_no_default_gateway_is_localhost():
    """>>> THE FAILING CASE THAT MATTERS: localhost is never bound by the gateway. <<<"""
    vals = _read(None)
    bad = {k: v for k, v in vals.items() if "localhost" in v or "127.0.0.1" in v}
    assert not bad, (
        f"default gateway points at a loopback address that the mesh-gateway never "
        f"binds: {bad} — this fails as a bare 'Connection refused' with no cause named"
    )


def test_all_default_gateways_agree():
    """Disagreement is the bug, independently of which value is right."""
    vals = _read(None)
    assert len(set(vals.values())) == 1, (
        f"default-gateway constants disagree, so a fix applied to one leaves the "
        f"others broken: {vals}"
    )


def test_env_override_wins_at_every_site():
    """MESH_GATEWAY_URL is the escape hatch for anyone outside this mesh."""
    override = "http://example.invalid:9999"
    vals = _read(override)
    wrong = {k: v for k, v in vals.items() if v != override}
    assert not wrong, f"MESH_GATEWAY_URL did not override these sites: {wrong}"


def test_the_guard_can_fail():
    """>>> A GUARD THAT CANNOT FAIL IS DECORATION. <<< Prove the localhost assertion
    actually fires, so this file is not a green that proves nothing."""
    vals = _read("http://localhost:8788")
    assert all("localhost" in v for v in vals.values())
    bad = {k: v for k, v in vals.items() if "localhost" in v}
    assert bad, "the detector found nothing in a deliberately-bad configuration"


# ---------------------------------------------------------------------------
# SOURCE-LEVEL SWEEP — added after cursor-lin's review of PR #276.
#
# >>> THE SITES LIST ABOVE CANNOT SEE EVERYTHING, AND THAT IS NOT FIXABLE BY
# ADDING ROWS. <<< Four more sites carried the identical defect and were
# invisible to both the original grep AND to an attribute-based guard:
#
#     commands/daemon.py:88        argparse default  (the daemon itself)
#     commands/onboard.py:114      argparse default
#     commands/ratify.py:60        argparse default
#     commands/codegraph_hook.py:40  module constant
#
# Three are ARGPARSE DEFAULTS — not module attributes, so no amount of
# extending SITES would ever reach them. And the original search missed all
# four because their default lives INSIDE os.environ.get(...) rather than
# after a bare '=', so `grep 'GATEWAY.*= *"http'` could not match.
#
# Had this merged with SITES alone, the package would have disagreed with
# itself 4-versus-4 UNDER A GUARD COVERING HALF OF IT — a green that proves
# nothing, which is the exact shape this whole card is about. cursor-lin's
# words: "post-merge the package disagrees with itself 4v4 under a guard
# covering half".
#
# A SOURCE SWEEP CATCHES ANY SHAPE, including ones nobody has invented yet.
# ---------------------------------------------------------------------------

import pathlib


def _src_root():
    here = pathlib.Path(__file__).resolve().parent.parent
    return here / "src" / "swarph_cli"


def test_no_loopback_gateway_default_anywhere_in_src():
    """No file under src/ may name a loopback gateway as a DEFAULT, in any form."""
    offenders = []
    for path in _src_root().rglob("*.py"):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            # A COMMENT MAY NAME THE DEFECT IT DESCRIBES. cell_selfcheck.py:204 explains
            # this exact bug and must not be flagged as committing it.
            if stripped.startswith("#"):
                continue
            if "8788" not in line:
                continue
            if "localhost:8788" in line or "127.0.0.1:8788" in line:
                offenders.append(f"{path.relative_to(_src_root())}:{i}: {line.strip()[:90]}")
    assert not offenders, (
        "a loopback gateway default survives in src/ — the mesh-gateway binds the "
        "tailnet IP only, so this fails as a bare 'Connection refused' with no cause "
        "named:\n  " + "\n  ".join(offenders)
    )


def test_the_source_sweep_can_fail(tmp_path, monkeypatch):
    """>>> PROVE THE SWEEP FIRES. <<< A detector that has only ever seen a clean
    tree is indistinguishable from one that matches nothing."""
    fake = tmp_path / "swarph_cli"
    fake.mkdir()
    (fake / "bad.py").write_text(
        'GW = os.environ.get("MESH_GATEWAY_URL", "http://localhost:8788")\n'
    )
    monkeypatch.setattr(f"{__name__}._src_root", lambda: fake)
    with pytest.raises(AssertionError):
        test_no_loopback_gateway_default_anywhere_in_src()
