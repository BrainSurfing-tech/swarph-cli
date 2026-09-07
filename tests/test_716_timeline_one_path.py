"""#716: one constant, both verbs, loud local fallback."""
from __future__ import annotations

from swarph_cli.commands import highlight as hl
from swarph_cli.commands import timeline as tl
from swarph_cli import timeline_paths as tp


def test_716_one_constant_both_verbs_follow(tmp_path, monkeypatch):
    """Can-fail (a): point the constant at a temp dir; BOTH verbs follow.

    If only one moves, the constant is not the single definition.
    """
    monkeypatch.delenv("SWARPH_TIMELINE", raising=False)
    monkeypatch.delenv("SWARPH_TIMELINE_DIR", raising=False)
    only = tmp_path / "only-place"
    monkeypatch.setattr(tp, "DEFAULT_TIMELINE_DIR", only)
    assert hl._resolve_dir(None) == only
    assert tl._timeline_path() == str(only / "TIMELINE.md")


def test_716_constant_is_the_shared_repo_not_the_hole():
    # DEFAULT_TIMELINE_DIR is bound at import; hermetic $HOME must not
    # be compared to it. The name of the hole vs the shared repo is the pin.
    assert tp.DEFAULT_TIMELINE_DIR.name == "swarph-timeline"
    assert ".swarph" not in tp.DEFAULT_TIMELINE_DIR.parts
    assert tp.default_timeline_file().name == "TIMELINE.md"


def _unwrap_help(s: str) -> str:
    """argparse may hyphen-wrap ``~/swarph-timeline`` across a newline."""
    return s.replace("\n", "")


def test_716_both_helps_name_the_same_default(capsys):
    try:
        hl.run_highlight(["--help"])
    except SystemExit as e:
        assert e.code == 0
    hl_help = _unwrap_help(capsys.readouterr().out)
    try:
        tl.run_timeline(["--help"])
    except SystemExit as e:
        assert e.code == 0
    tl_help = _unwrap_help(capsys.readouterr().out)
    assert "~/swarph-timeline" in hl_help
    assert "~/swarph-timeline" in tl_help
    assert "~/.swarph/timeline" not in hl_help
    assert "~/.swarph/timeline" not in tl_help
