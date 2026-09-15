"""Operator surface (`swarph-me`) — human-facing mesh handle.

This package is intentionally isolated from the cell CLI identity path.
It must not import any symbol that reads the cell-environment identity
variable. A regression test enforces that invariant so the 2026-08-26
attribution forgery cannot return via proximity to cell helpers.
"""

from .cli import main

__all__ = ["main"]
