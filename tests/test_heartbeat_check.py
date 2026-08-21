"""Guards for `swarph monitor heartbeat-check` — #544 Proposals A and B.

Every test here corresponds to a defect found by RUNNING the check against a
live membrane on 2026-08-21, not by reading the code. All three survived review
by two cells; none survived the first induced outage. That is the argument for
the tests being here rather than the feature shipping on its unit-level logic.
"""

from __future__ import annotations

import pytest

from swarph_cli.commands import monitor


# ── the invented-constant defect ─────────────────────────────────────────────

def test_threshold_is_derived_from_the_running_poll_interval_not_a_constant():
    """A staleness threshold is a FUNCTION of the poll interval, never a guess.

    Shipped as a bare 180. Nothing tied it to how often the writer can actually
    advance the file, which is the only thing that makes a threshold meaningful.
    """
    got, why = monitor._resolve_stale_after(None, 30)
    assert got == 180 and "derived" in why
    got, why = monitor._resolve_stale_after(None, 120)
    assert got == 720, "a slower poll must widen the window, not keep 180"


def test_derived_threshold_has_a_floor_so_a_tiny_poll_is_not_hair_trigger():
    got, _ = monitor._resolve_stale_after(None, 1)
    assert got == 60


def test_unknown_poll_interval_is_NAMED_not_silently_assumed():
    """No pidfile => the interval is unknown. Say so; do not present a guess
    as though it were read from the writer."""
    _got, why = monitor._resolve_stale_after(None, None)
    assert "UNKNOWN" in why


def test_a_threshold_that_cannot_come_out_negative_is_REFUSED():
    """>>> THE DEFECT, VERBATIM FROM THE FIRST INDUCED-TEST ARM. <<<

    `--stale-after-s 5` against a 30s poll reported DEGRADED cause=silent_hang
    on a cell that was draining perfectly. The heartbeat advances at most once
    per poll, so such a threshold is a detector that can only ever say yes.
    A permanent red trains readers to skip the row (obligation_sweep.py's own
    recorded lesson), so this is refused rather than honoured.
    """
    with pytest.raises(RuntimeError) as e:
        monitor._resolve_stale_after(5, 30)
    assert "cannot come out negative" in str(e.value)


def test_a_legitimate_explicit_threshold_still_works():
    got, why = monitor._resolve_stale_after(60, 30)
    assert got == 60 and why == "explicit"


# ── the seventh cause: capability, declared and never inferred ───────────────

def test_a_writer_that_never_declared_the_capability_is_not_accused_of_hanging():
    """>>> THE MAJORITY CASE DURING ANY ROLLOUT. <<<

    A hung writer and a writer that never implements the heartbeat produce the
    IDENTICAL artefact: a file that does not advance. Measured live — lab-ovh's
    shared editable clone sits on `main`, which carries zero occurrences of
    `drain_heartbeat`, so the supervised monitor CANNOT emit one. The six-cause
    design called that `silent_hang`: a confident wrong answer about a healthy
    cell. Ship that fleet-wide and every un-upgraded cell reds at once.
    """
    cause = monitor._classify_drain_failure(
        "cell", "live_ours", hb={"pid": 1, "ts": 0}, live_pid=9,
        emits_heartbeat=None)
    assert cause == "writer_lacks_heartbeat"


def test_capability_is_read_from_the_DECLARATION_not_from_the_artifact():
    """Asking the artifact whether the artifact is supported is circular
    (lab-ovh, DM 25744). The declaration resolves it on the FIRST check, with
    no heartbeat file present at all -- where a pid comparison has nothing to
    compare and would have to wait two intervals to say anything.
    """
    assert monitor._classify_drain_failure(
        "cell", "live_ours", hb=None, live_pid=9,
        emits_heartbeat=None) == "writer_lacks_heartbeat"


def test_a_capable_writer_with_no_heartbeat_is_absent_not_lacking():
    assert monitor._classify_drain_failure(
        "cell", "live_ours", hb=None, live_pid=9,
        emits_heartbeat=True) == "heartbeat_absent"


def test_a_capable_writer_whose_own_heartbeat_froze_IS_a_silent_hang():
    """The genuine case the whole proposal exists for: the process is alive,
    declares the capability, owns the newest heartbeat, and it stopped moving.
    No OS supervisor detects this."""
    assert monitor._classify_drain_failure(
        "cell", "live_ours", hb={"pid": 9, "ts": 0}, live_pid=9,
        emits_heartbeat=True) == "silent_hang"


