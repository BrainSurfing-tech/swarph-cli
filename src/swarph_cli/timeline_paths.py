"""Single default for timeline READ and WRITE (card #716).

``swarph highlight`` used to write ``~/.swarph/timeline`` while
``swarph timeline`` read ``~/swarph-timeline/TIMELINE.md``. A highlight
logged offline committed successfully and was invisible to every reader
on the same box. The one named victim is cursor-lin's 2026-09-02 #657
line — the log of the identity fix fell into the hole.

Both verbs import *functions* from this module, not a bound Path.
A test that points ``DEFAULT_TIMELINE_DIR`` at a temp dir must see
BOTH verbs follow. If only one moves, this is not the single definition.

Env: ``SWARPH_TIMELINE`` is the file. ``SWARPH_TIMELINE_DIR`` is the
repo dir (same default, one layer up). Two names, one constant.
"""
from __future__ import annotations

import os
from pathlib import Path

# The mesh log. Not ~/.swarph/timeline — that is the hole #716 names.
DEFAULT_TIMELINE_DIR = Path.home() / "swarph-timeline"
TIMELINE_FILENAME = "TIMELINE.md"

ENV_TIMELINE = "SWARPH_TIMELINE"
ENV_TIMELINE_DIR = "SWARPH_TIMELINE_DIR"


def default_timeline_dir() -> Path:
    return Path(DEFAULT_TIMELINE_DIR)


def default_timeline_file() -> Path:
    return default_timeline_dir() / TIMELINE_FILENAME


def timeline_file(*, file_arg: str | None = None,
                  dir_arg: str | None = None) -> Path:
    """Resolve the TIMELINE.md path. Call-time, so the constant is live."""
    if file_arg:
        return Path(os.path.expanduser(file_arg))
    env_file = (os.environ.get(ENV_TIMELINE) or "").strip()
    if env_file:
        return Path(os.path.expanduser(env_file))
    if dir_arg:
        return Path(os.path.expanduser(dir_arg)) / TIMELINE_FILENAME
    env_dir = (os.environ.get(ENV_TIMELINE_DIR) or "").strip()
    if env_dir:
        return Path(os.path.expanduser(env_dir)) / TIMELINE_FILENAME
    return default_timeline_file()


def timeline_dir(*, file_arg: str | None = None,
                 dir_arg: str | None = None) -> Path:
    """Repo dir for ``swarph highlight``. Parent of ``timeline_file``."""
    return timeline_file(file_arg=file_arg, dir_arg=dir_arg).parent
