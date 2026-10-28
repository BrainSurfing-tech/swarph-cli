"""Card #1277 — cost per accepted outcome, from recorded usage only.

A row whose usage is unknown is counted and excluded from the token
totals. It is never treated as zero.
"""
from __future__ import annotations

_FIELDS = ("tokens_in", "tokens_out", "cache_read", "cache_write", "thinking")
_JOBS = ("execute", "grade", "advise", "rework")
_GRADE = {"validate", "plan-review", "second-seat", "spec-review"}


def job_of(row) -> str | None:
    if row.get("rework") or row.get("step") == "rework":
        return "rework"
    step = row.get("step") or ""
    if step == "build":
        return "execute"
    if step in _GRADE:
        return "grade"
    if step in {"advise", "advisor", "pre-read"} or "advise" in step:
        return "advise"
    return None


def token_total(usage):
    if not isinstance(usage, dict):
        return None
    total = 0
    for name in _FIELDS:
        value = usage.get(name, "unknown")
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        total += value
    return total


def _window_label(window) -> str:
    window = window or {}
    card = window.get("card")
    since = window.get("since") or "-"
    until = window.get("until") or "-"
    where = f"card #{card}" if card else "all cards"
    return f"{where} since {since} until {until}"


def format_cost(report: dict) -> str:
    closes = report.get("closes") or []
    later = report.get("later_fix") or {}
    by_card: dict = {}
    for row in closes:
        by_card.setdefault(row.get("card_id"), []).append(row)
    lines = [f"window: {_window_label(report.get('window'))}"]
    unknown_rows = 0
    done_known = []
    perfect_known = []
    done = 0
    perfect = 0
    for card_id, rows in by_card.items():
        stage = rows[0].get("stage") or "-"
        fixes = later.get(str(card_id)) or later.get(card_id) or []
        fails = sum(1 for row in rows if row.get("outcome") == "fail")
        reworks = sum(1 for row in rows if row.get("rework") or row.get("step") == "rework")
        fix_label = (" later-fix=" + ",".join(f"#{n}" for n in fixes)) if fixes else ""
        lines.append(f"card #{card_id} stage={stage}{fix_label}")
        buckets = {job: {"tokens": 0, "known": 0, "unknown": 0} for job in _JOBS}
        card_known = 0
        card_unknown = 0
        for row in rows:
            total = token_total(row.get("usage"))
            job = job_of(row)
            if total is None:
                unknown_rows += 1
                card_unknown += 1
                if job in buckets:
                    buckets[job]["unknown"] += 1
                continue
            card_known += total
            if job in buckets:
                buckets[job]["tokens"] += total
                buckets[job]["known"] += 1
        for job in _JOBS:
            bucket = buckets[job]
            lines.append(
                f"  {job}: {bucket['tokens']} tokens over {bucket['known']} "
                f"known rows (unknown {bucket['unknown']})")
        lines.append(f"  rows: {len(rows)} FAIL: {fails} reworks: {reworks}")
        is_done = stage == "done"
        is_perfect = is_done and fails == 0 and reworks == 0 and not fixes
        if is_done:
            done += 1
            if card_unknown == 0:
                done_known.append(card_known)
            if is_perfect:
                perfect += 1
                if card_unknown == 0:
                    perfect_known.append(card_known)
    lines.append(f"unknown rows: {unknown_rows}")
    known_sum = sum(done_known)
    if done and len(done_known) == done:
        lines.append(
            f"tokens per card done: {known_sum}/{done} "
            f"(known tokens, unknown rows excluded)")
        lines.append(
            f"cost per accepted outcome: {known_sum}/{done} "
            f"tokens per done card (unknown rows: {unknown_rows})")
    else:
        lines.append(
            "tokens per card done: unknown "
            f"({len(done_known)} of {done} done cards fully read)")
        lines.append(
            "cost per accepted outcome: unknown "
            f"({len(done_known)} of {done} done cards fully read, "
            f"unknown rows: {unknown_rows})")
    lines.append(f"perfect-run rate: {perfect}/{done}")
    if perfect and len(perfect_known) == perfect:
        lines.append(
            f"cost per perfect run: {sum(perfect_known)}/{perfect} "
            f"tokens per perfect card (unknown rows: {unknown_rows})")
    else:
        lines.append(f"cost per perfect run: no perfect runs ({perfect})")
    return "\n".join(lines)
