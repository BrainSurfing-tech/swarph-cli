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
