"""Card #1445 phase 1b — usage pools: Codex reader + `swarph usage` table.

All fixtures are synthetic (never the real ~/.codex content): rollout
event shapes follow design post 103202, pool-file shapes follow the live
~/swarph_state/usage/*.json files. Tests use tmp dirs throughout.
"""
from __future__ import annotations

import inspect
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
    # source matches the file. A snapshot with no source is a null -> value
    # reset (see test_null_source_to_value_is_a_reset), not a burn.
    snapshot = {"readings": {
        "Claude\x00seven_day": {"used_percentage": 77.0,
                                "read_at": prev_read,
                                "source": "test"}}}
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


# ── FOLLOW-UP (card #6375): burn honesty, snapshot lifecycle, ──────────────
# ── override validation, rollout fallback, credential hygiene ──────────────
# All fixtures synthetic; no real ~/.codex content, no real credentials.

RESETS_A = _iso(NOW + timedelta(days=1))
RESETS_B = _iso(NOW + timedelta(days=8))


def _claude_dir(tmp_path: Path, *, used: float, read_at: str,
                resets: str, source: str = "test") -> Path:
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir(exist_ok=True)
    (usage_dir / "claude.json").write_text(json.dumps({
        "provider": "anthropic-claude", "source": source,
        "read_at": read_at,
        "windows": {"seven_day": {"used_percentage": used,
                                  "resets_at": resets}}}),
        encoding="utf-8")
    return usage_dir


def _snapshot_entry(*, used: float, read_at: str, source: str = "test",
                    resets: str = RESETS_A, burn=None, reset: bool = False):
    entry: dict = {"used_percentage": used, "read_at": read_at,
                   "source": source, "resets_at": resets}
    if burn is not None:
        entry["burn_per_h"] = burn
    if reset:
        entry["reset"] = True
    return entry


def test_burn_blank_and_reset_marked_on_used_drop(tmp_path, capsys):
    """98% -> 2% in 1h must not print -96.0%/h (the #6374 finding)."""
    usage_dir = _claude_dir(tmp_path, used=2.0,
                            read_at=_iso(NOW - timedelta(minutes=30)),
                            resets=RESETS_A)
    snapshot = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=98.0, read_at=_iso(NOW - timedelta(hours=1, minutes=30)))}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["burn_per_h"] is None
    assert claude["reset"] is True
    text = render_table(rows)
    assert "RESET" in text
    assert "-96" not in text
    marked = [r for r in rows_to_json(rows)["rows"]
              if r["pool"] == "Claude"][0]
    assert marked["reset"] is True and marked["burn_per_h"] is None
    save_snapshot(usage_dir, rows, snapshot)  # CLI show reads this dotfile
    rc = usage_cmd.run_usage(["--state-root", str(tmp_path)])
    assert rc == 0
    assert "RESET" in capsys.readouterr().out


def test_burn_blank_and_reset_marked_on_resets_change(tmp_path):
    """Same window name, new resets_at: a fresh period, not a rate."""
    usage_dir = _claude_dir(tmp_path, used=20.0,
                            read_at=_iso(NOW - timedelta(minutes=30)),
                            resets=RESETS_B)
    snapshot = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=10.0, read_at=_iso(NOW - timedelta(hours=1, minutes=30)),
        resets=RESETS_A)}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["burn_per_h"] is None
    assert claude["reset"] is True


def test_burn_blank_and_reset_marked_on_source_change(tmp_path):
    """Snapshot source differs from current source: no silent mixed rate."""
    usage_dir = _claude_dir(tmp_path, used=20.0,
                            read_at=_iso(NOW - timedelta(minutes=30)),
                            resets=RESETS_A, source="cell-b")
    snapshot = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=10.0, read_at=_iso(NOW - timedelta(hours=1, minutes=30)),
        source="cell-a")}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["burn_per_h"] is None
    assert claude["reset"] is True


