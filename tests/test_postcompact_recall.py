"""PostCompact timeline recall + memory emit-on-write.

Obligation from lab-ovh (2026-08-21): post-compact recall must be sourced from
the TIMELINE, not gbrain — gbrain reindexes daily and structurally cannot
contain today, so the first post-compact turn is exactly when today's highlights
matter most. Emit-on-write makes a memory write cache its own highlight with its
[[pointer]], so the loop does not depend on anyone remembering `swarph highlight`.

Both hooks are fail-safe: ANY error -> no-op output + exit 0. A hook that blocks
or crashes the session is worse than no recall at all.
"""
import json
import os
from pathlib import Path

import pytest

from swarph_cli.commands import postcompact_hook_output as pc
from swarph_cli.commands import memory_emit_hook as me


def _write_timeline(path: Path, lines: list[str]) -> None:
    path.write_text("# timeline\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _run_pc(monkeypatch, timeline: Path | None, stdin: str = "{}") -> dict:
    if timeline is not None:
        monkeypatch.setenv("SWARPH_TIMELINE", str(timeline))
    else:
        monkeypatch.setenv("SWARPH_TIMELINE", "/nonexistent/TIMELINE.md")
    monkeypatch.setattr(pc, "_maybe_pull", lambda repo: None)
    import io
    import contextlib
    out = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    with contextlib.redirect_stdout(out):
        rc = pc.run_postcompact_hook_output([])
    assert rc == 0, "the hook must ALWAYS exit 0"
    return json.loads(out.getvalue())


# ── PostCompact recall: the CANNOT/no-op branches first ─────────────────────

def test_missing_timeline_is_a_silent_noop(monkeypatch):
    out = _run_pc(monkeypatch, None)
    assert out == {}, "a missing timeline must inject NOTHING, not an error"


def test_malformed_stdin_still_reads_timeline(monkeypatch, tmp_path):
    tl = tmp_path / "TIMELINE.md"
    _write_timeline(tl, ["- 2026-08-21T04:55Z · **cursor-lin** · shipped #541"])
    out = _run_pc(monkeypatch, tl, stdin="not json at all")
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "shipped #541" in ctx


def test_malformed_lines_are_skipped_not_fatal(monkeypatch, tmp_path):
    tl = tmp_path / "TIMELINE.md"
    _write_timeline(tl, [
        "this line is not an entry",
        "- not-a-date · **x** · bad ts",
        "- 2026-08-21T04:55Z · **cursor-lin** · the good line",
    ])
    out = _run_pc(monkeypatch, tl)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "the good line" in ctx
    assert "not an entry" not in ctx


# ── the window: today must be present, day-8 must be gone ────────────────────

def test_window_includes_today_and_excludes_day_eight(monkeypatch, tmp_path):
    """>>> THE PROPERTY THE ACCEPT CHECK NAMES: the set must contain the current
    day — the case a gbrain-sourced design cannot produce. <<<"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%dT%H:%MZ")
    day6 = (now - timedelta(days=6)).strftime("%Y-%m-%dT%H:%MZ")
    day8 = (now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%MZ")
    tl = tmp_path / "TIMELINE.md"
    _write_timeline(tl, [
        f"- {day8} · **old-cell** · EIGHT DAYS OLD — outside the window",
        f"- {day6} · **peer** · six days old — inside",
        f"- {today} · **cursor-lin** · WRITTEN EARLIER TODAY",
    ])
    out = _run_pc(monkeypatch, tl)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "WRITTEN EARLIER TODAY" in ctx, "today's highlight must be injected"
    assert "six days old" in ctx
    assert "EIGHT DAYS OLD" not in ctx, "day-8 is outside the 7-day window"


def test_empty_window_emits_nothing(monkeypatch, tmp_path):
    tl = tmp_path / "TIMELINE.md"
    _write_timeline(tl, ["- 2026-01-01T00:00Z · **old** · ancient history only"])
    out = _run_pc(monkeypatch, tl)
    assert out == {}, "nothing in the window -> inject nothing, not an empty banner"


def test_budget_keeps_the_most_recent_and_says_how_many_dropped(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    # Append-only file order: OLDEST first, newest last — entry 000 is the
    # most recent and must be the LAST line, as the real timeline writes it.
    lines = [
        f"- {(now - timedelta(hours=h)).strftime('%Y-%m-%dT%H:%MZ')} · **c** · entry {h:03d} "
        + "x" * 200
        for h in reversed(range(200))
    ]
    tl = tmp_path / "TIMELINE.md"
    _write_timeline(tl, lines)
    out = _run_pc(monkeypatch, tl)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) <= pc._MAX_CHARS + 400, "the injection must be bounded"
    assert "entry 000" in ctx, "the MOST RECENT entries survive the budget"
    assert "earlier entries omitted" in ctx


def test_output_shape_names_postcompact(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    tl = tmp_path / "TIMELINE.md"
    _write_timeline(tl, [f"- {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')} · **c** · x"])
    out = _run_pc(monkeypatch, tl)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostCompact"


# ── emit-on-write: refusal branches first ────────────────────────────────────

def _run_emit(monkeypatch, payload: dict, posted: list, state: Path) -> int:
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setenv("SWARPH_EMIT_STATE", str(state))
    # A gateway must be CONFIGURED or the emit is a designed silent no-op.
    monkeypatch.setenv("SWARPH_GATEWAY", "http://test-gateway:8788")
    monkeypatch.setattr(me, "_log_via_gateway",
                        lambda gw, cell, text, mem, when, tf: posted.append(
                            {"text": text, "memory": mem}) or 0)
    return me.run_memory_emit_hook([])


def _write_payload(path: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path}}


def test_non_memory_path_is_silent(monkeypatch, tmp_path):
    posted = []
    rc = _run_emit(monkeypatch, _write_payload("/tmp/some/code.py"), posted,
                   tmp_path / "state.json")
    assert rc == 0 and posted == []


def test_memory_index_itself_is_not_a_memory(monkeypatch, tmp_path):
    posted = []
    rc = _run_emit(monkeypatch,
                   _write_payload("/home/ubuntu/.claude/projects/-home-ubuntu/memory/MEMORY.md"),
                   posted, tmp_path / "state.json")
    assert rc == 0 and posted == [], "writing the index is not caching a memory"


def test_missing_file_path_is_silent(monkeypatch, tmp_path):
    posted = []
    rc = _run_emit(monkeypatch, {"tool_name": "Write", "tool_input": {}}, posted,
                   tmp_path / "state.json")
    assert rc == 0 and posted == []


def test_gateway_failure_exits_zero(monkeypatch, tmp_path):
    import io
    memdir = tmp_path / ".claude" / "projects" / "-home-ubuntu" / "memory"
    memdir.mkdir(parents=True)
    target = memdir / "a-memory.md"
    target.write_text("# A memory\nbody\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(
        json.dumps(_write_payload(str(target)))))
    monkeypatch.setenv("SWARPH_EMIT_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("SWARPH_GATEWAY", "http://test-gateway:8788")
    monkeypatch.setattr(me, "_log_via_gateway", lambda *a: 1)  # gateway down
    assert me.run_memory_emit_hook([]) == 0, "a failed emit must NEVER fail the tool result"


# ── emit-on-write: the success path ──────────────────────────────────────────

def test_memory_write_emits_highlight_with_pointer(monkeypatch, tmp_path):
    memdir = tmp_path / ".claude" / "projects" / "-home-ubuntu" / "memory"
    memdir.mkdir(parents=True)
    target = memdir / "gate-state-expiry.md"
    target.write_text("# Gate-state expiry\nObservations expire; say the SHA.\n",
                      encoding="utf-8")
    posted = []
    rc = _run_emit(monkeypatch, _write_payload(str(target)), posted,
                   tmp_path / "state.json")
    assert rc == 0
    assert len(posted) == 1
    assert posted[0]["memory"] == "[[gate-state-expiry]]"
    assert "Gate-state expiry" in posted[0]["text"]


def test_repeat_write_within_window_is_suppressed(monkeypatch, tmp_path):
    memdir = tmp_path / ".claude" / "projects" / "-home-ubuntu" / "memory"
    memdir.mkdir(parents=True)
    target = memdir / "loop.md"
    target.write_text("# Loop\nv1\n", encoding="utf-8")
    posted = []
    state = tmp_path / "state.json"
    _run_emit(monkeypatch, _write_payload(str(target)), posted, state)
    _run_emit(monkeypatch, _write_payload(str(target)), posted, state)
    assert len(posted) == 1, "a second write of the same memory inside the window is noise"


def test_edit_tool_also_matches(monkeypatch, tmp_path):
    memdir = tmp_path / ".claude" / "projects" / "-home-ubuntu" / "memory"
    memdir.mkdir(parents=True)
    target = memdir / "edit-me.md"
    target.write_text("# Edit me\nbody\n", encoding="utf-8")
    posted = []
    rc = _run_emit(monkeypatch,
                   {"tool_name": "Edit", "tool_input": {"file_path": str(target)}},
                   posted, tmp_path / "state.json")
    assert rc == 0 and len(posted) == 1
