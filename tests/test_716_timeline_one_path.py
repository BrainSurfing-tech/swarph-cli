"""#716: one constant, both verbs, loud local fallback."""
from __future__ import annotations

import os
import sys

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
    # Pin the resolved default, not the override hook (None until pointed).
    resolved = tp.default_timeline_dir()
    assert resolved.name == "swarph-timeline"
    assert ".swarph" not in resolved.parts
    assert tp.default_timeline_file().name == "TIMELINE.md"


def _rehome(monkeypatch, home):
    """Windows Path.home() reads USERPROFILE / HOMEDRIVE+HOMEPATH, never HOME."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    if sys.platform == "win32":
        drive, tail = os.path.splitdrive(os.path.abspath(str(home)))
        monkeypatch.setenv("HOMEDRIVE", drive or "C:")
        monkeypatch.setenv("HOMEPATH", tail or "\\")


def test_775_home_after_import_reaches_both_verbs(tmp_path, monkeypatch):
    """#775 can-fail: module already imported; HOME set now must move both verbs.

    Fails at 25e737b because Path.home() was bound at import. Windows must
    set USERPROFILE (ntpath.expanduser never reads HOME).
    """
    monkeypatch.delenv("SWARPH_TIMELINE", raising=False)
    monkeypatch.delenv("SWARPH_TIMELINE_DIR", raising=False)
    home = tmp_path / "new-home"
    _rehome(monkeypatch, home)
    want = home / "swarph-timeline"
    assert tp.default_timeline_dir() == want
    assert hl._resolve_dir(None) == want
    assert tl._timeline_path() == str(want / "TIMELINE.md")


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
