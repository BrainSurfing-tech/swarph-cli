"""#729 — arm-check refuses stale/missing; status exposes delivery_gap."""
from __future__ import annotations

import json
import os
import time

import pytest

from swarph_cli.commands import mesh, monitor


@pytest.fixture(autouse=True)
def _clean_composer(monkeypatch):
    monkeypatch.setattr(mesh, "_composer_state", lambda t: "clear")


def _env(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


def _run(args, tmp_path):
    return monitor.run_monitor(args[:1] + ["--state-dir", str(tmp_path)] + args[1:])


def _write_pidfile(tmp_path, record):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "monitor.pid").write_text(json.dumps(record), encoding="utf-8")


def _own_record(**over):
    rec = {
        "pid": os.getpid(),
        "self": "lab-ovh",
        "sinks": ["pull"],
        "started_at": 0.0,
        "cmdline": mesh._proc_cmdline(os.getpid()),
        "emits_heartbeat": True,
        "poll_s": 30,
    }
    rec.update(over)
    return rec


def test_arm_check_refuses_missing(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    missing = tmp_path / "no-such-inbox.log"
    rc = _run(["arm-check", "--path", str(missing)], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSE" in err and "missing" in err.lower()
    assert str(missing) in err


def test_arm_check_refuses_stale(monkeypatch, tmp_path, capsys):
    """CAN-FAIL: deliberately stale file must go RED — card #729 accept."""
    _env(monkeypatch)
    stale = tmp_path / "stale-inbox.log"
    stale.write_text("old\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    rc = _run(["arm-check", "--path", str(stale), "--max-age-s", "900"], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2, f"expected refuse, got rc={rc} err={err!r}"
    assert "REFUSE" in err and "stale" in err.lower()
    assert str(stale) in err


def test_arm_check_ok_fresh(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    fresh = tmp_path / "fresh-inbox.log"
    fresh.write_text("now\n", encoding="utf-8")
    rc = _run(["arm-check", "--path", str(fresh), "--max-age-s", "900"], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
    assert str(fresh) in out


def test_arm_check_refuses_zero_max_age(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    fresh = tmp_path / "fresh-inbox.log"
    fresh.write_text("now\n", encoding="utf-8")
    rc = _run(["arm-check", "--path", str(fresh), "--max-age-s", "0"], tmp_path)
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSE" in err


def test_status_exposes_delivery_gap(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())
    (tmp_path / "cursor.json").write_text(
        json.dumps({"last_msg_id": 32174, "pending_channel_posts": []}),
        encoding="utf-8",
    )
    (tmp_path / "ledgers.json").write_text(
        json.dumps({
            "pull": {
                "last_delivered_id": 32023,
                "last_delivery_at": 1.0,
                "consecutive_failures": 0,
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / "inbox.log").write_text("", encoding="utf-8")
    rc = _run(["status", "--json"], tmp_path)
    data = json.loads(capsys.readouterr().out)
    assert data["delivery_gap"] == 151
    assert data["sinks"][0]["delivery_gap"] == 151
    assert rc in (0, 1)


def test_status_human_prints_delivery_gap_line(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())
    (tmp_path / "cursor.json").write_text(
        json.dumps({"last_msg_id": 100, "pending_channel_posts": []}),
        encoding="utf-8",
    )
    (tmp_path / "ledgers.json").write_text(
        json.dumps({
            "pull": {
                "last_delivered_id": 90,
                "last_delivery_at": 1.0,
                "consecutive_failures": 0,
            }
        }),
        encoding="utf-8",
    )
    (tmp_path / "inbox.log").write_text("", encoding="utf-8")
    _run(["status"], tmp_path)
    out = capsys.readouterr().out
    assert "delivery_gap: 10" in out
