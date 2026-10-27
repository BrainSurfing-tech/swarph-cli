"""Commit trailers a cell adds when it commits for a card.

Card #1320. `Swarph-Card: #<id>` and `Swarph-Cell: <cell>`. The prepare-commit-msg
hook calls `append_commit_trailers`. A message that already carries a trailer
is left alone, so a second run does not stack a second claim.
"""
from __future__ import annotations

import re

_CARD = re.compile(r"^Swarph-Card:[ \t]*#\d+[ \t]*$", re.MULTILINE)
_CELL = re.compile(r"^Swarph-Cell:[ \t]*\S+[ \t]*$", re.MULTILINE)
_CELL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def append_commit_trailers(message: str, card: str, cell: str) -> str:
    card_id = str(card).strip().lstrip("#")
    if not card_id.isdigit():
        raise ValueError(f"card must be a number, got {card!r}")
    cell_name = cell.strip()
    if _CELL_NAME.fullmatch(cell_name) is None:
        raise ValueError(f"cell must be a peer name, got {cell!r}")
    extra = []
    if _CARD.search(message) is None:
        extra.append(f"Swarph-Card: #{card_id}")
    if _CELL.search(message) is None:
        extra.append(f"Swarph-Cell: {cell_name}")
    if not extra:
        return message if message.endswith("\n") else message + "\n"
    body = message.rstrip("\n")
    return body + "\n\n" + "\n".join(extra) + "\n"
