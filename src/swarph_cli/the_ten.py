"""THE_TEN at session start, read from a commit. Card #1243.

The block is the current top ten (label and one line each), the cycle
stamp, and the commit sha it was read from. The bytes come from
``git show <sha>:docs/THE_TEN.md``. A working tree is never opened, so a
checkout sitting on an old branch cannot supply a stale copy. The sha is
printed so a copy that is old is visible.

Membranes:

- Claude Code. ``swarph hook-output`` appends the block to SessionStart
  ``additionalContext``. ``SWARPH_SPAWN=1`` still appends it. The starter
  prompt stays on the spawn flag, so the starter is not doubled.
- Codex. ``codex_hook_output`` calls the same function, so a Codex
  process that actually runs the hook gets the same block. Card #537
  measured that the install is accepted and the hook does not fire.
  This module does not claim a Codex session sees it.
- cursor-agent and antigravity. They honour SessionStart when the
  installed hook calls ``hook-output``. That path gets this block.
- gemini, grok, opencode, muse, vibe. Out of scope. Gemini would need
  ``GEMINI.md``, grok and opencode a wake preamble, and muse has no
  prompt slot on resume. Writing the ten into those files would be a
  checkout copy, which this card forbids. A later preamble calls
  ``session_block`` instead of copying a tree.

A missing repo, a missing ``origin/main``, or a git failure returns an
empty string. Session start must not stop because the ten could not be
read.

``law=L<n>`` and ``law=B<n>`` are citation tokens parsed from close
evidence. A missing token means the closer did not cite a law. It does
not mean a law was not used. The citation rate is not #327's catch-rate.
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ITEM = re.compile(r"(?m)^\d+\.\s+\*\*([LB]\d+)\*\*\s+·\s+\*\*(.+?)\*\*")
_STAMP = re.compile(r"(?m)^cycle\s+(\d+)\s+\(([^)]+)\):\s*(.+?)\s*$")
_SOURCE = re.compile(r"Source:\s*docs/prereg/RESULT-cycle-(\d+)\.md")
_DATE = re.compile(r"the commander,\s*(\d{4}-\d{2}-\d{2})")
_CITE = re.compile(r"(?<![A-Za-z0-9-])law=([LB]\d+)(?![A-Za-z0-9])")

# Cycle 4 is not opened here. Both gates are the spec-review's (msg 77850).
_CYCLE_4_OPEN = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
_CYCLE_4_SLIP = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)


def discover_repo() -> Path | None:
    """Where the commit is read from.

    ``SWARPH_PROVEN_REPO`` set to a path wins. Set to empty, discovery is
    off, which is how tests keep a host checkout out of the hook. Unset,
    ``~/proven`` is used when it is a git repo. The path is an object
    store. The worktree file is not the source.
    """
    raw = os.environ.get("SWARPH_PROVEN_REPO")
    if raw is not None:
        raw = raw.strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if _is_git(path) else None
    home = Path.home() / "proven"
    return home if _is_git(home) else None


def _is_git(path: Path) -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            check=True,
            capture_output=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def read_commit(repo: Path, rev: str = "origin/main") -> tuple[str, str]:
    """``(sha, docs/THE_TEN.md text)`` at ``rev``.

    ``rev`` is resolved to a commit, then the blob is shown as
    ``<sha>:docs/THE_TEN.md``. That does not read the worktree file.
    """
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", f"{rev}^{{commit}}"],
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    ).strip()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError(f"not a full commit sha: {sha!r}")
    text = subprocess.check_output(
        ["git", "-C", str(repo), "show", f"{sha}:docs/THE_TEN.md"],
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    return sha, text


def top_ten(text: str) -> list[tuple[str, str]]:
    """Numbered ``N. **L4** · **title**`` rows, in order, at most ten.

    Bench rows are bullets, not numbered, so they are not included.
    """
    found = [(m.group(1), m.group(2).strip()) for m in _ITEM.finditer(text or "")]
    return found[:10]


def stamp_of(text: str) -> str:
    """The cycle stamp printed next to the sha.

    The last ``cycle N (date): ...`` line wins, because a later week
    appends one. With no such line, the stamp is the document's own
    ``Source: RESULT-cycle-N`` and the commander date in that header.
    """
    stamps = list(_STAMP.finditer(text or ""))
    if stamps:
        last = stamps[-1]
        return f"cycle {last.group(1)} ({last.group(2)}): {last.group(3).strip()}"
    source = _SOURCE.search(text or "")
    dated = _DATE.search(text or "")
    if source and dated:
        return f"cycle {source.group(1)} ({dated.group(1)})"
    if source:
        return f"cycle {source.group(1)}"
    return "cycle (no stamp)"


def render_block(sha: str, text: str) -> str:
    """The session block: sha, stamp, then one line per law in the ten."""
    lines = [f"THE_TEN {sha}", stamp_of(text)]
    items = top_ten(text)
    if not items:
        lines.append("THE_TEN: no top-ten block parsed")
    else:
        lines.extend(f"{label} · {title}" for label, title in items)
    return "\n".join(lines)


def session_block(repo: Path | None = None, rev: str = "origin/main") -> str:
    """The block for this session, or ``""`` when it cannot be read.

    Never raises. A failure is an empty injection, not a failed session.
    """
    try:
        where = repo if repo is not None else discover_repo()
        if where is None:
            return ""
        sha, text = read_commit(where, rev)
        return render_block(sha, text)
    except Exception:
        return ""


def law_citations(evidence: str) -> list[str]:
    """Strict ``law=L<n>`` / ``law=B<n>`` tokens, first-seen order.

    An empty list means the evidence did not cite a law.
    """
    seen: list[str] = []
    for match in _CITE.finditer(evidence or ""):
        token = match.group(1)
        if token not in seen:
            seen.append(token)
    return seen


def stamp_line(cycle: int, when: str, change: str | None) -> str:
    """The line a weekly PR adds to ``THE_TEN.md``.

    ``change`` empty or None is ``no change``. A quiet week still stamps.
    """
    body = "no change" if not change or change == "no change" else change
    return f"cycle {cycle} ({when}): {body}"


def cycle_bounds(moment: datetime) -> dict[str, datetime]:
    """Monday 00:00Z, Sunday 20:00Z, Sunday 23:00Z of the week ``moment`` is in.

    ``moment`` must be aware. The score time is when the score script is
    committed. The close time is the tally. Lab creates those reminders.
    This function does not.
    """
    if moment.tzinfo is None:
        raise ValueError("cycle bounds are UTC; a naive datetime is refused")
    moment = moment.astimezone(timezone.utc)
    monday = (moment - timedelta(days=moment.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    sunday = monday + timedelta(days=6)
    return {
        "open": monday,
        "score": sunday.replace(hour=20),
        "close": sunday.replace(hour=23),
    }


def cycle_4_open(*, commander_go: bool, prereg_on_main: bool) -> datetime:
    """When cycle 4 opens. This does not open it.

    Both the commander's go and PREREG-0009 on main are required for
    2026-10-26T00:00Z. Otherwise the open moves to 2026-11-02T00:00Z.
    """
    if commander_go and prereg_on_main:
        return _CYCLE_4_OPEN
    return _CYCLE_4_SLIP


def cycle_4_close(*, commander_go: bool, prereg_on_main: bool) -> datetime:
    """Sunday 23:00Z six days after :func:`cycle_4_open`."""
    opened = cycle_4_open(commander_go=commander_go, prereg_on_main=prereg_on_main)
    return opened + timedelta(days=6, hours=23)