def test_reread_carries_last_burn_forward(tmp_path, capsys):
    """Showing the table twice on the SAME reading keeps the last burn."""
    read_at = _iso(NOW - timedelta(minutes=30))
    usage_dir = _claude_dir(tmp_path, used=87.0, read_at=read_at,
                            resets=RESETS_A)
    pool_map = load_pool_map(usage_dir)
    prev = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=77.0, read_at=_iso(NOW - timedelta(hours=4)))}}
    first = collect_rows(usage_dir=usage_dir, pool_map=pool_map, now=NOW,
                         snapshot=prev)
    burn = [r for r in first if r["pool"] == "Claude"][0]["burn_per_h"]
    assert burn == abs((87.0 - 77.0) / 3.5)
    save_snapshot(usage_dir, first, prev)
    stored = load_snapshot(usage_dir)["readings"]["Claude\x00seven_day"]
    assert stored["burn_per_h"] == burn  # the burn itself is persisted
    second = collect_rows(usage_dir=usage_dir, pool_map=pool_map, now=NOW,
                          snapshot=load_snapshot(usage_dir))
    again = [r for r in second if r["pool"] == "Claude"][0]
    assert again["burn_per_h"] == burn
    assert again["reset"] is False
    assert "+2.86%/h" in render_table(second)
    # Saving the identical re-read does not advance (or blank) the snapshot.
    before = (usage_dir / ".usage-last-seen.json").read_text()
    save_snapshot(usage_dir, second, load_snapshot(usage_dir))
    assert (usage_dir / ".usage-last-seen.json").read_text() == before
    rc = usage_cmd.run_usage(["--state-root", str(tmp_path)])
    assert rc == 0
    assert "+2.86%/h" in capsys.readouterr().out


def test_bad_pool_map_override_is_refused(tmp_path, capsys):
    """Malformed .pools.local.json -> ValueError / exit 2, never KeyError."""
    good = load_pool_map()
    variants = {
        "missing-pool-name": [{"files": ["claude.json"],
                               "providers": ["anthropic-claude"]}],
        "files-not-a-list": [{"pool": "Solo", "cells": ["me"],
                              "files": "solo.json", "providers": ["solo"]}],
        "entry-not-an-object": ["Solo"],
        "providers-not-a-list": [{"pool": "Solo", "cells": ["me"],
                                  "files": ["solo.json"],
                                  "providers": "solo"}],
        "empty-pool-name": [{"pool": "  ", "cells": ["me"],
                             "files": ["solo.json"], "providers": ["solo"]}],
        "non-string-cell": [{"pool": "Solo", "cells": [7],
                             "files": ["solo.json"], "providers": ["solo"]}],
    }
    for name, pools in variants.items():
        usage_dir = tmp_path / name / "usage"
        usage_dir.mkdir(parents=True)
        (usage_dir / ".pools.local.json").write_text(
            json.dumps({"stale_after_hours": 6, "pools": pools}))
        try:
            load_pool_map(usage_dir)
        except ValueError as exc:
            assert "pool map" in str(exc), name
        else:
            raise AssertionError(f"override {name} was accepted")
        rc = usage_cmd.run_usage(["--state-root", str(tmp_path / name),
                                  "--json"])
        assert rc == 2, name
        err = capsys.readouterr().err
        assert "pool map unreadable" in err, name
    assert good["pools"]  # the packaged default still loads


def test_sync_falls_back_to_older_rollout_with_event(tmp_path):
    """Newest rollout has no rate_limits yet -> newest older one with one."""
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-new-empty.jsonl",
                   [json.dumps({"payload": {"type": "token_count",
                                            "info": {}}})],
                   mtime=2000.0)
    _write_rollout(sessions / "rollout-old-full.jsonl",
                   [_rate_limits_event(used_weekly=42.0,
                                       resets_epoch=1795618145)],
                   mtime=1000.0)
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage")
    assert result is not None
    assert "rollout-old-full" in result["rollout"]
    assert result["windows"]["week"]["used_percentage"] == 42.0
    written = json.loads((tmp_path / "usage" / "openai.json").read_text())
    assert written["windows"]["week"]["used_percentage"] == 42.0


def test_sync_writes_nothing_when_no_rollout_has_event(tmp_path):
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-a.jsonl",
                   [json.dumps({"payload": {"type": "token_count",
                                            "info": {}}})],
                   mtime=2000.0)
    _write_rollout(sessions / "rollout-b.jsonl",
                   ["not json at all",
                    json.dumps({"payload": {"rate_limits": None}})],
                   mtime=1000.0)
    assert codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage") is None
    assert not (tmp_path / "usage" / "openai.json").exists()


