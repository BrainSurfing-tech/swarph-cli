#!/usr/bin/env python3
"""Label check for Swarph-Card and Swarph-Cell trailers.

Card #1320, after the card author's correction: a trailer is text the writer
chooses, and cells share a GitHub login, so this check cannot bind a commit
to the build-row holder. It only checks the label shape. The binding is the
merger's validated-head rule (card #1318), which compares the PR head to the
head a build row cited when that row was closed_by its holder.

`evaluate` still has the three label cases (no card, cell other than the
injected holder, cell equal to that holder). `--head` refuses a head that
names no card. Neither one reads the live board.
"""
from __future__ import annotations

import re
import subprocess
import sys

CARD = re.compile(r"^Swarph-Card:[ \t]*#(\d+)[ \t]*$", re.MULTILINE)
CELL = re.compile(r"^Swarph-Cell:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)[ \t]*$", re.MULTILINE)


def evaluate(messages, holder_of):
    """-> (ok, reason). Pure. `holder_of(card_id) -> str | None`."""
    cards, cells = [], []
    for message in messages:
        cards.extend(CARD.findall(message or ""))
        cells.extend(CELL.findall(message or ""))
    if not cards:
        return False, "names no card"
    if len(set(cards)) != 1:
        return False, "names more than one card"
    if len(set(cells)) != 1:
        return False, "names no single cell"
    card_id = int(cards[0])
    cell = cells[0]
    holder = holder_of(card_id)
    if not holder:
        return False, f"card #{card_id} has no build-row holder"
    if cell != holder:
        return False, f"trailer cell {cell} is not the build-row holder {holder}"
    return True, f"card #{card_id} trailer cell {cell} is the build-row holder"


def _self_test() -> int:
    holder = lambda card: "friendly-coder" if card == 1320 else None
    cases = [
        (["no trailer here\n"], False),
        (["Swarph-Card: #1320\nSwarph-Cell: someone-else\n"], False),
        (["Swarph-Card: #1320\nSwarph-Cell: friendly-coder\n"], True),
    ]
    failed = False
    for messages, expect in cases:
        ok, reason = evaluate(messages, holder)
        print(("OK  " if ok == expect else "FAIL"), reason, "expected", expect)
        failed = failed or ok != expect
    return 1 if failed else 0


def _head_messages() -> list[str]:
    log = subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        capture_output=True, text=True, check=False,
    )
    if log.returncode != 0:
        print(log.stderr)
        return []
    return [log.stdout]


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return _self_test()
    if "--head" in argv:
        messages = _head_messages()
        if not any(CARD.search(m or "") for m in messages):
            print("FAIL names no card")
            return 1
        if not any(CELL.search(m or "") for m in messages):
            print("FAIL names no cell")
            return 1
        print("OK head names a card and a cell")
        return 0
    print("usage: check_card_trailers.py --self-test|--head")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
