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
_SWARPH_ENV = (
    "SWARPH_BRAIN_GATEWAY",
    "SWARPH_BRAIN_MCP",
    "SWARPH_BRAIN_TOKEN",
    "SWARPH_SELF",
    "SWARPH_NODE",
    "SWARPH_FACADE",
    "SWARPH_TIMELINE",
    "SWARPH_TIMELINE_DIR",
    "GBRAIN_TOKEN",
    "MESH_GATEWAY_TOKEN",
)


@pytest.fixture(autouse=True)
def _hermetic_swarph_env(monkeypatch):
    """Strip ambient swarph config from every test.

    autouse so it cannot be forgotten — the failure mode it prevents is invisible to
    the person who forgets, and shows up only on a box configured differently from
    theirs.
    """
    for name in _SWARPH_ENV:
        monkeypatch.delenv(name, raising=False)
