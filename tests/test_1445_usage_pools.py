"""Card #1445 phase 1b — usage pools: Codex reader + `swarph usage` table.

All fixtures are synthetic (never the real ~/.codex content): rollout
event shapes follow design post 103202, pool-file shapes follow the live
~/swarph_state/usage/*.json files. Tests use tmp dirs throughout.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from swarph_cli.commands import usage as usage_cmd
from swarph_cli.main import registered_verbs
from swarph_cli.usage import (
    collect_rows,
    default_usage_dir,
    load_pool_map,
    load_snapshot,
    render_table,
    rows_to_json,
    save_snapshot,
)
from swarph_cli.usage import codex as codex_reader

NOW = datetime(2026, 11, 23, 6, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _rate_limits_event(*, used_weekly: float, resets_epoch: int,
                       used_5h: "float | None" = None,
                       nested_in_info: bool = False) -> str:
    rate_limits: dict = {
        "primary": {"used_percent": used_weekly, "window_minutes": 10080,
                    "resets_at": resets_epoch},
        "secondary": None,
    }
    if used_5h is not None:
        rate_limits["secondary"] = {"used_percent": used_5h,
                                    "window_minutes": 300,
                                    "resets_at": resets_epoch}
    payload: dict = {"type": "token_count", "info": {}}
    if nested_in_info:
        payload["info"] = {"rate_limits": rate_limits}
    else:
        payload["rate_limits"] = rate_limits
    return json.dumps({"timestamp": "2026-11-22T09:43:41Z",
                       "type": "event_msg", "payload": payload})


def _write_rollout(path: Path, lines: list, *, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _usage_dir_with(tmp_path: Path) -> Path:
    """A small usage dir: one fresh Claude file with an epoch reset."""
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    (usage_dir / "claude.json").write_text(json.dumps({
        "provider": "anthropic-claude", "source": "test",
        "read_at": _iso(NOW - timedelta(minutes=30)),
        "windows": {"seven_day": {"used_percentage": 87.0,
                                  "resets_at": 1795453200}}}),
        encoding="utf-8")
    return usage_dir


# ── CODEX READER ──────────────────────────────────────────────────────────

def test_old_filename_but_newest_mtime_wins(tmp_path):
    """A resumed session keeps its old dated name: pick by mtime, not name."""
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "2026" / "11" / "rollout-2026-11-22T09-00-00-new.jsonl",
                   [_rate_limits_event(used_weekly=10.0,
                                       resets_epoch=1795618145)],
                   mtime=1000.0)
    _write_rollout(sessions / "2026" / "08" / "rollout-2026-01-01T00-00-00-old.jsonl",
                   [_rate_limits_event(used_weekly=42.0,
                                       resets_epoch=1795618145)],
                   mtime=2000.0)
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage")
    assert result is not None
    assert result["windows"]["week"]["used_percentage"] == 42.0
    assert "2026-01-01" in result["rollout"]
    written = json.loads((tmp_path / "usage" / "openai.json").read_text())
    assert written["provider"] == "openai"
    assert written["windows"]["week"]["used_percentage"] == 42.0
    # Epoch resets are normalised to UTC ISO Z, like muse.json.
    assert written["windows"]["week"]["resets_at"].endswith("Z")
    assert written["read_at"].endswith("Z")


def test_no_rate_limits_anywhere_writes_nothing(tmp_path):
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-2026-11-22T09-00-00-x.jsonl",
                   [json.dumps({"timestamp": "2026-11-22T09:43:41Z",
                                "type": "event_msg",
                                "payload": {"type": "token_count",
                                            "info": {}}}),
                    "not json at all",
                    json.dumps({"payload": {"rate_limits": None}})],
                   mtime=1000.0)
    assert codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage") is None
    assert not (tmp_path / "usage" / "openai.json").exists()


def test_no_rollout_file_writes_nothing(tmp_path):
    assert codex_reader.write_openai_json(
        sessions_dir=tmp_path / "nope", usage_dir=tmp_path / "usage") is None
    assert not (tmp_path / "usage" / "openai.json").exists()


def test_two_events_last_wins(tmp_path):
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-2026-11-22T09-00-00-x.jsonl",
                   [_rate_limits_event(used_weekly=20.0,
                                       resets_epoch=1795618145),
                    "garbage line",
                    _rate_limits_event(used_weekly=21.0,
                                       resets_epoch=1795618145)],
                   mtime=1000.0)
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage")
    assert result["windows"]["week"]["used_percentage"] == 21.0


def test_info_nested_rate_limits_variant(tmp_path):
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-2026-11-22T09-00-00-x.jsonl",
                   [_rate_limits_event(used_weekly=33.0,
                                       resets_epoch=1795618145,
                                       nested_in_info=True)],
                   mtime=1000.0)
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage")
    assert result["windows"]["week"]["used_percentage"] == 33.0


def test_both_windows_mapped_by_minutes(tmp_path):
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-2026-11-22T09-00-00-x.jsonl",
                   [_rate_limits_event(used_weekly=10.0, used_5h=2.5,
                                       resets_epoch=1795618145)],
                   mtime=1000.0)
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage")
    assert set(result["windows"]) == {"week", "5h"}
    assert result["windows"]["5h"]["used_percentage"] == 2.5


# ── TABLE ─────────────────────────────────────────────────────────────────

def _full_usage_dir(tmp_path: Path) -> Path:
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir(exist_ok=True)
    (usage_dir / "claude.json").write_text(json.dumps({
        "provider": "anthropic-claude", "source": "test",
        "read_at": _iso(NOW - timedelta(minutes=30)),
        "windows": {"seven_day": {"used_percentage": 87.0,
                                  "resets_at": 1795453200}}}),
        encoding="utf-8")
    (usage_dir / "muse.json").write_text(json.dumps({
        "provider": "muse", "source": "test",
        "read_at": _iso(NOW - timedelta(hours=17)),
        "windows": {"5h": {"used_percentage": 1.0,
                           "resets_at": _iso(NOW + timedelta(hours=3))},
                    "week": {"used_percentage": 75.0,
                             "resets_at": _iso(NOW + timedelta(hours=6))}}}),
        encoding="utf-8")
    (usage_dir / "cursor.json").write_text(json.dumps({
        "provider": "cursor+xai-supergrok", "source": "test",
        "read_at": _iso(NOW - timedelta(hours=1)),
        "windows": {"included": {"used_percentage": 29.55628442354106,
                                 "resets_at": _iso(NOW + timedelta(days=2))}}}),
        encoding="utf-8")
    (usage_dir / "google.json").write_text(json.dumps({
        "provider": "google-antigravity", "source": "test",
        "read_at": _iso(NOW - timedelta(minutes=10)),
        "quota": {"gemini-weekly": {"remaining_fraction": 0.9,
                                    "reset_time": _iso(NOW + timedelta(days=5))}}}),
        encoding="utf-8")
    return usage_dir


def test_stale_file_marked_stale_missing_pool_shown(tmp_path):
    usage_dir = _full_usage_dir(tmp_path)
    pool_map = load_pool_map(usage_dir)
    rows = collect_rows(usage_dir=usage_dir, pool_map=pool_map, now=NOW,
                        snapshot={"readings": {}})
    by_pool = {}
    for row in rows:
        by_pool.setdefault(row["pool"], []).append(row)
    muse_rows = {r["window"]: r for r in by_pool["Muse"]}
    assert muse_rows["week"]["status"] == "stale"
    assert muse_rows["week"]["stale"] is True
    text = render_table(rows)
    assert "STALE" in text
    # Pools with no file still appear, visibly not current.
    assert by_pool["OpenAI"][0]["status"] == "missing"
    assert by_pool["opencode"][0]["status"] == "missing"
    assert "missing" in text
    # Fresh readings are presented as current, not stale.
    claude = [r for r in by_pool["Claude"] if r["window"] == "seven_day"][0]
    assert claude["status"] == "ok" and claude["stale"] is False


def test_google_quota_shape_converts_remaining_to_used(tmp_path):
    usage_dir = _full_usage_dir(tmp_path)
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    gemini = [r for r in rows if r["pool"] == "Antigravity"
              and r["window"] == "gemini-weekly"][0]
    assert gemini["used_pct"] == abs(100.0 * (1.0 - 0.9))
    assert gemini["status"] == "ok"


def test_cursor_float_percent_shown(tmp_path, capsys):
    usage_dir = _full_usage_dir(tmp_path)
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    cursor = [r for r in rows if r["pool"] == "Cursor+SuperGrok"][0]
    assert cursor["used_pct"] == 29.55628442354106
    assert "29.6%" in render_table(rows)
    rc = usage_cmd.run_usage(["--state-root", str(tmp_path),
                              "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    names = {(r["pool"], r["window"]) for r in payload["rows"]}
    assert ("Cursor+SuperGrok", "included") in names
    assert ("opencode", None) in names


def test_epoch_resets_render_as_utc(tmp_path):
    usage_dir = _usage_dir_with(tmp_path)
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["resets_at"].endswith("Z")
    text = render_table(rows)
    assert claude["resets_at"].replace("T", " ")[:-1] in text


def test_burn_needs_two_distinct_readings(tmp_path):
    usage_dir = _full_usage_dir(tmp_path)
    pool_map = load_pool_map(usage_dir)
    prev_read = _iso(NOW - timedelta(hours=4))
    snapshot = {"readings": {
        "Claude\x00seven_day": {"used_percentage": 77.0,
                                "read_at": prev_read}}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=pool_map, now=NOW,
                        snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    # (87 - 77) over (3.5h) = +2.857 %/h.
    assert claude["burn_per_h"] == abs((87.0 - 77.0) / 3.5)
    assert "+2.86%/h" in render_table(rows)
    # Every other row had a single reading: no fabricated burn.
    assert all(r["burn_per_h"] is None for r in rows if r is not claude)


def test_same_reading_is_not_a_second_reading(tmp_path):
    usage_dir = _full_usage_dir(tmp_path)
    rows = collect_rows(usage_dir=usage_dir,
                        pool_map=load_pool_map(usage_dir), now=NOW,
                        snapshot={"readings": {
                            "Claude\x00seven_day": {
                                "used_percentage": 87.0,
                                "read_at": _iso(NOW - timedelta(minutes=30))}}})
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["burn_per_h"] is None


def test_snapshot_dotfile_is_never_a_pool(tmp_path, capsys):
    usage_dir = _full_usage_dir(tmp_path)
    save_snapshot(usage_dir,
                  collect_rows(usage_dir=usage_dir,
                               pool_map=load_pool_map(usage_dir), now=NOW,
                               snapshot={"readings": {}}),
                  {"readings": {}})
    assert (usage_dir / ".usage-last-seen.json").exists()
    rc = usage_cmd.run_usage(["--state-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "usage-last-seen" not in out
    assert load_snapshot(usage_dir)["readings"]


def test_table_lists_all_six_pools_in_map_order(tmp_path, capsys):
    usage_dir = _full_usage_dir(tmp_path)
    rc = usage_cmd.run_usage(["--state-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    pools = ["Claude", "Antigravity", "Muse", "Cursor+SuperGrok",
             "OpenAI", "opencode"]
    for pool in pools:
        assert pool in out
    assert [out.index(p) for p in pools] == sorted(
        [out.index(p) for p in pools])


def test_sync_writes_openai_json(tmp_path, capsys):
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-2026-11-22T09-00-00-x.jsonl",
                   [_rate_limits_event(used_weekly=10.0,
                                       resets_epoch=1795618145)],
                   mtime=1000.0)
    rc = usage_cmd.run_usage(["sync", "--state-root", str(tmp_path),
                              "--codex-sessions", str(sessions)])
    assert rc == 0
    assert "openai.json" in capsys.readouterr().out
    assert (tmp_path / "usage" / "openai.json").exists()


def test_sync_with_no_events_writes_nothing(tmp_path, capsys):
    rc = usage_cmd.run_usage(["sync", "--state-root", str(tmp_path),
                              "--codex-sessions", str(tmp_path / "empty")])
    assert rc == 0
    assert "wrote nothing" in capsys.readouterr().out


def test_pool_map_is_data_not_code(tmp_path):
    pool_map = load_pool_map()
    pools = {p["pool"]: p for p in pool_map["pools"]}
    assert set(pools) == {"Claude", "Antigravity", "Muse",
                          "Cursor+SuperGrok", "OpenAI", "opencode"}
    assert pools["Claude"]["cells"] == ["lab-ovh", "science-claude",
                                       "drop-on-meta-edge", "gridiron",
                                       "droplet"]
    assert pools["OpenAI"]["cells"] == ["gpt-ops"]
    assert pools["opencode"]["files"] == []
    # A usage-dir dotfile override replaces the default: no code change.
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir()
    override = dict(pool_map)
    override["pools"] = [{"pool": "Solo", "cells": ["me"],
                          "files": ["solo.json"], "providers": ["solo"]}]
    (usage_dir / ".pools.local.json").write_text(json.dumps(override))
    assert [p["pool"] for p in load_pool_map(usage_dir)["pools"]] == ["Solo"]


def test_state_root_override_is_honoured(monkeypatch, tmp_path):
    monkeypatch.delenv("SWARPH_STATE_ROOT", raising=False)
    import pathlib
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path / "home")
    assert default_usage_dir().parent == tmp_path / "home" / "swarph_state"
    monkeypatch.setenv("SWARPH_STATE_ROOT", str(tmp_path / "custom"))
    assert default_usage_dir() == tmp_path / "custom" / "usage"


def test_usage_verb_is_registered():
    assert "usage" in registered_verbs()
