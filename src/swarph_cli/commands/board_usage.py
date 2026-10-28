"""Card #1277 — usage from the closing session only.

One session file, or every token field is unknown. Numbers are the
TokenUsage fields the transcript actually wrote. A missing field stays
"unknown". Nothing here turns a missing field into zero.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "thinking",
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


def _as_int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _thinking(usage: dict):
    details = usage.get("output_tokens_details")
    if isinstance(details, dict) and "thinking_tokens" in details:
        return details.get("thinking_tokens")
    return usage.get("thinking_tokens")


def _cache_parts(usage: dict):
    """(1h, 5m, unsplit) as ints or None. A present split is not also a flat total."""
    created = usage.get("cache_creation")
    if isinstance(created, dict) and (
            "ephemeral_1h_input_tokens" in created or "ephemeral_5m_input_tokens" in created):
        return (
            _as_int(created.get("ephemeral_1h_input_tokens")),
            _as_int(created.get("ephemeral_5m_input_tokens")),
            None,
        )
    if "cache_creation_input_tokens" in usage:
        return None, None, _as_int(usage.get("cache_creation_input_tokens"))
    return None, None, None


def _response_key(rec, message):
    """One Claude response is (message.id, requestId). Lines without an id stay separate."""
    mid = message.get("id") if isinstance(message, dict) else None
    if not isinstance(mid, str) or not mid:
        return None
    req = rec.get("requestId")
    if not isinstance(req, str) or not req:
        req = None
    return (mid, req)


def sum_transcript(lines, taken_at, now) -> dict:
    """Sum one session's usage objects inside [taken_at, now].

    Claude Code writes one JSONL line per content block and repeats the
    whole usage on each. A response is counted once, keyed by message id
    and request id. Thinking is stored and is not a separate total.
    """
    start = _parse_time(taken_at)
    end = _parse_time(now)
    totals = {name: 0 for name in _COUNT_FIELDS}
    seen = {name: False for name in _COUNT_FIELDS}
    missing = set()
    one_h = five = unsplit = 0
    saw_split = saw_flat = False
    split_gap = flat_gap = False
    models = []
    counted = 0
    seen_responses = set()
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
        key = _response_key(rec, message if isinstance(message, dict) else {})
        if key is not None:
            if key in seen_responses:
                continue
            seen_responses.add(key)
        counted += 1
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model.strip():
            models.append(model.strip())
        raw = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "thinking": _thinking(usage),
        }
        for name in _COUNT_FIELDS:
            value = _as_int(raw[name])
            if value is None:
                missing.add(name)
                continue
            seen[name] = True
            totals[name] += value
        hour, minute, flat = _cache_parts(usage)
        if hour is not None or minute is not None:
            saw_split = True
            if hour is None or minute is None:
                split_gap = True
            else:
                one_h += hour
                five += minute
        elif flat is not None:
            saw_flat = True
            unsplit += flat
        else:
            flat_gap = True
    out = {}
    for name in _COUNT_FIELDS:
        if name in missing or not seen[name] or counted == 0:
            out[name] = "unknown"
        else:
            out[name] = totals[name]
    if counted == 0 or saw_split and saw_flat or split_gap or (not saw_split and (not saw_flat or flat_gap)):
        out["cache_creation_1h_tokens"] = "unknown"
        out["cache_creation_5m_tokens"] = "unknown"
        out["cache_creation_unsplit_tokens"] = "unknown"
    elif saw_split:
        out["cache_creation_1h_tokens"] = one_h
        out["cache_creation_5m_tokens"] = five
        out["cache_creation_unsplit_tokens"] = "unknown"
    else:
        out["cache_creation_1h_tokens"] = "unknown"
        out["cache_creation_5m_tokens"] = "unknown"
        out["cache_creation_unsplit_tokens"] = unsplit
    distinct = set(models)
    out["model"] = models[0] if len(distinct) == 1 else "unknown"
    return out


def _project_root(env) -> Path:
    cwd = env.get("PWD") or os.getcwd()
    home = env.get("HOME") or str(Path.home())
    slug = "-" + str(cwd).strip("/").replace("/", "-")
    return Path(home) / ".claude" / "projects" / slug


def _resolve_id(root: Path, value: str) -> Path | None:
    direct = Path(value)
    if direct.is_file() and direct.suffix == ".jsonl":
        return direct
    if value and root.is_dir():
        named = root / f"{value}.jsonl"
        if named.is_file():
            return named
    return None


def _ppid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    end = stat.rfind(")")
    if end < 0:
        return 0
    parts = stat[end + 2:].split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def _is_claude(pid: int) -> bool:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return comm == "claude" or comm.startswith("claude")


def _open_jsonl(pid: int) -> list[Path]:
    found = []
    seen = set()
    try:
        entries = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if not target.endswith(".jsonl"):
            continue
        path = Path(target)
        if not path.is_file():
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def _ancestor_jsonl() -> Path | None:
    """The one jsonl the nearest ancestor claude process holds open."""
    pid = _ppid(os.getpid())
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        if _is_claude(pid):
            files = _open_jsonl(pid)
            if len(files) == 1:
                return files[0]
            if len(files) > 1:
                return None
        pid = _ppid(pid)
    return None


def _pinned_session(env, agent) -> str | None:
    if not agent or agent in {".", ".."} or "/" in agent or "\\" in agent:
        return None
    home = env.get("HOME") or str(Path.home())
    xdg = env.get("XDG_CONFIG_HOME") or str(Path(home) / ".config")
    path = Path(xdg) / "swarph" / "cells" / f"{agent}.yaml"
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("session_id:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            return value or None
    return None


def closing_session_file(env, agent) -> Path | None:
    """Exactly one session file, or None.

    Order: CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID if set; else the
    jsonl held open by the nearest ancestor claude; else the session_id
    pinned in the --as cell's swarph cell config when that names a file.
    A set session id that does not name a file does not fall through.
    """
    root = _project_root(env)
    session = env.get("CLAUDE_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID")
    if session:
        return _resolve_id(root, session)
    held = _ancestor_jsonl()
    if held is not None:
        return held
    pinned = _pinned_session(env, agent)
    if pinned:
        return _resolve_id(root, pinned)
    return None


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
        "input_tokens": "unknown",
        "output_tokens": "unknown",
        "cache_read_tokens": "unknown",
        "cache_creation_1h_tokens": "unknown",
        "cache_creation_5m_tokens": "unknown",
        "cache_creation_unsplit_tokens": "unknown",
        "thinking": "unknown",
        "wall_s": "unknown",
    }


def _session_lines(path: Path) -> list[str]:
    """The session file plus that session's subagents/*.jsonl."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    folder = path.with_suffix("") / "subagents"
    if not folder.is_dir():
        return lines
    for child in sorted(folder.glob("*.jsonl")):
        try:
            lines.extend(child.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return lines


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
    path = closing_session_file(env, who)
    if path is None:
        found = _blank(who)
        found["model"] = _lane_model(env, taken_at, now)
        return found
    lines = _session_lines(path)
    if not lines and not path.is_file():
        return _blank(who)
    summed = sum_transcript(lines, taken_at, now)
    summed["agent"] = who or "unknown"
    summed["wall_s"] = "unknown"
    return summed
