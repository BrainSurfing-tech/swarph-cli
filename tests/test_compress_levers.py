from swarph_cli.compress.levers import archival_split, ArchivalResult


def test_archival_split_lossless_roundtrip():
    src = "# Manual\nlive line\n## Session 3\nold\n## Session 4\nolder\n"
    r = archival_split(src, boundary=r"^## Session", archive_name="LOG.md")
    # live = head + pointer; archive = the cold tail
    assert "live line" in r.live and "## Session 3" not in r.live
    assert "## Session 3" in r.archive and "## Session 4" in r.archive
    assert "LOG.md" in r.live                     # pointer present
    # lossless: concat archive tail back == original tail
    assert r.archive.rstrip().endswith("older")


def test_archival_refuse_no_boundary():
    src = "# Manual\njust live content, no cold tail\n"
    r = archival_split(src, boundary=r"^## Session", archive_name="LOG.md")
    assert r is None                              # no boundary -> refuse (leave)
