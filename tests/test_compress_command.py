from pathlib import Path
from swarph_cli.commands.compress import run_compress


def test_unmarked_file_refused_untouched(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# plain\nno marker\n")
    before = f.read_text()
    rc = run_compress([str(f)])                     # dry-run
    assert rc != 0                                  # refused
    assert f.read_text() == before                  # untouched


def test_dryrun_writes_nothing(tmp_path):
    f = tmp_path / "log.md"
    f.write_text('<!-- swarph:compress lever=archival boundary="^## Old" -->\n# Live\nkeep\n## Old\ngone\n')
    before = f.read_text()
    rc = run_compress([str(f)])                      # dry-run, no --apply
    assert rc == 0 and f.read_text() == before       # reported, not written


def test_apply_archival_writes_live_and_archive_and_bak(tmp_path):
    f = tmp_path / "log.md"
    f.write_text('<!-- swarph:compress lever=archival boundary="^## Old" -->\n# Live\nkeep\n## Old\ngone\n')
    rc = run_compress([str(f), "--apply"])
    assert rc == 0
    # cold-tail CONTENT moved out of live (the marker line legitimately stays —
    # the surface remains declared-compressible for future re-archival):
    assert "gone" not in f.read_text()               # cold section body gone from live
    archive = tmp_path / "log.archive.md"
    assert archive.exists()                          # archive written
    assert "## Old" in archive.read_text() and "gone" in archive.read_text()
    assert (tmp_path / "log.md.bak").exists()        # backup left
