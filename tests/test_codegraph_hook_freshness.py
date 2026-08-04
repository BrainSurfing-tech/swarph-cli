"""The index-age line must not render a placeholder heuristic as a measurement."""
from __future__ import annotations

from swarph_cli.commands import codegraph_hook


def _render(freshness):
    env = {"results": [{"repo": "r", "path": "p.py", "line": 1,
                        "kind": "function", "name": "f", "callers": 0}],
           "freshness": freshness}
    return codegraph_hook.render("someterm", env)


def test_heuristic_basis_is_flagged_as_unreliable():
    """>>> THIS DEFECT FAILS IN THE RARE DIRECTION: IT MAKES GOOD DATA LOOK BAD.
    <<<

    The gateway's freshness record carries basis=
    'age_heuristic_pending_indexed_commit_193' — the age tracks the last FULL
    rebuild while ~10-minute incremental patches never restamp indexed_at. Shown
    as a bare "index 17.8h old" it told agents a minutes-fresh index was nearly a
    day stale, next to `stale: false` from the same payload.

    An agent that distrusts a correct answer falls back to grep and leaves no
    error behind — the failure is invisible, which is why the hedge must be in
    the rendered line rather than in the payload nobody reads.
    """
    out = _render([{"repo": "r", "index_age_hours": 17.8, "stale": False,
                    "basis": "age_heuristic_pending_indexed_commit_193"}])
    assert "UNRELIABLE" in out
    assert "17.8h old" not in out          # never the bare authoritative claim
    assert "incremental" in out


def test_a_real_measurement_is_still_reported_plainly():
    """The hedge must not swallow a genuine age — pins non-over-reach."""
    out = _render([{"repo": "r", "index_age_hours": 2.0, "stale": False,
                    "basis": "indexed_commit"}])
    assert "index 2.0h old" in out
    assert "UNRELIABLE" not in out


def test_missing_basis_is_treated_as_a_measurement():
    """Absence of `basis` predates the field; do not retro-flag every record."""
    out = _render([{"repo": "r", "index_age_hours": 3.0, "stale": False}])
    assert "index 3.0h old" in out
    assert "UNRELIABLE" not in out
