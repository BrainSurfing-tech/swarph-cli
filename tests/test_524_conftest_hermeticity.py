"""Card #524 — the hermeticity guard must be COMPLETE, and nothing checked that it was.

`tests/conftest.py` strips ambient swarph config so a test measures the code rather than
the developer's box. It listed `MESH_GATEWAY_TOKEN` and NOT `MESH_GATEWAY_URL`, so four
tests asserting the default gateway were red on any machine with the mesh configured and
green on a clean one — THE MORE INTEGRATED THE CELL, THE LESS USABLE ITS TEST RUN.

A hand-maintained list is exactly the kind of guard that reads green while missing a case.
This file derives the swarph env vars the SOURCE actually reads and asserts each one is
either stripped or exempted with a reason, so the next variable cannot leak in silently.

The env list was only the first door. The second was the FILESYSTEM: token resolution
falls back to `Path.home()/.config/swarph/<name>.peer_token`, so the suite read a REAL
peer token off disk. Both are covered here.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "swarph_cli"

# Read by the source but deliberately NOT stripped. Each needs a reason: an exemption
# without one is how the original gap looked, too.
_EXEMPT = {
    # binary discovery — a test that needs a fake binary sets it explicitly, and
    # clearing it would not make anything more hermetic.
    "SWARPH_BIN": "path to an executable, not cell config",
    "GBRAIN_BIN": "path to an executable, not cell config",
    # platform/behaviour probes the test harness itself sets to reproduce a condition
    # on a box that does not have it (see feedback: model the environment as data).
    "SWARPH_FORCE_WT": "harness sets it to simulate Windows Terminal",
    "SWARPH_WIN_ACK": "harness sets it to simulate a Windows ack",
    "SWARPH_SPAWN": "marks that we are inside a spawn; tests assert on it directly",
    "SWARPH_TMUX_TARGET": "stripped via _SWARPH_ENV",
}

# Direct reads: os.environ["X"], os.environ.get("X"), getenv("X"). Quote-tolerant —
# a single-quoted read was invisible to the first version.
_ENV_RE = re.compile(r"""(?:os\.environ(?:\.get)?\(|getenv\()['"]([A-Z][A-Z0-9_]*)['"]""")

# >>> COMPUTED NAMES, AND THIS IS THE HOLE cursor-lin FOUND WITH LIVE INSTANCES. <<<
# brain_ask.py:178 passes env_keys=("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN") down to
# tokens.py:284's `os.environ.get(key)` — a COMPUTED name the direct pattern cannot see.
# Both were covered only because someone hand-added them to _SWARPH_ENV, which is
# precisely the failure mode this lock exists to kill: remove them and the lock stays
# green while the suite leaks.
_ENV_KEYS_RE = re.compile(r"env_keys\s*=\s*\(([^)]*)\)", re.S)
_QUOTED_RE = re.compile(r"""['"]([A-Z][A-Z0-9_]*)['"]""")


def _env_vars_read_by_source() -> set:
    """Names the source reads, from BOTH the direct and the computed-name shapes.

    RESIDUAL LIMIT, stated rather than hidden: a name assembled at runtime (f-string,
    concatenation, a list built from config) is invisible to any static scan. This
    closes the two shapes that exist today; it does not make the derivation total, and
    a lock that claimed to be total would be the more dangerous artifact.
    """
    found = set()
    for py in SRC.rglob("*.py"):
        body = py.read_text(encoding="utf-8", errors="replace")
        names = set(_ENV_RE.findall(body))
        for group in _ENV_KEYS_RE.findall(body):
            names.update(_QUOTED_RE.findall(group))
        found.update(n for n in names
                     if n.startswith(("SWARPH_", "MESH_", "GBRAIN_")))
    return found


def test_every_swarph_env_var_is_stripped_or_explicitly_exempted():
    """>>> THE LOCK. <<< MESH_GATEWAY_URL leaked for as long as it did because the list
    was hand-maintained and nothing compared it to reality. Adding a new env var to the
    source now fails here until someone decides, in writing, which side it is on."""
    # NOT `from tests.conftest import ...`: that resolves only because
    # `python -m pytest` puts the CWD on sys.path so `tests` imports as a namespace
    # package. CI runs BARE `pytest`, which does not — ModuleNotFoundError, green
    # where written and red on CI. `import conftest` resolves under both, because
    # pytest's prepend import mode puts the TEST FILE'S OWN DIR on sys.path.
    # (cursor-lin, review of PR #264 — fourth instance of this class today.)
    import conftest as _cf  # noqa: PLC0415 - deliberate late import
    _SWARPH_ENV = _cf._SWARPH_ENV

    read = _env_vars_read_by_source()
    unguarded = sorted(read - set(_SWARPH_ENV) - set(_EXEMPT))
    assert not unguarded, (
        "these env vars are read by the source but neither stripped in conftest's "
        f"_SWARPH_ENV nor exempted in this file's _EXEMPT: {unguarded}. "
        "Decide which — an unlisted var makes the suite measure the developer's box.")


def test_the_exemptions_are_real_and_carry_a_reason():
    """NON-VACUITY. An exemption list that drifts out of date would let the test above
    pass while guarding nothing — the same failure one layer up."""
    read = _env_vars_read_by_source()
    stale = sorted(k for k in _EXEMPT if k not in read)
    assert not stale, (
        f"_EXEMPT names vars the source no longer reads: {stale}. Remove them, or the "
        "exemption list becomes a place for entries nobody re-examines.")
    for name, reason in _EXEMPT.items():
        assert reason.strip(), f"{name} is exempted with no reason"


def test_the_derivation_sees_a_COMPUTED_env_name():
    """>>> THE NON-VACUITY PROOF THAT ACTUALLY BITES. <<< The first version of this file
    proved itself by removing MESH_GATEWAY_URL — which is DERIVATION-VISIBLE, so it only
    ever tested the easy shape. GBRAIN_TOKEN is read solely via env_keys=(...) into a
    computed os.environ.get(key); if the scan cannot see it, removing it from _SWARPH_ENV
    leaves the lock GREEN while the suite leaks. (cursor-lin, review of PR #264.)"""
    read = _env_vars_read_by_source()
    for computed in ("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN"):
        assert computed in read, (
            f"{computed} is read only through env_keys=(...) and the derivation missed "
            "it — the lock would stay green while that var leaked")


def test_both_xdg_roots_are_cleared():
    """cell.py honours XDG_CONFIG_HOME (:78) AND XDG_STATE_HOME (:90). Clearing one and
    not the other is the MESH_GATEWAY_TOKEN/URL pairing one layer down — and both are
    outside this lock's SWARPH_/MESH_/GBRAIN_ prefix filter, so only a named test sees
    it."""
    assert not os.environ.get("XDG_CONFIG_HOME")
    assert not os.environ.get("XDG_STATE_HOME")


def test_the_gateway_url_specifically_is_stripped():
    """The one that actually broke, named so a future refactor of the list cannot drop
    it without a test saying its name."""
    import conftest as _cf  # noqa: PLC0415
    _SWARPH_ENV = _cf._SWARPH_ENV

    assert "MESH_GATEWAY_URL" in _SWARPH_ENV


def test_home_is_redirected_so_no_real_peer_token_is_readable():
    """>>> THE SECOND DOOR. <<< Stripping the environment does not stop
    `Path.home()/.config/swarph/<name>.peer_token` from being read. On a configured box
    the suite picked up a LIVE credential and printed it in an assertion diff."""
    assert Path.home() != Path("/home/ubuntu"), (
        "HOME is not redirected — a test can read the operator's real peer token")
    assert not (Path.home() / ".config" / "swarph").exists(), (
        "the hermetic home already contains swarph config")


def test_package_imports_still_resolve_in_a_subprocess():
    """HOME answers two questions: config lookup AND Python's user site. Redirecting it
    for the first moved the second and broke three subprocess tests. PYTHONUSERBASE is
    pinned to the real home so package lookup stays put — verified BY EXECUTION, because
    an import path is exactly the thing that reads correct and behaves otherwise."""
    r = subprocess.run(
        [sys.executable, "-c", "import swarph_cli, swarph_mesh; print('ok')"],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert r.returncode == 0, (
        f"a subprocess cannot import the packages under the hermetic HOME:\n{r.stderr}")
    assert "ok" in r.stdout


def test_the_fake_home_is_not_inside_the_tests_tmp_path(tmp_path):
    """A dry-run test proves it wrote nothing by asserting `tmp_path` is empty. The
    first version of the HOME fixture created the fake home there and failed five such
    tests — the isolation was planting evidence in the place under inspection."""
    assert list(tmp_path.iterdir()) == [], (
        "something created an entry in tmp_path before the test ran")
    assert Path.home() != tmp_path
    assert tmp_path not in Path.home().parents
