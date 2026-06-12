from swarph_cli.compress.marker import parse_marker, Marker


def test_no_marker_returns_none():
    assert parse_marker("# A file\njust text\n") is None


def test_shorthand_marker_parsed():
    m = parse_marker('<!-- swarph:compress lever=shorthand pointer="](*.md)" floor=0.45 -->\n- [x](a.md)\n')
    assert m == Marker(lever="shorthand", pointer="](*.md)", floor=0.45, boundary=None)


def test_archival_marker_parsed():
    m = parse_marker('<!-- swarph:compress lever=archival boundary="^## Session" -->\n')
    assert m.lever == "archival" and m.boundary == "^## Session"


def test_malformed_marker_returns_none():
    # unknown lever → fail safe (treated as no marker)
    assert parse_marker('<!-- swarph:compress lever=bogus -->\n') is None
