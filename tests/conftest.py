"""Suite-wide hermeticity: tests must not inherit the developer's swarph environment.

MEASURED 2026-07-29. Nine tests passed in CI and failed on lab-ovh, on identical code:

    with SWARPH_BRAIN_GATEWAY set (a real cell's config)  ->  9 failed
    with it unset (what CI sees)                          ->  48 passed

`brain-ask` and `memory` branch on `SWARPH_BRAIN_GATEWAY` — gateway mode when present,
direct mode when absent. The tests exercise the direct path and never cleared the
variable, so they silently took the OTHER branch on any box where a cell is configured.

>>> CI COULD NEVER SEE THIS: CI HAS NO SWARPH ENV, SO CI ONLY EVER RUNS ONE OF THE TWO
    BRANCHES, AND GREEN CI SAYS NOTHING ABOUT THE OTHER. <<<
It surfaced only because lab-ovh added SWARPH_BRAIN_GATEWAY to its own settings that
morning — a change OUTSIDE the repo, in a file the repo does not know exists — and
every "suite green" reported after that point was measured under an environment CI does
not have. Not wrong; not the same measurement, while being presented as equivalent.

THE RULE THIS ENFORCES: a test declares the environment it needs. Ambient env is not an
input; it is contamination. Anything that wants gateway mode does
`monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", ...)` and thereby says so in the test body,
where the next reader can see it.

This is the same defect family as the other false instruments found the same day — a
`bash --noprofile --norc` "cold" shell that inherits the parent env, and a `fire-now`
"rehearsal" that only marks a watermark: **the instrument could not reach the condition,
and its output was read as evidence about it.**
"""
import os
import pathlib

import pytest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    """Make SUBPROCESS tests import the tree, exactly like in-process tests do.

    >>> `pythonpath` IN THE PYTEST CONFIG BINDS THIS PROCESS ONLY. A SUBPROCESS
        INHERITS PYTHONPATH FROM THE ENVIRONMENT, WHICH PYTEST DOES NOT SET. <<<

    So tests that shell out to `python -m swarph_cli.main` imported whatever is
    INSTALLED while the assertions around them read the tree. That is a
    cross-boundary comparison, and it is silently vacuous whenever the two agree —
    which is always, right up until the tree moves ahead of the install.

    Found by a version bump: `assert __version__ in result.stdout` compared the
    tree's new version against a subprocess printing the installed one. Before the
    `pythonpath` fix BOTH sides were the install, so it passed while measuring
    nothing. CI cannot see this — CI installs editable, so tree and install are the
    same bytes and the boundary is invisible there BY CONSTRUCTION.

    One test had already patched this at its own call site (console-encoding e2e,
    PYTHONPATH="src"). Doing it here covers the other three and every future one,
    because the next person to shell out will not know the boundary exists.

    Deliberately NOT covered: subprocesses launched with -I/-S, which drop
    PYTHONPATH on purpose to assert isolation. Those must keep failing to see the
    tree — that is precisely what they test.
    """
    prior = os.environ.get("PYTHONPATH", "")
    if _SRC not in prior.split(os.pathsep):
        os.environ["PYTHONPATH"] = _SRC + (os.pathsep + prior if prior else "")


