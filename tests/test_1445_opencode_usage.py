"""Card #1445 — opencode Go usage reader.

Synthetic fixtures only. The timer file shape is DM 104684
{provider, source, read_at, response} with the live USAGE-JSON windows
at response.usage.{rolling,weekly,monthly}.{status,percent,resetsAt}
(DM 104655). No real /var/lib/swarph-usage file and no credential.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from swarph_cli.commands import usage as usage_cmd
from swarph_cli.usage import (
    SYNC_CRON_LINE,
    SYNC_TIMER_EXECSTART,
    collect_rows,
    load_pool_map,
    render_table,
)
from swarph_cli.usage import opencode as opencode_reader

NOW = datetime(2026, 11, 23, 6, 0, 0, tzinfo=timezone.utc)

# Obvious fakes. Never a real key.
_SECRET_A = "sk-PROBE-SYNTHETIC-opencode-aaa"
_SECRET_B = "tok-probe-synthetic-opencode-bbb"


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(percent, resets_at, *, status="ok", **extra) -> dict:
    entry = {"status": status, "percent": percent, "resetsAt": resets_at}
    entry.update(extra)
    return entry


def _timer_body(*, read_at: str, rolling=5.0, weekly=12.5, monthly=68.0,
                resets=None, extra=None) -> dict:
    if resets is None:
        resets = {
            "rolling": _iso(NOW + timedelta(hours=5)),
            "weekly": _iso(NOW + timedelta(days=6)),
            "monthly": _iso(NOW + timedelta(days=20)),
        }
    body = {
        "provider": "opencode-go",
        "source": "go usage api",
        "read_at": read_at,
        "response": {
            "usage": {
                "rolling": _window(rolling, resets["rolling"]),
                "weekly": _window(weekly, resets["weekly"]),
                "monthly": _window(monthly, resets["monthly"]),
            }
        },
    }
    if extra:
        body.update(extra)
    return body


def _write_timer(path: Path, body: dict, *, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_three_windows_use_body_read_at_not_file_mtime(tmp_path):
    """read_at is the file body's read_at. A fresh mtime must not win."""
    read_at = _iso(NOW - timedelta(minutes=30))
    timer = _write_timer(
        tmp_path / "opencode-go.json",
        _timer_body(read_at=read_at),
        mtime=NOW.timestamp(),
    )
    usage_dir = tmp_path / "usage"
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=usage_dir)
    assert result is not None
    assert set(result["windows"]) == {"rolling", "weekly", "monthly"}
    assert result["windows"]["monthly"]["used_percentage"] == 68.0
    assert result["windows"]["rolling"]["used_percentage"] == 5.0
    written = json.loads((usage_dir / "opencode.json").read_text())
    assert written["provider"] == "opencode"
    assert written["read_at"] == read_at
    # File mtime is NOW. Using it would stamp the sync moment, age ~0.
    assert written["read_at"] != _iso(NOW)
    assert "mtime" in written["source"]
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    by_window = {r["window"]: r for r in rows if r["pool"] == "opencode"}
    assert set(by_window) == {"rolling", "weekly", "monthly"}
    assert by_window["weekly"]["used_pct"] == 12.5
    assert by_window["weekly"]["status"] == "ok"
    assert by_window["weekly"]["age_h"] == 0.5
    text = render_table(rows)
    for name in ("rolling", "weekly", "monthly"):
        assert name in text


def test_missing_percent_is_not_a_fresh_zero(tmp_path):
    """A window with no numeric percent is omitted, never stored as 0."""
    body = _timer_body(read_at=_iso(NOW - timedelta(minutes=10)))
    body["response"]["usage"]["rolling"].pop("percent")
    body["response"]["usage"]["weekly"]["percent"] = None
    body["response"]["usage"]["monthly"]["percent"] = "68"
    # A real zero on a fourth copy must still be distinguishable: rolling
    # is the missing one. monthly's string is not coerced.
    timer = _write_timer(tmp_path / "opencode-go.json", body, mtime=1000.0)
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage")
    assert result is None
    assert not (tmp_path / "usage" / "opencode.json").exists()

    # One good window beside two bad ones: only the good window is written.
    body["response"]["usage"]["weekly"]["percent"] = 12.5
    _write_timer(tmp_path / "opencode-go.json", body, mtime=1000.0)
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage")
    assert result is not None
    assert set(result["windows"]) == {"weekly"}
    text = (tmp_path / "usage" / "opencode.json").read_text(encoding="utf-8")
    assert "rolling" not in text
    assert "monthly" not in text
    assert '"used_percentage": 0' not in text
    rows = collect_rows(usage_dir=tmp_path / "usage",
                        pool_map=load_pool_map(tmp_path / "usage"),
                        now=NOW, snapshot={"readings": {}})
    opencode = [r for r in rows if r["pool"] == "opencode"]
    assert [r["window"] for r in opencode] == ["weekly"]
    assert opencode[0]["used_pct"] == 12.5
    assert all(r["used_pct"] != 0 for r in opencode)


