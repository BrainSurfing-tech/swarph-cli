import datetime as dt
from swarph_cli.commands import timeline

SAMPLE = (
    "# swarph timeline\n"
    "- 2026-07-10T21:02Z · **lab-ovh** · built tunnel-watch · → [[feedback_x]]\n"
    "- 2026-07-13T04:24Z · **lab-ovh** · credential isolation note [[reference_swairm_repo]]\n"
    "- 2026-07-15T08:51Z · **gridiron** · reaper operational · → [[feedback_y]]\n"
)


def _write(tmp_path):
    p = tmp_path / "TIMELINE.md"
    p.write_text(SAMPLE, encoding="utf-8")
    return str(p)


def test_load_entries_parses_ts_cell_links(tmp_path):
    entries = timeline.load_entries(_write(tmp_path))
    assert len(entries) == 3
    e = entries[0]
    assert e.ts == dt.datetime(2026, 7, 10, 21, 2, tzinfo=dt.timezone.utc)
    assert e.cell == "lab-ovh"
    assert e.links == ["feedback_x"]
    # inline [[link]] (not just the → pointer) is captured
    assert entries[1].links == ["reference_swairm_repo"]


def test_range_since_around(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    assert timeline.run_timeline(["range", "2026-07-12", "2026-07-14"]) == 0
    out = capsys.readouterr().out
    assert "2026-07-13T04:24Z" in out and "2026-07-10" not in out and "2026-07-15" not in out
    assert timeline.run_timeline(["since", "2026-07-14"]) == 0
    assert "2026-07-15T08:51Z" in capsys.readouterr().out
    assert timeline.run_timeline(["around", "2026-07-13", "--window", "1d"]) == 0
    around = capsys.readouterr().out
    assert "2026-07-13T04:24Z" in around and "2026-07-15" not in around


def test_missing_file_is_fail_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_TIMELINE", str(tmp_path / "nope.md"))
    rc = timeline.run_timeline(["since", "2026-07-01"])
    assert rc == 1                       # non-zero, not a traceback
    assert "timeline" in capsys.readouterr().err.lower()


def test_range_full_iso_end_is_exact_bound_no_eod(tmp_path, monkeypatch, capsys):
    # entry exactly at the full-ISO end must be included; one minute later excluded.
    sample = (
        "# swarph timeline\n"
        "- 2026-07-12T00:00Z · **lab-ovh** · start of window\n"
        "- 2026-07-14T10:00Z · **lab-ovh** · exactly at end · [[a]]\n"
        "- 2026-07-14T10:01Z · **lab-ovh** · one minute past end · [[b]]\n"
    )
    p = tmp_path / "TIMELINE.md"
    p.write_text(sample, encoding="utf-8")
    monkeypatch.setenv("SWARPH_TIMELINE", str(p))
    assert timeline.run_timeline(["range", "2026-07-12", "2026-07-14T10:00Z"]) == 0
    out = capsys.readouterr().out
    assert "2026-07-14T10:00Z" in out
    assert "2026-07-14T10:01Z" not in out


def test_range_bare_date_end_still_gets_end_of_day(tmp_path, monkeypatch, capsys):
    # bare-date end must still include an entry late on that day (unchanged behavior).
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    assert timeline.run_timeline(["range", "2026-07-14", "2026-07-15"]) == 0
    out = capsys.readouterr().out
    assert "2026-07-15T08:51Z" in out


def test_around_full_iso_zero_window_is_exact_center(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    assert timeline.run_timeline(["around", "2026-07-13T04:24Z", "--window", "0h"]) == 0
    out = capsys.readouterr().out
    assert "2026-07-13T04:24Z" in out
    assert "2026-07-15T08:51Z" not in out
    assert "2026-07-10T21:02Z" not in out


def test_json_emits_okf_node_edges(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    assert timeline.run_timeline(["since", "2026-07-14", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    rec = payload[0]
    assert rec["node"] == {"id": "2026-07-15T08:51Z", "hemisphere": "time",
                           "ts": "2026-07-15T08:51Z"}
    assert rec["edges"] == [{"type": "link", "to": "feedback_y",
                             "to_hemisphere": "knowledge", "direction": "out"}]
    assert rec["cell"] == "gridiron"
    assert rec["text"] == "reaper operational · → [[feedback_y]]"


def test_timeline_registered_in_dispatch():
    from swarph_cli import main as m
    assert m._VERB_HANDLERS["timeline"] == "swarph_cli.commands.timeline.run_timeline"


def test_timeline_navigate_failsafe(tmp_path, monkeypatch):
    from swarph_cli.commands import mcp_server
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    got = mcp_server._timeline_navigate("since", date="2026-07-14")
    assert got and got[0]["node"]["id"] == "2026-07-15T08:51Z"
    # fail-safe: unknown op / bad input NEVER raises
    assert mcp_server._timeline_navigate("bogus") == []
    monkeypatch.setenv("SWARPH_TIMELINE", str(tmp_path / "nope.md"))
    assert mcp_server._timeline_navigate("since", date="2026-07-01") == []


def test_human_output_link_appears_once(tmp_path, monkeypatch, capsys):
    # Regression test: verify human output shows memory pointer exactly once (not duplicated)
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    assert timeline.run_timeline(["since", "2026-07-14"]) == 0
    out = capsys.readouterr().out
    # Entry has embedded pointer "· → [[feedback_y]]" in text; should appear exactly once
    assert out.count("[[feedback_y]]") == 1


# ── #135(b): accept every form the shared timeline actually contains ─────────
# MEASURED on the live TIMELINE.md 2026-07-27, by shape:
#     274  NNNN-NN-NNTNN:NNZ      minute precision — the only form this parsed
#      65  NNNN-NN-NN             DATE ONLY — the OMEGA genesis entries
#       1  NNNN-NN-NNTNN:NN:NNZ   seconds
# The strict parser dropped 66 of 340 entries — the mesh's ENTIRE PRE-HISTORY, 2026-03
# to 2026-04 — and `swarph timeline since 2026-03-01` returned nothing and exited 0.
# Found only because the gateway's GET /highlights reported parse_skipped=67 on its
# first live call.
import pytest


@pytest.mark.parametrize("raw,expect", [
    ("2026-07-27T21:07Z",         "2026-07-27T21:07:00+00:00"),   # what we write
    ("2026-07-27T21:07:33Z",      "2026-07-27T21:07:33+00:00"),   # seconds
    ("2026-07-27T21:07:33.500Z",  "2026-07-27T21:07:33.500000+00:00"),
    ("2026-03-19",                "2026-03-19T00:00:00+00:00"),   # GENESIS: date only
    ("2026-07-27 21:07Z",         "2026-07-27T21:07:00+00:00"),   # space separator
    ("2026-07-27T21:07:00+00:00", "2026-07-27T21:07:00+00:00"),   # explicit offset
])
def test_every_timestamp_form_in_the_real_timeline_parses(raw, expect):
    got = timeline._parse_entry_ts(raw)
    assert got is not None, f"{raw!r} dropped — this is how 66 entries vanished"
    assert got.isoformat() == expect, (raw, got.isoformat())


@pytest.mark.parametrize("raw", ["garbage-timestamp", "", "2026-13-45", "not a date"])
def test_unparseable_timestamps_are_still_refused(raw):
    """Tolerance must not become guessing. A guessed timestamp files an entry under the
    wrong day, which is worse than admitting the line was not understood."""
    assert timeline._parse_entry_ts(raw) is None


def test_date_only_entries_are_reachable_by_a_since_query(tmp_path):
    """The end-to-end property: the genesis entries must actually come back.

    `since 2026-03-01` returning nothing WITH EXIT 0 was the defect — indistinguishable
    from a quiet period that in fact contains the mesh's founding events.
    """
    p = tmp_path / "TIMELINE.md"
    p.write_text(
        "- 2026-03-19 · **OMEGA · Genesis** · MCP-architecture orchestrator seeded\n"
        "- 2026-07-27T21:07Z · **droplet** · a recent entry\n", encoding="utf-8")
    entries = timeline.load_entries(str(p))
    assert len(entries) == 2, entries
    assert entries[0].cell == "OMEGA · Genesis"
    assert entries[0].ts == dt.datetime(2026, 3, 19, tzinfo=dt.timezone.utc)
