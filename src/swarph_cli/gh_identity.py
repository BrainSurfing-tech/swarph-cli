"""#397 — resolve a cell's GitHub identity from its mesh identity, or REFUSE.

>>> THE DEFECT THIS EXISTS TO END: GITHUB'S GATES KEY ON THE CREDENTIAL, NOT ON
AUTHORSHIP. <<< PR #214 was authored-of-record by `darw007d` purely because that
credential happened to be active at `gh pr create` time — lab wrote every line. So
lab could have approved its own work under a second login and every gate would have
passed green. THE ROUTER IS WHAT MAKES author-of-record MEAN author-in-fact.

Two rules, each earned by a measured failure:

>>> INJECT ``GH_TOKEN`` PER CALL. NEVER RUN ``gh auth switch``. <<< A switch is
GLOBAL TO THE BOX. Measured 2026-08-11: switching to record one review would have
attributed every later `gh` call from another session — including a MERGE — to the
reviewer, until somebody switched back. Same shared-mutable-substrate shape as the
overwritten commit: one setting, several actors, and the actor cannot see its own
scope.

>>> REFUSE ON AN UNMAPPED CELL. NEVER FALL BACK TO THE AMBIENT LOGIN. <<< A router
that silently uses "whoever is logged in" reproduces the identity-substitution class
one layer out, on a surface with no audit trail of its own — which is exactly how
drop-on-meta-edge ran five days as lab-ovh (#360). The request stays internally
consistent, so no server-side control can fire.

THE MAPPING IS OPERATOR DATA, NOT SHIPPED DATA. A wrong mapping is a silent identity
substitution, so this module ships NO defaults and NO built-in table: an absent or
incomplete mapping is a REFUSAL, never a guess.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Where the peer -> github-login table lives. Operator-owned, read-only here.
MAPPING_ENV = "SWARPH_GH_IDENTITY_MAP"
DEFAULT_MAPPING_PATH = Path.home() / ".config" / "swarph" / "gh-identities.json"

#: `gh` at the head of the command, or after leading env assignments / sudo -E etc.
#: Deliberately anchored: a bare `gh` word anywhere (e.g. `grep gh file`) must NOT
#: match, or the router would rewrite unrelated commands.
_GH_INVOCATION = re.compile(r"(?:^|[;&|]\s*|\$\(\s*)(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*gh(?=\s|$)")

#: Already carries an explicit GH_TOKEN — an operator decision; do not double-inject.
_HAS_GH_TOKEN = re.compile(r"(?:^|[;&|]\s*|\$\(\s*)(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*GH_TOKEN=")


class RouterRefusal(Exception):
    """Raised instead of falling back. Carries the operator-facing reason."""


@dataclass(frozen=True)
class Resolution:
    peer: str
    login: str
    source: str          # where the mapping came from, for the audit line


def mapping_path() -> Path:
    override = os.environ.get(MAPPING_ENV, "").strip()
    return Path(override) if override else DEFAULT_MAPPING_PATH


def load_mapping(path: Optional[Path] = None) -> dict:
    """Read the peer -> login table. A MISSING file is not an empty mapping — it is
    an unconfigured router, and the two must not collapse into the same behaviour
    (one is 'nobody is mapped yet', the other is 'this peer specifically is not')."""
    p = path or mapping_path()
    if not p.exists():
        raise RouterRefusal(
            f"no GitHub identity mapping at {p}.\n"
            f"  The router refuses rather than using whichever `gh` account happens\n"
            f"  to be active — an ambient identity is how a cell runs for days as\n"
            f"  another cell (#360) with every request internally consistent.\n"
            f"  Create it:  {{\"<peer-name>\": \"<github-login>\", ...}}\n"
            f"  Or point {MAPPING_ENV} at an existing file."
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RouterRefusal(f"GitHub identity mapping at {p} is unreadable: {exc}")
    if not isinstance(data, dict):
        raise RouterRefusal(f"GitHub identity mapping at {p} must be a JSON object")
    return data


def resolve(peer: Optional[str], path: Optional[Path] = None) -> Resolution:
    """peer -> Resolution, or RouterRefusal. Never returns a fallback."""
    if not peer:
        raise RouterRefusal(
            "SWARPH_SELF is unset, so this cell has no mesh identity to route from.\n"
            "  >>> THE ROUTER WILL NOT GUESS A CELL NAME. <<< Guessing makes a cell\n"
            "  act as whichever identity a default happened to name — measured on\n"
            "  6 of 6 cells (#243) and the direct cause of #360.\n"
            "  Set SWARPH_SELF, or pass GH_TOKEN=... explicitly for a one-off."
        )
    table = load_mapping(path)
    login = table.get(peer)
    if not login:
        p = path or mapping_path()
        raise RouterRefusal(
            f"cell {peer!r} has no GitHub identity in {p}.\n"
            f"  Known cells: {', '.join(sorted(table)) or '(none)'}\n"
            f"  >>> REFUSING RATHER THAN USING THE ACTIVE ACCOUNT. <<< Falling back\n"
            f"  would attribute {peer}'s action to whoever is logged in, and GitHub's\n"
            f"  own gates key on the CREDENTIAL, not on who did the work.\n"
            f"  Add \"{peer}\": \"<github-login>\" to that file, or pass GH_TOKEN=... explicitly."
        )
    if not isinstance(login, str):
        raise RouterRefusal(f"mapping for {peer!r} must be a string, got {type(login).__name__}")
    return Resolution(peer=peer, login=login, source=str(path or mapping_path()))


def targets_gh(command: str) -> bool:
    """True when the command actually invokes `gh`."""
    return bool(_GH_INVOCATION.search(command or ""))


def already_explicit(command: str) -> bool:
    """True when the caller already set GH_TOKEN — an explicit decision (#332: an
    explicit argument is a decision, not a hint). The router must not override it,
    and must not stack a second assignment in front of it."""
    return bool(_HAS_GH_TOKEN.search(command or ""))


def inject(command: str, login: str) -> str:
    """Prefix the per-invocation credential. Leaves global `gh` state untouched.

    `gh auth token --user <login>` reads the stored OAuth token for that account
    WITHOUT switching the active one — the property the whole card rests on.
    """
    return f'GH_TOKEN=$(gh auth token --user {login}) {command}'
