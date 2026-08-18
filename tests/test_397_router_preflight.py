"""#397 Task 2 — the router's ORDERING hazard, not its refusal.

Rule 1 is CORRECT to refuse an unmapped cell: falling back to the ambient `gh` account
is how a cell ran five days as another cell (#360) with every request internally
consistent. The hazard is the SEQUENCE. `~/.config/swarph/gh-identities.json` does not
exist on lab-ovh, so installing the hook first makes the router refuse every `gh` call
on the box, discovered one broken command at a time.

>>> AND THE BLAST RADIUS IS WIDER THAN `gh`, WHICH IS drop's MEASUREMENT AND NOT AN
INFERENCE. <<< The invocation regex has 4 known false positives, all quoting. So

    git commit -m "fix; gh routing was wrong"

matches, resolves to an unmapped cell, and THE COMMIT IS REFUSED. An unmapped box does
not merely lose `gh`; it loses every command that mentions one inside a string.

THE TEST THAT CARRIES THE TASK is not "the doctor refuses when the mapping is missing"
— it is that the doctor reports on EVERY CELL ON THE BOX rather than on its caller. The
hook is installed per-box and fires for all of them. lab-ovh runs eleven cells on one
uid, so a caller-scoped check there measures the wrong population and reports green
while ten cells are one install away from total refusal.
"""
from __future__ import annotations

import json

import pytest

from swarph_cli import gh_identity as ghid
from swarph_cli.commands import gh_route


@pytest.fixture
def mapping(tmp_path, monkeypatch):
    """Point the router at a throwaway mapping. Returns a writer."""
    p = tmp_path / "gh-identities.json"
    monkeypatch.setenv(ghid.MAPPING_ENV, str(p))
    return lambda d: p.write_text(json.dumps(d), encoding="utf-8")


def test_doctor_REFUSES_when_the_mapping_is_absent(mapping, capsys):
    """Absent mapping = unconfigured router. Installing now breaks the whole box."""
    rc = gh_route.run_doctor()

    err = capsys.readouterr().err
    assert rc != 0
    assert "no GitHub identity mapping" in err
    assert "DO NOT INSTALL THE HOOK" in err


def test_the_refusal_names_the_WIDER_blast_radius(mapping, capsys):
    """A refusal saying only "gh will not work" understates it. drop measured that a
    quoted `gh` inside an unrelated command matches the invocation regex, so an
    unmapped box loses commits too. The operator must be told the real radius before
    deciding, not after."""
    gh_route.run_doctor()

    err = capsys.readouterr().err
    assert "QUOTES" in err or "quotes" in err


def test_doctor_REPORTS_EVERY_CELL_ON_THE_BOX_not_just_the_caller(
        mapping, monkeypatch, capsys):
    """>>> THE POINT OF THIS TASK. <<< The hook is per-box. A caller-scoped preflight
    passes on a box where every other cell is about to break — and lab-ovh runs eleven
    cells on one uid, so that is the normal case here, not the corner."""
    mapping({"lab-ovh": "orchestrators-hue"})
    monkeypatch.setattr(gh_route, "_cells_on_this_box",
                        lambda: ["cursor-lin", "lab-ovh", "mistral"])

    rc = gh_route.run_doctor()

    out = capsys.readouterr().out
    assert rc != 0, "a box with unmapped cells is NOT safe to install on"
    assert "cursor-lin" in out and "mistral" in out, \
        "the doctor must name the cells that would be refused, not only count them"
    assert "2 of 3" in out


def test_doctor_PASSES_only_when_every_cell_resolves(mapping, monkeypatch, capsys):
    """NON-VACUITY: the doctor must be able to say YES. A check that always refuses is
    a dead control wearing a green name, and it would pass the three tests above."""
    mapping({"lab-ovh": "orchestrators-hue", "cursor-lin": "cursor-lin-gh"})
    monkeypatch.setattr(gh_route, "_cells_on_this_box", lambda: ["cursor-lin", "lab-ovh"])

    rc = gh_route.run_doctor()

    assert rc == 0
    assert "safe to install" in capsys.readouterr().out


def test_NO_CELLS_FOUND_is_CANNOT_EVALUATE_not_a_clean_bill(mapping, monkeypatch, capsys):
    """>>> AN EMPTY CELL LIST IS NOT A CLEAN BOX, IT IS AN UNREAD ONE. <<< Reporting
    "0 of 0 refused, safe to install" would be a green derived from having looked
    nowhere — the exact shape of a check that reads clean because it could not see.
    The #476 detector shipped this same branch for the same reason."""
    mapping({"lab-ovh": "orchestrators-hue"})
    monkeypatch.setattr(gh_route, "_cells_on_this_box", lambda: [])

    rc = gh_route.run_doctor()

    err = capsys.readouterr().err
    assert rc != 0
    assert "CANNOT EVALUATE" in err
    assert "not a clean box, it is an unread one" in err


def test_a_NON_STRING_mapping_value_counts_as_UNMAPPED(mapping, monkeypatch, capsys):
    """`{"lab-ovh": null}` and `{"lab-ovh": {"login": "x"}}` are configuration errors,
    not mappings. Treating a present-but-wrong value as mapped would let the doctor
    bless a box where `resolve()` will still refuse at call time — the preflight and
    the runtime must agree about what counts, or the preflight is decoration."""
    mapping({"lab-ovh": None, "cursor-lin": {"login": "nope"}})
    monkeypatch.setattr(gh_route, "_cells_on_this_box", lambda: ["cursor-lin", "lab-ovh"])

    rc = gh_route.run_doctor()

    assert rc != 0
    assert "2 of 2" in capsys.readouterr().out


def test_cells_on_this_box_survives_a_missing_config_dir(monkeypatch):
    """The hook must never take down a session. An unreadable cells dir yields [] —
    which the doctor then reports as CANNOT EVALUATE rather than as clean."""
    monkeypatch.setattr(gh_route, "cells_dir",
                        lambda: (_ for _ in ()).throw(OSError("gone")))
    assert gh_route._cells_on_this_box() == []
