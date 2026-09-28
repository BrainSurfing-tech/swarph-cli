"""Which cell am I? One answer for the whole CLI (card #402).

Before this module the question was answered at more than twenty sites, each with
its own copy of the order and its own ending: `lab-ovh` (brain_ask), `lab`
(hooks, until #360), the hostname, a state directory, a sentinel, or a refusal.
A rule repeated at N sites is a rule applied at N-1: #360 fixed four defaults,
then a fifth, sixth and seventh turned up.

THE ORDER, everywhere: an explicit flag, then $SWARPH_SELF, then $SWARPH_CELL.
SWARPH_SELF is the canonical variable (#360). SWARPH_CELL is still accepted
where a site accepted it before, because MCP hosts and the watchdog unit set it,
but it never outranks SWARPH_SELF: psmux leaks SWARPH_CELL from the spawning
environment (#538), so CELL-first posts under another cell's name.

THERE IS NO DEFAULT PEER NAME. When nothing declares the identity:
  - `require` RAISES IdentityNotDeclared (the #872 rule: refuse, do not guess);
  - `or_sentinel` returns SENTINEL, for code that must never fail a turn (a
    liveness hook). The sentinel is not a peer name, so nothing is written or
    sent as somebody else.

Sites that deliberately read fewer variables pass `env=`. The mesh verbs read
SWARPH_SELF only, because reading an inbox consumes it and an ambient SWARPH_CELL
must not be able to act as another cell there. That restriction is now visible
at the call site instead of implied by which line a function happens to read.
"""
from __future__ import annotations

import os
from typing import Optional, Sequence, Tuple

ENV: Tuple[str, ...] = ("SWARPH_SELF", "SWARPH_CELL")
SELF_ONLY: Tuple[str, ...] = ("SWARPH_SELF",)
# SWARPH_NODE: a legacy alias brain_ask has always read, after SWARPH_SELF (brain_ask
# reads SELF_ONLY + NODE_ALIAS because its identity picks a credential). Named here
# so no importable module spells an identity variable. (cell_probe and cell_selfcheck
# also read SWARPH_SELF directly: they must run as bare files where this package is
# not importable, and neither falls back to a peer name.)
NODE_ALIAS: Tuple[str, ...] = ("SWARPH_NODE",)
SENTINEL = "unidentified-cell"


class IdentityNotDeclared(RuntimeError):
    """No flag and no identity variable named this cell. There is no default."""


def declared(explicit: Optional[str] = None, *,
             env: Sequence[str] = ENV) -> Tuple[Optional[str], str]:
    """(name, source), or (None, "undeclared"). Never guesses.

    A blank flag (None, "", whitespace) means the flag was not given, so the
    environment decides (#872 pins this for --caller-cell). Among the
    environment variables the FIRST non-empty value decides, exactly as the
    `a or b` chains this replaces did: an empty string falls through to the
    next variable, anything else stops the search. The deciding value is
    stripped; if nothing is left (a whitespace-only SWARPH_SELF), the identity
    is UNDECLARED. It does not fall through, so a blank SWARPH_SELF can never
    hand the identity to a leaked SWARPH_CELL (#538).
    """
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip(), "flag"
    for var in env:
        raw = os.environ.get(var) or ""
        if raw:
            name = raw.strip()
            return (name, f"${var}") if name else (None, "undeclared")
    return None, "undeclared"


def require(explicit: Optional[str] = None, *, verb: str,
            env: Sequence[str] = ENV) -> str:
    """The declared identity, or IdentityNotDeclared naming how to declare it."""
    name, _ = declared(explicit, env=env)
    if name is None:
        raise IdentityNotDeclared(
            f"swarph {verb}: no cell identity declared, and there is no default. "
            f"Set {' or '.join('$' + v for v in env)}, or pass the verb's identity flag."
        )
    return name


def or_sentinel(explicit: Optional[str] = None, *,
                env: Sequence[str] = ENV) -> str:
    """The declared identity, or SENTINEL. For code that must never raise."""
    name, _ = declared(explicit, env=env)
    return name if name is not None else SENTINEL