def test_bool_percent_is_not_a_number(tmp_path):
    body = _timer_body(read_at=_iso(NOW - timedelta(minutes=10)))
    body["response"]["usage"]["rolling"]["percent"] = True
    body["response"]["usage"]["weekly"]["percent"] = False
    timer = _write_timer(tmp_path / "opencode-go.json", body, mtime=1000.0)
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage")
    assert set(result["windows"]) == {"monthly"}
    assert result["windows"]["monthly"]["used_percentage"] == 68.0


def test_nonfinite_percent_is_not_written(tmp_path):
    """NaN and Infinity are omitted: not stored, not a fresh zero, not fresher."""
    body_read = _iso(NOW - timedelta(hours=10))
    body = _timer_body(read_at=body_read)
    body["response"]["usage"]["rolling"]["percent"] = float("nan")
    body["response"]["usage"]["weekly"]["percent"] = float("inf")
    body["response"]["usage"]["monthly"]["percent"] = float("-inf")
    timer = _write_timer(tmp_path / "opencode-go.json", body,
                         mtime=NOW.timestamp())
    usage_dir = tmp_path / "usage"
    assert opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=usage_dir) is None
    assert not (usage_dir / "opencode.json").exists()
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    opencode = [r for r in rows if r["pool"] == "opencode"]
    assert len(opencode) == 1
    assert opencode[0]["status"] == "missing"
    assert opencode[0]["used_pct"] is None
    assert opencode[0]["age_h"] is None
    assert opencode[0]["read_at"] is None
    # A fresh file mtime must not surface as a current row.
    assert "0m" not in render_table(rows).split("opencode")[-1]

    # One finite window beside NaN and Infinity. The non-finite windows
    # stay out; the finite one keeps the stale body read_at.
    body["response"]["usage"]["weekly"]["percent"] = 12.5
    _write_timer(timer, body, mtime=NOW.timestamp())
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=usage_dir)
    assert result is not None
    assert set(result["windows"]) == {"weekly"}
    assert result["windows"]["weekly"]["used_percentage"] == 12.5
    assert math.isfinite(result["windows"]["weekly"]["used_percentage"])
    text = (usage_dir / "opencode.json").read_text(encoding="utf-8")
    assert "NaN" not in text
    assert "Infinity" not in text
    assert "rolling" not in text
    assert "monthly" not in text
    assert '"used_percentage": 0' not in text
    written = json.loads(text)
    assert written["read_at"] == body_read
    assert written["read_at"] != _iso(NOW)
    assert set(written["windows"]) == {"weekly"}
    stored = written["windows"]["weekly"]["used_percentage"]
    assert stored == 12.5
    assert math.isfinite(stored)
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    opencode = [r for r in rows if r["pool"] == "opencode"]
    assert [r["window"] for r in opencode] == ["weekly"]
    assert opencode[0]["used_pct"] == 12.5
    assert math.isfinite(opencode[0]["used_pct"])
    assert opencode[0]["used_pct"] != 0
    assert opencode[0]["read_at"] == body_read
    assert opencode[0]["age_h"] == 10.0
    assert opencode[0]["status"] == "stale"
    table = render_table(rows)
    opencode_lines = [line for line in table.splitlines() if "opencode" in line]
    assert opencode_lines
    assert all("0m" not in line for line in opencode_lines)
    assert all("STALE" in line for line in opencode_lines)
    assert "10.0h" in table


def test_explicit_zero_percent_is_kept(tmp_path):
    """A present 0 is a real reading, not the missing-field stand-in."""
    body = _timer_body(read_at=_iso(NOW - timedelta(minutes=10)),
                       rolling=0, weekly=0.0, monthly=0)
    timer = _write_timer(tmp_path / "opencode-go.json", body, mtime=1000.0)
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage")
    assert result["windows"]["rolling"]["used_percentage"] == 0.0
    assert result["windows"]["weekly"]["used_percentage"] == 0.0


def test_missing_read_at_writes_nothing(tmp_path):
    """No body read_at: do not stamp mtime or a nested fresh clock."""
    fresh = _iso(NOW)
    body = _timer_body(read_at=fresh)
    body.pop("read_at")
    body["response"]["read_at"] = fresh
    timer = _write_timer(tmp_path / "opencode-go.json", body,
                         mtime=NOW.timestamp())
    assert opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage") is None
    assert not (tmp_path / "usage" / "opencode.json").exists()
    rows = collect_rows(usage_dir=tmp_path / "usage",
                        pool_map=load_pool_map(tmp_path / "usage"),
                        now=NOW, snapshot={"readings": {}})
    opencode = [r for r in rows if r["pool"] == "opencode"]
    assert len(opencode) == 1
    assert opencode[0]["status"] == "missing"
    assert opencode[0]["age_h"] is None
    assert "0m" not in render_table(rows).split("opencode")[-1]


