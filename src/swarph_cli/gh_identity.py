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

#: Wrappers that take a command as their argument, so `gh` is argv[1+] rather than
#: argv[0]. grok-researcher enumerated these against the first version, which
#: matched none of them: `timeout 40 gh pr view 249` is the one it had itself run
#: twice while reviewing these PRs, which is as real-world as a corpus gets.
_WRAPPERS = (
    r"env(?:\s+-\w+)*|sudo(?:\s+-\w+(?:\s+\S+)?)*|command(?:\s+-\w+)*|exec|nohup"
    r"|time|nice(?:\s+-n\s*-?\d+)?|timeout\s+\S+|stdbuf(?:\s+-\S+)*|rlwrap|xargs(?:\s+-\S+(?:\s+\S+)?)*"
)

#: Positions from which a command can start: string start, after a separator, inside
#: a substitution, after a block/subshell opener, or after then/do/else.
#: >>> MULTILINE IS LOAD-BEARING. <<< Without it `^` is string-start only, so
#: `cd /tmp\ngh pr list` missed entirely — and multi-line Bash is the DOMINANT shape
#: in this harness. drop-on-meta-edge measured 11 false negatives in 16 realistic
#: shapes against the first version; that was the biggest single class.
# >>> `\bif\b` AND FRIENDS WERE MISSING, AND drop MEASURED THE COST: 1 OF 11 FALSE
# NEGATIVES SURVIVED THE FIX, AND IT WAS `if gh pr view 249; then`. <<< The list had
# then/do/else — the words that FOLLOW a condition — and not the words that OPEN one.
# A command in the CONDITION of an if/while/until is exactly as much a `gh` call as
# one in its body, and it is a shape people write constantly.
#
# Found by re-running drop's own failing corpus rather than the PR's seven cases:
# "0/7 false positives is measured against your seven cases, not mine, and the claim
# as written is wider than its evidence." The token list was, too.
_START = (r"(?:^|[;&|]|\$\(|`|\(|\{"
          r"|\bthen\b|\bdo\b|\belse\b|\bif\b|\bwhile\b|\buntil\b|\belif\b"
          r"|&&|\|\|)")
_LEAD = r"[ \t]*"                       # leading whitespace/tab — also missed before
_ENVPFX = r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*[ \t]+)*"
_WRAPPFX = rf"(?:(?:{_WRAPPERS})[ \t]+)*"
#: `gh`, `./gh`, or any absolute path ending in /gh.
_GH_WORD = r"(?:\.{0,2}/|(?:/[\w.-]+)*/)?gh"

_GH_INVOCATION = re.compile(
    rf"{_START}{_LEAD}{_ENVPFX}{_WRAPPFX}{_GH_WORD}(?=[ \t]|$)", re.MULTILINE)

#: Already carries an explicit GH_TOKEN — an operator decision; do not double-inject.
#: >>> `export GH_TOKEN=...` MUST COUNT. <<< The first version matched only the
#: `VAR=x cmd` prefix form, so `export GH_TOKEN=abc; gh pr view` had its gh half
#: detected and its token half missed — and the router stacked its own credential in
#: front of the one the operator deliberately chose (#332: an explicit argument is a
#: decision, not a hint). grok-researcher named this as the live stacking case.
_HAS_GH_TOKEN = re.compile(
    rf"{_START}{_LEAD}(?:(?:export|declare|local|typeset|readonly)[ \t]+)?"
    rf"{_WRAPPFX}(?:[A-Za-z_][A-Za-z0-9_]*=\S*[ \t]+)*GH_TOKEN[ \t]*=",
    re.MULTILINE)


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
    """Export the credential for this invocation. Leaves global `gh` state untouched.

    `gh auth token --user <login>` reads the stored OAuth token for that account
    WITHOUT switching the active one — the property the whole card rests on.

    >>> `export VAR=x; cmd`, NOT `VAR=x cmd`. THE FIRST VERSION USED THE PREFIX FORM
    AND WAS INERT FOR EVERY COMPOSED COMMAND. <<< In bash a `VAR=x cmd` prefix scopes
    VAR to `cmd` ALONE, so the token landed on `cd`, on `echo`, on whatever came
    first — never on `gh`. Found by drop-on-meta-edge, verified here:

        bash -c 'GH_TOKEN=FAKE cd /tmp && echo "[${GH_TOKEN}]"'   ->  []
        bash -c 'export GH_TOKEN=OK; cd /tmp && echo "[${GH_TOKEN}]"'  ->  [OK]

    `cd /tmp && gh pr list` was a DOCUMENTED SUPPORTED EXAMPLE and it did nothing.

    >>> AND IT WAS THE WORST AVAILABLE FAILURE MODE, because it REPORTED SUCCESS:
    rc=0 plus a systemMessage naming the resolved identity, while the gh process ran
    under the ambient account. The router's own output became the evidence that it
    had worked. <<<

    The suite could not see it: the test asserted the STRING SHAPE (startswith /
    endswith) and nothing executed. That is the proxy detector pointed at our own
    tests — same class as a 409 handler that is confident, well-tested, and about
    the wrong thing. test_the_injected_token_REACHES_a_composed_command now runs a
    real shell.

    The export is scoped to this Bash tool call's own shell, which is exactly the
    per-invocation property wanted — it is not `gh auth switch`, and it touches no
    global state and no other session.
    """
    return f'export GH_TOKEN=$(gh auth token --user {login}); {command}'