def test_rollout_credentials_never_reach_openai_json(tmp_path):
    """Credential-shaped payload keys are never copied into openai.json."""
    secrets = ("sk-PROBE-SYNTHETIC-abc123", "tok-probe-synthetic-xyz789",
               "sekret-probe-synthetic-456def")
    sessions = tmp_path / "sessions"
    event = json.loads(_rate_limits_event(used_weekly=11.0,
                                          resets_epoch=1795618145))
    event["payload"]["api_key"] = secrets[0]
    event["payload"]["session_token"] = secrets[1]
    event["payload"]["auth"] = {"secret": secrets[2]}
    _write_rollout(sessions / "rollout-creds.jsonl", [json.dumps(event)],
                   mtime=1000.0)
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage")
    assert result is not None
    text = (tmp_path / "usage" / "openai.json").read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in text
    assert "api_key" not in text and "session_token" not in text


# ── FOLLOW-UP (card #1445 / plan-review #6379): event age, unmappable ──────
# ── fallthrough, null-source reset, pool-map NaN and duplicate names ───────
# Synthetic fixtures only. Never the real ~/.codex directory.

_RESET_BLIND_SPOT = (
    "a rise right after a reset, with no resets_at to compare, "
    "cannot be told apart from ordinary burn"
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _solo_pool(*cells: str, filename: str = "solo.json",
               provider: str = "solo", name: str = "Solo") -> dict:
    return {"pool": name, "cells": list(cells) or ["me"],
            "files": [filename], "providers": [provider]}


def _write_pool_override(usage_dir: Path, *, stale, pools: list) -> None:
    usage_dir.mkdir(parents=True, exist_ok=True)
    (usage_dir / ".pools.local.json").write_text(
        json.dumps({"stale_after_hours": stale, "pools": pools}),
        encoding="utf-8")


def test_fallback_read_at_is_event_timestamp_not_sync_clock(tmp_path):
    """Event time ages the reading. The plan-review probe wrote read_at from
    the sync clock, so a 2026-11-01 event (file mtime epoch 1000) showed
    age_h ~0 and stale False. At the table clock that age is 534h, STALE.
    """
    sessions = tmp_path / "sessions"
    _write_rollout(sessions / "rollout-new-empty.jsonl",
                   [json.dumps({"timestamp": "2026-11-23T05:59:00Z",
                                "payload": {"type": "token_count",
                                            "info": {}}})],
                   mtime=3000.0)
    event = json.loads(_rate_limits_event(used_weekly=40.0,
                                          resets_epoch=1795618145))
    event["timestamp"] = "2026-11-01"
    _write_rollout(sessions / "rollout-old.jsonl",
                   [json.dumps(event)], mtime=1000.0)
    usage_dir = tmp_path / "usage"
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=usage_dir)
    assert result is not None
    assert "rollout-old" in result["rollout"]
    written = json.loads((usage_dir / "openai.json").read_text(encoding="utf-8"))
    assert written["read_at"] == "2026-11-01T00:00:00Z"
    # Epoch 1000 is 1970-01-01T00:16:40Z. The event time wins over mtime.
    assert written["read_at"] != "1970-01-01T00:16:40Z"
    assert written["source"] == codex_reader.SOURCE
    assert "mtime" not in written["source"]
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    openai = [r for r in rows if r["pool"] == "OpenAI" and r["window"] == "week"][0]
    assert openai["age_h"] == 534.0
    assert openai["stale"] is True
    assert openai["status"] == "stale"
    assert "STALE" in render_table(rows)


def test_event_without_timestamp_uses_file_mtime_and_says_so(tmp_path):
    """No event timestamp -> rollout mtime, and source records that."""
    base = json.loads(_rate_limits_event(used_weekly=11.0,
                                         resets_epoch=1795618145))
    missing = dict(base)
    missing.pop("timestamp")
    explicit_null = dict(base)
    explicit_null["timestamp"] = None
    blank = dict(base)
    blank["timestamp"] = "  "
    unparseable = dict(base)
    unparseable["timestamp"] = "not-a-timestamp"
    for index, event in enumerate((missing, explicit_null, blank, unparseable)):
        isolated = tmp_path / f"sessions-{index}"
        _write_rollout(isolated / "rollout-notime.jsonl",
                       [json.dumps(event)], mtime=1000.0)
        usage_dir = tmp_path / f"usage-{index}"
        result = codex_reader.write_openai_json(
            sessions_dir=isolated, usage_dir=usage_dir)
        assert result is not None, index
        written = json.loads(
            (usage_dir / "openai.json").read_text(encoding="utf-8"))
        assert written["read_at"] == "1970-01-01T00:16:40Z", index
        assert written["source"] == codex_reader.SOURCE_FROM_FILE_MTIME, index
        assert "mtime" in written["source"]
        assert "no timestamp" in written["source"]


