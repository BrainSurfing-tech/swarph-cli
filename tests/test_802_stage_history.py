"""#802 — `cards history` renders stage_history; the default `cards show`
does not. Gateway already selects the column; this is the missing read.

A card whose every entry looks the same cannot prove the gate stamp is
shown. The fixture has a pre-gate row and a stamped row."""
from swarph_cli.commands import board


def _card(**kw):
    c = {
        "id": 777,
        "stage": "done",
        "stage_history": [
            {"stage": "proposed", "by": "lab-ovh",
             "at": "2026-09-08T12:32:18.943473+00:00"},
            {"stage": "build", "by": "lab-ovh",
             "at": "2026-09-08T12:40:31.405576+00:00",
             "gate": {"mode": "warn", "flip_at": None, "menu_version": 1,
                      "labels": ["channels", "wake"], "missing": [],
                      "not_passed": []}},
            {"stage": "done", "by": "lab-ovh",
             "at": "2026-09-09T07:19:43.544449+00:00",
             "gate": {"mode": "warn", "flip_at": "2026-09-22T12:00:00Z",
                      "menu_version": 1, "labels": ["channels", "wake"],
                      "missing": [], "not_passed": [], "grandfathered": 0}},
        ],
    }
    c.update(kw)
    return c


def test_802_history_prints_at_by_and_gate_only_where_stamped():
    out = board._format_stage_history(_card())
    lines = out.splitlines()
    assert lines[0] == "card #777 stage=done 3 transition(s)"
    assert lines[1] == (
        "  proposed  by=lab-ovh  at=2026-09-08T12:32:18.943473+00:00")
    assert "gate=" not in lines[1], "pre-gate row must not invent a stamp"
    assert "gate=warn" in lines[2] and "menu_v=1" in lines[2]
    assert "missing=none" in lines[2] and "flip_at=-" in lines[2]
    assert "labels=channels, wake" in lines[2]
    assert "gate=warn" in lines[3] and "flip_at=2026-09-22T12:00:00Z" in lines[3]
    assert "grandfathered=0" in lines[3]
    assert "{" not in out, "must not dump the raw JSON blob"


def test_802_absent_key_is_not_an_empty_list():
    out = board._format_stage_history({"id": 1, "stage": "spec"})
    assert "stage_history ABSENT" in out
    assert "0 transition" not in out


def test_802_empty_list_is_said():
    out = board._format_stage_history({"id": 1, "stage": "spec", "stage_history": []})
    assert "0 transition(s)" in out
    assert "(empty)" in out


def test_802_strips_terminal_escapes():
    out = board._format_stage_history(_card(stage_history=[{
        "stage": "build\x1b[31m", "by": "evil\x1b]0;x\x07", "at": "t",
        "gate": {"mode": "warn\x1b", "missing": ["x\x1b"], "labels": []},
    }]))
    assert "\x1b" not in out and "\x07" not in out
