"""Pack loader + the normative JSON-Schema validation (spec §1).

A pack is one JSON file, three required parts: ``system`` (skill/context),
``tasks[].prompt`` (tests), ``tasks[].expected`` (ground truth). Hand-rolled
schema check (no ``jsonschema`` dependency — swarph-cli keeps its core paths
dependency-light, per the existing ``codegraph``/``compress`` pattern) that
enforces exactly the normative schema from the spec plus the constraints
listed beneath it:

- ``id`` unique within a pack
- ``expected`` shape matches ``type`` (numeric->number, categorical->string,
  ranking->array, text->string)
- ``system`` SHOULD be present (warning, not an error, if absent)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TASK_TYPES = ("numeric", "categorical", "ranking", "text")


class PackError(Exception):
    """Raised by :func:`load_pack` when the JSON itself can't be read/parsed."""


def load_pack(path: str | Path) -> dict:
    """Read + JSON-parse a pack file. Raises :class:`PackError` on I/O or
    JSON-decode failure (schema validity is a separate step — see
    :func:`validate_schema` — so callers can report *why* a pack is invalid
    rather than crash)."""
    p = Path(path)
    try:
        raw = p.read_text()
    except OSError as exc:
        raise PackError(f"cannot read pack {p}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PackError(f"pack {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError(f"pack {p} must be a JSON object, got {type(data).__name__}")
    return data


def _expected_matches_type(expected: Any, ttype: str) -> bool:
    if ttype == "numeric":
        return isinstance(expected, (int, float)) and not isinstance(expected, bool)
    if ttype == "categorical":
        return isinstance(expected, str)
    if ttype == "ranking":
        return isinstance(expected, list) and all(isinstance(x, (str, int, float)) for x in expected)
    if ttype == "text":
        return isinstance(expected, str)
    return False


def validate_schema(pack: dict) -> tuple[list[str], list[str]]:
    """Validate ``pack`` against the normative JSON-Schema (spec §1) plus the
    constraints beneath it. -> ``(errors, warnings)``. Non-empty ``errors``
    means the pack is INVALID (schema violation, dup id, type/expected
    mismatch); ``warnings`` are advisory (e.g. missing ``system``) and don't
    fail the pack on their own."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(pack, dict):
        return ([f"pack must be a JSON object, got {type(pack).__name__}"], warnings)

    theme = pack.get("theme")
    if not isinstance(theme, str) or not theme.strip():
        errors.append("'theme' is required and must be a non-empty string")

    if "version" in pack and not isinstance(pack["version"], str):
        errors.append("'version' must be a string when present")

    if "system" not in pack or not str(pack.get("system") or "").strip():
        warnings.append(
            "pack has no 'system' — a pack without a system tests raw model priors, "
            "not applying a real operating context (spec §5d: system is the DOMINANT "
            "variable and must mirror real deployment)"
        )
    elif not isinstance(pack.get("system"), str):
        errors.append("'system' must be a string when present")

    if "description" in pack and not isinstance(pack["description"], str):
        errors.append("'description' must be a string when present")

    tasks = pack.get("tasks")
    if not isinstance(tasks, list) or len(tasks) < 1:
        errors.append("'tasks' is required and must be a non-empty array")
        return (errors, warnings)

    seen_ids: set[str] = set()
    for i, task in enumerate(tasks):
        where = f"tasks[{i}]"
        if not isinstance(task, dict):
            errors.append(f"{where} must be an object")
            continue

        tid = task.get("id")
        if not isinstance(tid, str) or not tid.strip():
            errors.append(f"{where}.id is required and must be a non-empty string")
        elif tid in seen_ids:
            errors.append(f"duplicate task id {tid!r}")
        else:
            seen_ids.add(tid)

        prompt = task.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append(f"{where}.prompt is required and must be a non-empty string")

        ttype = task.get("type")
        if ttype not in TASK_TYPES:
            errors.append(f"{where}.type must be one of {TASK_TYPES}, got {ttype!r}")
            continue  # can't check expected-shape against an unknown type

        if "expected" not in task:
            errors.append(f"{where}.expected is required")
        elif not _expected_matches_type(task["expected"], ttype):
            errors.append(
                f"{where}.expected does not match type={ttype!r} "
                f"(got {type(task['expected']).__name__})"
            )

        if "weight" in task:
            w = task["weight"]
            if not isinstance(w, (int, float)) or isinstance(w, bool) or w <= 0:
                errors.append(f"{where}.weight must be a number > 0, got {w!r}")

        if "meta" in task and not isinstance(task["meta"], dict):
            errors.append(f"{where}.meta must be an object when present")

    return (errors, warnings)


def slugify_theme(theme: str) -> str:
    """``theme`` -> a filesystem-safe pack slug: lowercase, non-alnum runs ->
    ``_``, trimmed. Drives the ``packs/<theme>.json`` naming convention
    (spec §6 / ``bench add``) — the pack's OWN header names the file, no
    separate name is ever asked for on the command line."""
    s = re.sub(r"[^a-z0-9]+", "_", theme.strip().lower()).strip("_")
    return s or "pack"