def test_unmappable_newest_rollout_falls_through(tmp_path):
    """used_percent without resets_at yields no windows; the older file maps."""
    sessions = tmp_path / "sessions"
    unmappable = {
        "timestamp": "2026-11-23T01:00:00Z",
        "type": "event_msg",
        "payload": {"rate_limits": {
            "primary": {"used_percent": 88.0, "window_minutes": 10080},
            "secondary": None,
        }},
    }
    _write_rollout(sessions / "rollout-new-unmappable.jsonl",
                   [json.dumps(unmappable)], mtime=5000.0)
    _write_rollout(sessions / "rollout-mid-empty.jsonl",
                   [json.dumps({"payload": {"type": "token_count",
                                            "info": {}}})],
                   mtime=3000.0)
    _write_rollout(sessions / "rollout-old-mappable.jsonl",
                   [_rate_limits_event(used_weekly=42.0,
                                       resets_epoch=1795618145)],
                   mtime=1000.0)
    usage_dir = tmp_path / "usage"
    result = codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=usage_dir)
    assert result is not None
    assert "rollout-old-mappable" in result["rollout"]
    assert result["windows"]["week"]["used_percentage"] == 42.0
    written = json.loads((usage_dir / "openai.json").read_text(encoding="utf-8"))
    assert written["windows"]["week"]["used_percentage"] == 42.0
    assert written["read_at"] == "2026-11-22T09:43:41Z"


def test_all_unmappable_rollouts_write_nothing(tmp_path):
    sessions = tmp_path / "sessions"
    unmappable = {
        "timestamp": "2026-11-23T01:00:00Z",
        "payload": {"rate_limits": {
            "primary": {"used_percent": 88.0, "window_minutes": 10080},
        }},
    }
    _write_rollout(sessions / "rollout-a.jsonl", [json.dumps(unmappable)],
                   mtime=2000.0)
    _write_rollout(sessions / "rollout-b.jsonl",
                   [json.dumps({"payload": {"rate_limits": {"primary": {
                       "used_percent": 1.0, "window_minutes": 300}}}})],
                   mtime=1000.0)
    assert codex_reader.write_openai_json(
        sessions_dir=sessions, usage_dir=tmp_path / "usage") is None
    assert not (tmp_path / "usage" / "openai.json").exists()


def test_null_source_to_value_is_a_reset(tmp_path):
    """None -> "cell-b" is a reset. The old guard required both sources
    non-null, so this rise still computed a burn."""
    usage_dir = _claude_dir(tmp_path, used=20.0,
                            read_at=_iso(NOW - timedelta(minutes=30)),
                            resets=RESETS_A, source="cell-b")
    snapshot = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=10.0, read_at=_iso(NOW - timedelta(hours=1, minutes=30)),
        source=None)}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["reset"] is True
    assert claude["burn_per_h"] is None
    assert "RESET" in render_table(rows)


def test_used_rise_with_unchanged_resets_at_keeps_burn(tmp_path):
    """A used rise with the same resets_at is burn, not a reset."""
    usage_dir = _claude_dir(tmp_path, used=20.0,
                            read_at=_iso(NOW - timedelta(minutes=30)),
                            resets=RESETS_A, source="cell-a")
    snapshot = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=10.0, read_at=_iso(NOW - timedelta(hours=1, minutes=30)),
        source="cell-a", resets=RESETS_A)}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["reset"] is False
    assert claude["burn_per_h"] == 10.0


