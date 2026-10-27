#!/usr/bin/env python3
"""prepare-commit-msg hook: add Swarph-Card and Swarph-Cell when the env names them.

Card #1320. Install as .git/hooks/prepare-commit-msg (or chain it). The hook
reads SWARPH_CARD and SWARPH_CELL, falling back to SWARPH_SELF for the cell.
With neither set, the message is unchanged: a commit that is not for a card
is not given a fake one.
"""
from __future__ import annotations

import os
import sys

from swarph_cli.commit_trailers import append_commit_trailers


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: prepare-commit-msg-swarph.py COMMIT_MSG_FILE", file=sys.stderr)
        return 2
    card = os.environ.get("SWARPH_CARD", "").strip()
    cell = (os.environ.get("SWARPH_CELL") or os.environ.get("SWARPH_SELF") or "").strip()
    if not card or not cell:
        return 0
    path = argv[1]
    with open(path, encoding="utf-8") as fh:
        message = fh.read()
    updated = append_commit_trailers(message, card, cell)
    if updated != message:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
