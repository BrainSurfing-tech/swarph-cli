"""Build a default caller-convention slug for one-shot CLI invocations.

Per swarph_shared.caller_convention the slug must match:

    ^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$

So we sanitize the OS username (which can have hyphens, capitals,
digits-first, etc.) into a conformant fragment and prepend a fixed
``cli.oneshot.`` prefix.
"""

from __future__ import annotations

import getpass
import os
import re


_NON_SLUG_CHARS = re.compile(r"[^a-z0-9_]+")


def _sanitize_username(name: str) -> str:
    """Turn an arbitrary username into a caller-convention fragment.

    - Lowercase
    - Replace any non-alnum/underscore with underscore
    - Collapse runs of underscores
    - Ensure leading char is a letter (prepend ``u_`` if not)
    - Fall back to ``unknown`` if empty
    """
    s = name.lower()
    s = _NON_SLUG_CHARS.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        return "unknown"
    if not s[0].isalpha():
        s = "u_" + s
    return s


def default_caller() -> str:
    """Return ``cli.oneshot.<sanitized-user>`` for the current OS user."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    return f"cli.oneshot.{_sanitize_username(user)}"