# Every env var that changes a code path. Cleared by default so a configured cell's
# box and a bare CI runner produce the SAME result. Add to this list rather than
# letting a new variable quietly fork behaviour again.
# >>> THIS LIST IS ONLY AS GOOD AS ITS COMPLETENESS, AND NOTHING CHECKED IT. <<<
# It stripped MESH_GATEWAY_TOKEN and NOT MESH_GATEWAY_URL, so four tests asserting
# the default gateway measured the developer's box instead of the code — red on any
# machine with the mesh configured, green on a clean one. The more integrated the
# cell, the less usable its test run (card #524).
#
# test_conftest_hermeticity.py now derives the env vars the SOURCE reads and asserts
# each one is either stripped here or exempted with a reason, so the next variable
# cannot leak in silently.
_SWARPH_ENV = (
    # identity + addressing
    "SWARPH_SELF",
    "SWARPH_NODE",
    "SWARPH_CELL",
    "SWARPH_SESSION_NAME",
    "SWARPH_TMUX_TARGET",
    # gateways
    "MESH_GATEWAY_URL",
    "SWARPH_GATEWAY",
    "SWARPH_HIGHLIGHT_GATEWAY",
    "SWARPH_CODEGRAPH_GATEWAY",
    "SWARPH_BRAIN_GATEWAY",
    "SWARPH_BRAIN_MCP",
    "SWARPH_METAEDGE_URL",
    "GBRAIN_MCP_URL",
    # credentials
    "MESH_GATEWAY_TOKEN",
    "MESH_GATEWAY_COMMANDER_TOKEN",
    "SWARPH_BRAIN_TOKEN",
    "SWARPH_SERVICE_TOKEN",
    "SWARPH_METAEDGE_TOKEN",
    "SWARPH_FACADE_TOKEN",
    "GBRAIN_TOKEN",
    # behaviour switches a test must declare rather than inherit
    "SWARPH_FACADE",
    "SWARPH_FACADE_MODEL",
    "SWARPH_TIMELINE",
    "SWARPH_TIMELINE_DIR",
    "SWARPH_CODEGRAPH_INDEX",
    "SWARPH_WITNESS",
    "MESH_DB_PATH",
    "MESH_CALLER_BINDING_ENFORCE",
    "MESH_REVOCATION_ENFORCE",
    "MESH_GROUP_ADMINS",
)


@pytest.fixture(autouse=True)
def _hermetic_swarph_home(monkeypatch, tmp_path_factory):
    """Point HOME at a tmp dir for every test.

    >>> STRIPPING THE ENVIRONMENT IS NOT ENOUGH: THE SECOND LEAK IS THE FILESYSTEM. <<<
    Token resolution falls back to `Path.home()/.config/swarph/<name>.peer_token`, so on
    a box with a configured cell the suite reads a REAL PEER TOKEN off disk. A test
    asserting `token == "tok"` then fails with the live credential in its diff — which is
    both a false red and a credential in test output.

    Same defect class as the env list above and found in the same pass (card #524): a
    guard that covers one channel while a second channel carries the same state. Ambient
    state is not an input a test may inherit, whichever door it arrives through.
    """
    # NOT inside the test's own `tmp_path`: several tests assert `tmp_path` is empty
    # to prove a dry-run wrote nothing, and a fake home there is an extra entry that
    # fails them. The isolation must not become a fixture that plants evidence in the
    # place under inspection.
    home = tmp_path_factory.mktemp("hermetic-home")
    # >>> HOME ANSWERS TWO QUESTIONS AND ONLY ONE OF THEM IS OURS. <<< It resolves
    # CONFIG (~/.config/swarph/<name>.peer_token — the leak) and PACKAGES (Python's
    # user site, ~/.local/lib/...). Redirecting it for the first also moves the second,
    # so the three subprocess tests that shell out to `python3 -m swarph_cli.main` lost
    # their imports. Pin PYTHONUSERBASE to the REAL home so package lookup stays put
    # while config lookup moves. Split the state; do not special-case the callers.
    real_home = os.environ.get("HOME") or str(pathlib.Path.home())
    monkeypatch.setenv("PYTHONUSERBASE",
                       os.environ.get("PYTHONUSERBASE",
                                      str(pathlib.Path(real_home) / ".local")))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    # cursor-lin, review of PR #264: the SAME DOOR HAS A THIRD HINGE. cell.py honours
    # XDG_CONFIG_HOME (:78) *and* XDG_STATE_HOME (:90) for the state root. Deleting one
    # and not the other is the MESH_GATEWAY_TOKEN/URL pairing again, one layer down.
    # Both are invisible to the completeness lock, whose prefix filter is SWARPH_/MESH_/
    # GBRAIN_ — a guard cannot check the case it is not looking at.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


@pytest.fixture(autouse=True)
def _hermetic_swarph_env(monkeypatch):
    """Strip ambient swarph config from every test.

    autouse so it cannot be forgotten — the failure mode it prevents is invisible to
    the person who forgets, and shows up only on a box configured differently from
    theirs.
    """
    for name in _SWARPH_ENV:
        monkeypatch.delenv(name, raising=False)
