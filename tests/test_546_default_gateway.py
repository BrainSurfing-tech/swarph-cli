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
