"""Who an alert escalates to: config, never a peer name baked into the wheel (card #1685).

Five modules used to route to a hardcoded peer: stall_alert, the triage router,
duplicate_session, rowclear, wake_watchdog. A fresh install has no such peer, so
each now reads its target from the environment or operator.json, and with
neither set it names no target, routes nowhere, and prints ONE line saying so.

    escalation target     $SWARPH_ESCALATION_TARGET       operator.json "escalation_target"
    alternate peer        $SWARPH_ESCALATION_ALTERNATE    operator.json "escalation_alternate"
    rowclear opt-in cells $SWARPH_ROWCLEAR_OPT_IN         operator.json "rowclear_opt_in"
    triage owner map      $SWARPH_TRIAGE_OWNERS_FILE      operator.json "triage_owners_file"

The environment wins over operator.json. operator.json is
``~/.config/swarph/operator.json``, read leniently: a missing or unreadable file,
or a key of the wrong type, is "not set", never an exception into a delivery loop.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from swarph_cli.console_safe import print_safe

ENV_TARGET = "SWARPH_ESCALATION_TARGET"
KEY_TARGET = "escalation_target"
ENV_ALTERNATE = "SWARPH_ESCALATION_ALTERNATE"
KEY_ALTERNATE = "escalation_alternate"
ENV_ROWCLEAR_OPT_IN = "SWARPH_ROWCLEAR_OPT_IN"
KEY_ROWCLEAR_OPT_IN = "rowclear_opt_in"
ENV_TRIAGE_OWNERS = "SWARPH_TRIAGE_OWNERS_FILE"
KEY_TRIAGE_OWNERS = "triage_owners_file"

_SAID: set[tuple[str, str]] = set()


def setting(env: str, key: str) -> str | None:
    """The env var, else the operator.json key (a list is comma-joined), else None."""
    value = os.environ.get(env, "").strip()
    if value:
        return value
    try:
        path = Path.home() / ".config" / "swarph" / "operator.json"
        value = json.loads(path.read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, AttributeError, RuntimeError):
        return None
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def target() -> str | None:
    return setting(ENV_TARGET, KEY_TARGET)


def alternate() -> str | None:
    return setting(ENV_ALTERNATE, KEY_ALTERNATE)


def rowclear_opt_in() -> frozenset[str]:
    raw = setting(ENV_ROWCLEAR_OPT_IN, KEY_ROWCLEAR_OPT_IN) or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def say_none(who: str, what: str, env: str = ENV_TARGET, key: str = KEY_TARGET) -> None:
    """The one line a module prints when it has no target. Once per process per
    (who, what): a monitor poll must not repeat it every 30 seconds."""
    if (who, what) in _SAID:
        return
    _SAID.add((who, what))
    print_safe(
        f"[{who}] no {what} configured, so it routes nowhere. "
        f"Set ${env} or operator.json \"{key}\".",
        file=sys.stderr, flush=True)