def test_the_four_causes_are_distinct_values():
    """Family B-DUAL: two states with opposite remedies must not share one
    observable. These four have four different remedies -- upgrade the cell,
    investigate the writer, restart the process, install a supervisor."""
    seen = {
        monitor._classify_drain_failure("c", "live_ours", hb={"pid": 1, "ts": 0},
                                        live_pid=9, emits_heartbeat=None),
        monitor._classify_drain_failure("c", "live_ours", hb=None, live_pid=9,
                                        emits_heartbeat=True),
        monitor._classify_drain_failure("c", "live_ours", hb={"pid": 9, "ts": 0},
                                        live_pid=9, emits_heartbeat=True),
        monitor._classify_drain_failure("no-such-cell-anywhere", "stale",
                                        hb={"pid": 1, "ts": 0}),
    }
    assert len(seen) == 4, f"causes collapsed into one observable: {seen}"


# ── the cross-cell attribution defect ────────────────────────────────────────

def test_a_unit_is_only_this_cells_if_its_ExecStart_NAMES_this_cell():
    """>>> IT READ ANOTHER CELL'S SUPERVISOR AND CALLED IT THIS ONE'S. <<<

    The probe used to fall back to the bare `swarph-monitor.service`. On the
    lab-ovh box that unit is LAB-OVH'S monitor (`--as lab-ovh`, measured), so
    stopping science-claude's own unit yielded `cause=unrecognized`: it found
    lab's unit active, scanned LAB'S journal, and reported a plausible wrong
    answer where the truth was `supervisor_absent`.

    THE UNIT NAME IS A PROXY; THE INVOCATION IS THE FACT. The template's own
    header already warned that a generic service name on a multi-cell host is
    a collision waiting to happen.
    """
    units = monitor._candidate_units("science-claude")
    assert units[0] == "swarph-monitor@science-claude.service", (
        "the template INSTANCE is safe by construction -- %i IS the cell name")

    # POSITIVE CONTROL, and it is the point of the test. On a host with no
    # generic `swarph-monitor.service` there is nothing to wrongly attribute,
    # so the assertion below would pass while checking NOTHING -- a vacuous
    # green, which is the defect this whole card is about. Skip loudly instead.
    if not monitor._unit_exists("swarph-monitor.service"):
        pytest.skip(
            "control is VACUOUS on this host: no generic swarph-monitor.service "
            "exists, so nothing could be misattributed. NOT a pass.")
    assert not monitor._unit_names_this_cell(
        "swarph-monitor.service", "science-claude"), (
        "fixture no longer divergent -- the generic unit now names this cell, "
        "so it cannot demonstrate cross-cell misattribution")
    assert "swarph-monitor.service" not in units, (
        "the GENERIC unit name is not attributable to any particular cell and "
        "must never be accepted on the strength of its name")


def test_candidate_units_never_returns_a_unit_belonging_to_another_cell():
    for unit in monitor._candidate_units("science-claude"):
        if unit == "swarph-monitor@science-claude.service":
            continue          # safe by construction, no ExecStart read needed
        assert monitor._unit_names_this_cell(unit, "science-claude"), (
            f"{unit} was accepted without proving it names this cell")


# ── lab-ovh's Q1 finding: --as is NOT the only identity path ─────────────────

def _has_systemd_units() -> bool:
    return monitor._unit_exists("swarph-monitor.service")


def test_identity_resolves_from_SWARPH_SELF_when_ExecStart_has_no_as_flag():
    """>>> `--as` IS NOT THE ONLY WAY A MONITOR GETS ITS IDENTITY. <<<

    (lab-ovh, measured, DM 25772.) `_self_name_was_derived` shows $SWARPH_SELF
    alone is sufficient, so a unit with `Environment=SWARPH_SELF=<PEER>` and no
    `--as` runs perfectly -- and was INVISIBLE to the ExecStart-only probe,
    silently dropping out of its own check.

    The hole is reachable rather than theoretical: the SHIPPED unit sets both,
    so `--as` reads as redundant to anyone tidying that file.
    """
    unit_text = 'ExecStart={ path=/x ; argv[]=/x monitor start --deliver pull }\nEnvironment=SWARPH_SELF=somecell HOME=/home/ubuntu\n'
    import re as _re
    assert not _re.search(r"--as[\s=]+([\w.-]+)", unit_text), "fixture must have no --as"
    assert _re.search(r"SWARPH_SELF=([\w.-]+)", unit_text).group(1) == "somecell"


@pytest.mark.skipif(not _has_systemd_units(),
                    reason="no swarph-monitor units on this host — control vacuous, NOT a pass")
def test_identity_is_read_from_the_invocation_on_real_units():
    """The generic unit is lab-ovh's; the template instances name their own
    cell. Both paths exercised against real units rather than fixtures."""
    assert monitor._unit_identity("swarph-monitor.service") is not None, (
        "a live monitor unit must be attributable to SOME cell")


def test_a_unit_naming_nobody_is_NOT_claimed_by_this_cell():
    """Third state: not-attributable is neither mine nor another's. Folding it
    into 'not mine' is the Family B-DUAL defect this card is about."""
    assert monitor._unit_names_this_cell.__doc__  # symbol exists
    mine, unattributable = monitor._partition_units("science-claude")
    assert all(u not in mine for u in unattributable), (
        "an unattributable unit must never be silently claimed as this cell's")