def test_used_rise_with_null_resets_at_keeps_burn_not_reset(tmp_path):
    """Limit that remains: a rise right after a reset, with no resets_at
    to compare, cannot be told apart from ordinary burn."""
    usage_dir = _claude_dir(tmp_path, used=20.0,
                            read_at=_iso(NOW - timedelta(minutes=30)),
                            resets=None, source="cell-a")
    snapshot = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=10.0, read_at=_iso(NOW - timedelta(hours=1, minutes=30)),
        source="cell-a", resets=None)}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["resets_at"] is None
    assert claude["reset"] is False
    assert claude["burn_per_h"] == 10.0
    assert _RESET_BLIND_SPOT in _flat(usage_cmd.__doc__)
    assert _RESET_BLIND_SPOT in _flat(usage_cmd._build_parser().description)


def test_used_rise_when_source_cleared_is_not_labelled_reset(tmp_path):
    """Value -> null is not the null-to-value reset. A rise with the same
    resets_at stays burn."""
    usage_dir = _claude_dir(tmp_path, used=20.0,
                            read_at=_iso(NOW - timedelta(minutes=30)),
                            resets=RESETS_A, source=None)
    snapshot = {"readings": {"Claude\x00seven_day": _snapshot_entry(
        used=10.0, read_at=_iso(NOW - timedelta(hours=1, minutes=30)),
        source="cell-a", resets=RESETS_A)}}
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot=snapshot)
    claude = [r for r in rows if r["pool"] == "Claude"][0]
    assert claude["source"] is None
    assert claude["reset"] is False
    assert claude["burn_per_h"] == 10.0


def test_usage_help_documents_unresolved_reset_rise(capsys):
    assert _RESET_BLIND_SPOT in _flat(usage_cmd.__doc__)
    assert _RESET_BLIND_SPOT in _flat(usage_cmd._build_parser().description)
    # The code comment at the reset check carries the same sentence.
    assert _RESET_BLIND_SPOT in _flat(inspect.getsource(collect_rows))
    try:
        usage_cmd.run_usage(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("swarph usage --help did not exit")
    assert _RESET_BLIND_SPOT in _flat(capsys.readouterr().out)


def test_pool_map_refuses_nan_stale_after_hours(tmp_path, capsys):
    """NaN used to be accepted, and a 534h-old reading stayed status ok
    because `age_h > nan` is false."""
    usage_dir = tmp_path / "usage"
    _write_pool_override(usage_dir, stale=float("nan"), pools=[_solo_pool()])
    try:
        load_pool_map(usage_dir)
    except ValueError as exc:
        assert "stale_after_hours" in str(exc)
        assert "finite" in str(exc)
    else:
        raise AssertionError("NaN stale_after_hours was accepted")
    rc = usage_cmd.run_usage(["--state-root", str(tmp_path), "--json"])
    assert rc == 2
    assert "pool map unreadable" in capsys.readouterr().err


def test_pool_map_refuses_infinite_and_nonpositive_stale_after_hours(tmp_path):
    for stale in (float("inf"), float("-inf"), 0, 0.0, -1, -0.5):
        usage_dir = tmp_path / f"usage-{stale}"
        _write_pool_override(usage_dir, stale=stale, pools=[_solo_pool()])
        try:
            load_pool_map(usage_dir)
        except ValueError as exc:
            assert "stale_after_hours" in str(exc), stale
        else:
            raise AssertionError(f"stale_after_hours {stale!r} was accepted")
    # A positive finite threshold still loads.
    ok = tmp_path / "ok" / "usage"
    _write_pool_override(ok, stale=6, pools=[_solo_pool()])
    assert load_pool_map(ok)["stale_after_hours"] == 6


def test_pool_map_refuses_duplicate_pool_names(tmp_path, capsys):
    """Duplicate names used to be accepted and the second entry's cells dropped."""
    usage_dir = tmp_path / "usage"
    pools = [
        _solo_pool("first-cell", filename="a.json", provider="a"),
        _solo_pool("second-cell", filename="b.json", provider="b"),
    ]
    _write_pool_override(usage_dir, stale=6, pools=pools)
    try:
        load_pool_map(usage_dir)
    except ValueError as exc:
        text = str(exc)
        assert "duplicate" in text
        assert "Solo" in text
        assert "second entry's cells" in text
    else:
        raise AssertionError("duplicate pool names were accepted")
    rc = usage_cmd.run_usage(["--state-root", str(tmp_path)])
    assert rc == 2
    assert "pool map unreadable" in capsys.readouterr().err