def test_unparseable_read_at_writes_nothing(tmp_path):
    body = _timer_body(read_at="not-a-timestamp")
    timer = _write_timer(tmp_path / "opencode-go.json", body, mtime=NOW.timestamp())
    assert opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage") is None
    assert not (tmp_path / "usage" / "opencode.json").exists()


def test_stale_reading_is_not_shown_fresher_than_read_at(tmp_path):
    """A 10h-old body read_at stays 10h and STALE, even if the file is new
    and a nested read_at claims the table clock.
    """
    body_read = _iso(NOW - timedelta(hours=10))
    body = _timer_body(read_at=body_read)
    body["response"]["read_at"] = _iso(NOW)
    timer = _write_timer(tmp_path / "opencode-go.json", body,
                         mtime=NOW.timestamp())
    usage_dir = tmp_path / "usage"
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=usage_dir)
    assert result is not None
    written = json.loads((usage_dir / "opencode.json").read_text())
    assert written["read_at"] == body_read
    assert written["read_at"] != _iso(NOW)
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    opencode = [r for r in rows if r["pool"] == "opencode"]
    assert {r["window"] for r in opencode} == {"rolling", "weekly", "monthly"}
    for row in opencode:
        assert row["read_at"] == body_read
        assert row["age_h"] == 10.0
        assert row["stale"] is True
        assert row["status"] == "stale"
        assert row["age_h"] > 6
    text = render_table(rows)
    assert "STALE" in text
    assert "10.0h" in text
    # The fresh mtime / nested clock would have rendered as minutes.
    opencode_lines = [line for line in text.splitlines() if "opencode" in line]
    assert opencode_lines
    assert all("0m" not in line for line in opencode_lines)
    assert all("STALE" in line for line in opencode_lines)


def test_nested_usage_json_body_still_maps_windows(tmp_path):
    """response holds the USAGE-JSON {read_at, response.usage...}."""
    outer = _iso(NOW - timedelta(hours=8))
    body = {
        "provider": "opencode",
        "source": "timer",
        "read_at": outer,
        "response": {
            "read_at": _iso(NOW),
            "response": {
                "usage": {
                    "rolling": _window(1, _iso(NOW + timedelta(hours=4))),
                    "weekly": _window(2, _iso(NOW + timedelta(days=2))),
                    "monthly": _window(3, 1795618145),
                }
            },
        },
    }
    timer = _write_timer(tmp_path / "opencode-go.json", body,
                         mtime=NOW.timestamp())
    usage_dir = tmp_path / "usage"
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=usage_dir)
    assert set(result["windows"]) == {"rolling", "weekly", "monthly"}
    assert result["windows"]["monthly"]["resets_at"].endswith("Z")
    written = json.loads((usage_dir / "opencode.json").read_text())
    assert written["read_at"] == outer
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    monthly = [r for r in rows if r["pool"] == "opencode"
               and r["window"] == "monthly"][0]
    assert monthly["age_h"] == 8.0
    assert monthly["status"] == "stale"


def test_missing_resets_at_keeps_the_percent(tmp_path):
    body = _timer_body(read_at=_iso(NOW - timedelta(minutes=15)))
    body["response"]["usage"]["rolling"].pop("resetsAt")
    timer = _write_timer(tmp_path / "opencode-go.json", body, mtime=1000.0)
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage")
    assert result["windows"]["rolling"]["used_percentage"] == 5.0
    assert result["windows"]["rolling"]["resets_at"] is None


def test_missing_file_writes_nothing(tmp_path):
    assert opencode_reader.write_opencode_json(
        usage_file=tmp_path / "nope.json",
        usage_dir=tmp_path / "usage") is None
    assert not (tmp_path / "usage" / "opencode.json").exists()


def test_credential_fields_never_reach_opencode_json(tmp_path):
    body = _timer_body(
        read_at=_iso(NOW - timedelta(minutes=5)),
        extra={"api_key": _SECRET_A, "source": _SECRET_B},
    )
    body["response"]["token"] = _SECRET_A
    body["response"]["usage"]["note"] = _SECRET_B
    body["response"]["usage"]["rolling"]["secret"] = _SECRET_A
    timer = _write_timer(tmp_path / "opencode-go.json", body, mtime=1000.0)
    result = opencode_reader.write_opencode_json(
        usage_file=timer, usage_dir=tmp_path / "usage")
    assert result is not None
    text = (tmp_path / "usage" / "opencode.json").read_text(encoding="utf-8")
    assert _SECRET_A not in text
    assert _SECRET_B not in text
    assert "api_key" not in text
    assert "token" not in text


