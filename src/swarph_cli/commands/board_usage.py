"""Card #1277 — usage read off the closing cell's own transcript.

Numbers come from fields the harness wrote. A missing field stays
"unknown". Nothing here estimates a token count from duration or from
a character length.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_TOKEN_FIELDS = (
    ("tokens_in", ("input_tokens",)),
    ("tokens_out", ("output_tokens",)),
    ("cache_read", ("cache_read_input_tokens",)),
    ("cache_write", ("cache_creation_input_tokens",)),
    ("thinking", ("thinking_tokens",)),
)


def _parse_time(value):
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _thinking(usage: dict):
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and "thinking_tokens" in details:
        return details.get("thinking_tokens")
    return usage.get("thinking_tokens")


def _field(usage: dict, name: str):
    if name == "thinking":
        return _thinking(usage)
    key = {"tokens_in": "input_tokens", "tokens_out": "output_tokens",
           "cache_read": "cache_read_input_tokens",
           "cache_write": "cache_creation_input_tokens"}[name]
    return usage.get(key)


def sum_transcript(lines, taken_at, now) -> dict:
    """Sum usage objects whose timestamp is inside [taken_at, now].

    A usage object that omits a field makes that field unknown for the
    whole window. Messages outside the window are not part of the sum.
    """
    start = _parse_time(taken_at)
    end = _parse_time(now)
    totals = {name: 0 for name, _ in _TOKEN_FIELDS}
    seen = {name: False for name, _ in _TOKEN_FIELDS}
    missing = set()
    models = []
    counted = 0
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        when = _parse_time(rec.get("timestamp"))
        if start is None or end is None or when is None or when < start or when > end:
            continue
        message = rec.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        counted += 1
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model.strip():
            models.append(model.strip())
        for name, _keys in _TOKEN_FIELDS:
            value = _field(usage, name)
            if isinstance(value, bool) or not isinstance(value, int):
                missing.add(name)
                continue
            seen[name] = True
            totals[name] += value
    out = {}
    for name, _keys in _TOKEN_FIELDS:
        if name in missing or not seen[name] or counted == 0:
            out[name] = "unknown"
        else:
            out[name] = totals[name]
    distinct = set(models)
    out["model"] = models[0] if len(distinct) == 1 else "unknown"
    return out


def _transcript_lines(env) -> list[str]:
    explicit = env.get("SWARPH_CLAUDE_TRANSCRIPT")
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path.read_text(encoding="utf-8").splitlines()
        if path.is_dir():
            lines = []
            for item in sorted(path.glob("*.jsonl")):
                lines.extend(item.read_text(encoding="utf-8").splitlines())
            return lines
        return []
    if not (env.get("CLAUDECODE") or env.get("CLAUDE_CODE_ENTRYPOINT")):
        return []
    cwd = env.get("PWD") or os.getcwd()
    slug = "-" + cwd.strip("/").replace("/", "-")
    home = env.get("HOME") or str(Path.home())
    root = Path(home) / ".claude" / "projects" / slug
    if not root.is_dir():
        return []
    session = env.get("CLAUDE_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID")
    files = sorted(root.glob("*.jsonl"))
    if session:
        files = [item for item in files if item.stem == session]
    lines = []
    for item in files:
        try:
            lines.extend(item.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return lines


def _lane_model(env, taken_at, now) -> str:
    raw = env.get("SWARPH_LANE_USAGE") or ""
    path = env.get("SWARPH_LANE_USAGE_FILE")
    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            raw = ""
    start = _parse_time(taken_at)
    end = _parse_time(now)
    chosen = None
    for line in raw.splitlines():
        text = line.strip()
        if text.startswith("CURSOR_LANE_USAGE"):
            text = text[len("CURSOR_LANE_USAGE"):].strip()
        if not text.startswith("{"):
            continue
        try:
            rec = json.loads(text)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        when = _parse_time(rec.get("ts"))
        if start is None or end is None or when is None or when < start or when > end:
            continue
        model = rec.get("model")
        if isinstance(model, str) and model.strip() and len(model.strip()) <= 120:
            chosen = model.strip()
    return chosen or "unknown"


def _blank(agent: str) -> dict:
    return {
        "agent": agent or "unknown",
        "model": "unknown",
        "tokens_in": "unknown",
        "tokens_out": "unknown",
        "cache_read": "unknown",
        "cache_write": "unknown",
        "thinking": "unknown",
        "wall_s": "unknown",
    }


def read_close_usage(agent=None, taken_at=None, now=None, env=None) -> dict:
    """Usage for one close. wall_s stays unknown: the gateway measures it."""
    env = os.environ if env is None else env
    who = agent or env.get("SWARPH_SELF") or "unknown"
    if taken_at is None:
        taken_at = env.get("SWARPH_USAGE_TAKEN_AT") or None
    if now is None:
        now = env.get("SWARPH_USAGE_NOW") or datetime.now(timezone.utc).isoformat()
    if not taken_at:
        return _blank(who)
    lines = _transcript_lines(env)
    if lines:
        summed = sum_transcript(lines, taken_at, now)
        summed["agent"] = who or "unknown"
        summed["wall_s"] = "unknown"
        return summed
    found = _blank(who)
    found["model"] = _lane_model(env, taken_at, now)
    return found
