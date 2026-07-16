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