def test_flag_overrides_env_and_default(tmp_path, monkeypatch):
    monkeypatch.delenv(opencode_reader.ENV_USAGE_FILE, raising=False)
    assert opencode_reader.opencode_usage_path(None) == Path(
        opencode_reader.DEFAULT_USAGE_FILE)
    env_path = tmp_path / "from-env.json"
    flag_path = tmp_path / "from-flag.json"
    monkeypatch.setenv(opencode_reader.ENV_USAGE_FILE, str(env_path))
    assert opencode_reader.opencode_usage_path(None) == env_path
    assert opencode_reader.opencode_usage_path(flag_path) == flag_path

    _write_timer(flag_path, _timer_body(read_at=_iso(NOW - timedelta(minutes=5))),
                 mtime=1000.0)
    _write_timer(env_path, _timer_body(read_at=_iso(NOW - timedelta(hours=2)),
                                      rolling=99),
                 mtime=1000.0)
    rc_flag = usage_cmd.run_usage([
        "sync", "--state-root", str(tmp_path / "flag-root"),
        "--codex-sessions", str(tmp_path / "no-sessions"),
        "--opencode-file", str(flag_path),
    ])
    assert rc_flag == 0
    written = json.loads(
        (tmp_path / "flag-root" / "usage" / "opencode.json").read_text())
    assert written["windows"]["rolling"]["used_percentage"] == 5.0

    rc_env = usage_cmd.run_usage([
        "sync", "--state-root", str(tmp_path / "env-root"),
        "--codex-sessions", str(tmp_path / "no-sessions"),
    ])
    assert rc_env == 0
    env_written = json.loads(
        (tmp_path / "env-root" / "usage" / "opencode.json").read_text())
    assert env_written["windows"]["rolling"]["used_percentage"] == 99.0


def _assert_absolute_schedule(text):
    """Cron and ExecStart name swarph by an absolute path."""
    assert "command -v swarph" in text
    assert SYNC_CRON_LINE in text
    assert SYNC_TIMER_EXECSTART in text
    for raw in text.splitlines():
        body = raw.strip()
        if body.startswith("#"):
            body = body[1:].strip()
        if body.startswith("0 ") and "usage sync" in body:
            command = body.split()[5]
            assert command.startswith("/"), body
        if "ExecStart=" in body:
            value = body.split("ExecStart=", 1)[1].lstrip()
            assert value.startswith("/"), body


def test_sync_help_documents_the_cron_line(capsys):
    assert SYNC_CRON_LINE == (
        "0 */4 * * * /home/ubuntu/.local/bin/swarph usage sync")
    assert SYNC_TIMER_EXECSTART == (
        "ExecStart=/home/ubuntu/.local/bin/swarph usage sync")
    import swarph_cli.usage as usage_mod
    for text in (usage_cmd.__doc__, usage_mod.__doc__):
        _assert_absolute_schedule(text)
    assert "6h" in usage_mod.__doc__
    assert "OnCalendar=*-*-* 00/4:00:00" in usage_mod.__doc__
    try:
        usage_cmd.run_usage(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("swarph usage --help did not exit")
    help_text = capsys.readouterr().out
    _assert_absolute_schedule(help_text)
    assert "6h STALE" in help_text


def test_table_shows_opencode_rows_from_cli(tmp_path, capsys):
    read_at = _iso(NOW - timedelta(hours=10))
    timer = _write_timer(tmp_path / "opencode-go.json",
                         _timer_body(read_at=read_at),
                         mtime=NOW.timestamp())
    rc = usage_cmd.run_usage([
        "sync", "--state-root", str(tmp_path / "state"),
        "--codex-sessions", str(tmp_path / "no-sessions"),
        "--opencode-file", str(timer),
    ])
    assert rc == 0
    sync_out = capsys.readouterr().out
    assert "opencode.json" in sync_out
    assert "rolling" in sync_out and "weekly" in sync_out and "monthly" in sync_out
    # Show uses wall clock, so pin the file and read rows at NOW instead.
    usage_dir = tmp_path / "state" / "usage"
    rows = collect_rows(usage_dir=usage_dir, pool_map=load_pool_map(usage_dir),
                        now=NOW, snapshot={"readings": {}})
    text = render_table(rows)
    assert "opencode" in text
    for name in ("rolling", "weekly", "monthly"):
        assert name in text
    assert "STALE" in text
    assert "10.0h" in text
