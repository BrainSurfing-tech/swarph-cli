"""State paths for capture artifacts, mirroring cell.session_state_path's XDG layout.

Lives beside sessions/ under $XDG_STATE_HOME/swarph (or ~/.local/state/swarph):
  sessions/<role>.session-id   (existing R5 pin store)
  lineage/<role>.jsonl         (append-only provenance log)
  captures/<role>.json         (revival-kit manifest + live-pin + reserved HEAD)
"""
from __future__ import annotations

import os
from pathlib import Path


def _state_root() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "state"


def _swarph_state() -> Path:
    return _state_root() / "swarph"


def lineage_dir() -> Path:
    return _swarph_state() / "lineage"


def captures_dir() -> Path:
    return _swarph_state() / "captures"


def lineage_path(role: str) -> Path:
    return lineage_dir() / f"{role}.jsonl"


def manifest_path(role: str) -> Path:
    return captures_dir() / f"{role}.json"
