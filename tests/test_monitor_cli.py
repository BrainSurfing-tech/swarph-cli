"""Card #122 — `swarph monitor` start / status / stop.

The surface exists so a cell can PULL ("do I have DMs?") instead of depending on a
push sink being alive. The mesh has gone silently deaf twice by depending on push:
a tmux crash kills the wake Monitor, SessionStart drains but does not re-arm, and
"no wake arrived" is indistinguishable from "no mail arrived". A pull check run BY
the cell lives one layer above tmux and cannot die with it.

That only works if `start` is safe to call unconditionally from a hook and `status`
never converts an ABSENCE into EVIDENCE. Both are asserted here.

Run: .venv/bin/python -m pytest tests/test_monitor_cli.py -v
"""
from __future__ import annotations

import json
import os

from swarph_cli.commands import mesh, monitor


def _env(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.delenv("SWARPH_TMUX_TARGET", raising=False)


def _dm(msg_id, *, frm="droplet", read_at=None):
    return {"id": msg_id, "from_node": frm, "kind": "question",
            "content": "ping", "read_at": read_at}


def _serve(monkeypatch, *dms):
    monkeypatch.setattr(
        mesh, "_http_get_json", lambda u, t, **k: (200, {"messages": list(dms)})
    )


def _run(args, tmp_path):
    return monitor.run_monitor(args[:1] + ["--state-dir", str(tmp_path)] + args[1:])


def _write_pidfile(tmp_path, record):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "monitor.pid").write_text(json.dumps(record), encoding="utf-8")


def _own_record(**over):
    rec = {
        "pid": os.getpid(),
        "self": "lab-ovh",
        "sinks": ["pull"],
        "started_at": 0.0,
        "cmdline": mesh._proc_cmdline(os.getpid()),
    }
    rec.update(over)
    return rec


# ── start: safe to call unconditionally from a hook ──────────────────────────

def test_start_is_quiet_and_zero_when_already_running(monkeypatch, tmp_path, capsys):
    """A SessionStart hook runs this every session. It must cost nothing and say
    nothing on the already-running path — no banner, no re-poll."""
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())
    monkeypatch.setattr(mesh, "_http_get_json", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("already-running start must NOT poll")))

    rc = _run(["start", "--once"], tmp_path)

    out = capsys.readouterr()
    assert rc == 0
    assert out.out == "", f"the already-running path must be silent, got {out.out!r}"


def test_start_reclaims_a_stale_pidfile_and_logs_the_reclaim(monkeypatch, tmp_path, capsys):
    """Silent reclaim is fine right up until the day it was not actually stale."""
    _env(monkeypatch)
    _serve(monkeypatch)
    _write_pidfile(tmp_path, _own_record(pid=4000000, cmdline="swarph monitor start"))

    rc = _run(["start", "--once"], tmp_path)

    err = capsys.readouterr().err
    assert rc == 0
    assert "4000000" in err and "stale" in err.lower()
    assert json.loads((tmp_path / "monitor.pid").read_text())["pid"] == os.getpid()


def test_start_does_not_adopt_a_live_foreign_pid(monkeypatch, tmp_path, capsys):
    """Adopting a foreign PID is how `stop` ends up killing something unrelated."""
    _env(monkeypatch)
    _serve(monkeypatch)
    _write_pidfile(tmp_path, _own_record(cmdline="/usr/bin/definitely-not-swarph --serve"))

    rc = _run(["start", "--once"], tmp_path)

    err = capsys.readouterr().err
    assert rc == 0
    assert "not" in err.lower() and str(os.getpid()) in err
    assert json.loads((tmp_path / "monitor.pid").read_text())["pid"] == os.getpid(), (
        "the pidfile is reclaimed for US; the foreign process is left alone"
    )


def test_stop_refuses_to_kill_a_foreign_pid(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record(cmdline="/usr/bin/definitely-not-swarph --serve"))
    killed = []
    monkeypatch.setattr(mesh, "_terminate", killed.append)

    rc = _run(["stop"], tmp_path)

    assert rc == 2
    assert killed == [], "stop must never signal a process it cannot prove is ours"
    assert "not" in capsys.readouterr().err.lower()


def test_stop_signals_our_monitor_and_says_deliveries_are_abandoned(
    monkeypatch, tmp_path, capsys
):
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())
    killed = []
    monkeypatch.setattr(mesh, "_terminate", killed.append)

    rc = _run(["stop"], tmp_path)

    assert rc == 0
    assert killed == [os.getpid()]
    assert "abandon" in capsys.readouterr().out.lower(), (
        "abandoning owed deliveries is fine; doing it without saying so is not"
    )
    assert not (tmp_path / "monitor.pid").exists()


# ── the webhook gate ─────────────────────────────────────────────────────────

def test_webhook_sink_exits_non_zero_naming_the_gate(monkeypatch, tmp_path, capsys):
    """HELD by the commander. A hold that silently no-ops rots into a phantom
    capability — someone configures it, sees no error, and believes it delivers."""
    _env(monkeypatch)
    rc = _run(["start", "--deliver", "webhook:https://example.test/h", "--once"], tmp_path)

    err = capsys.readouterr().err.lower()
    assert rc != 0, "a held sink must FAIL, not be quietly ignored"
    assert "held" in err
    assert not (tmp_path / "monitor.pid").exists()


# ── status: exit codes carry the answer ──────────────────────────────────────

def test_status_exits_2_when_not_running(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    assert _run(["status"], tmp_path) == 2
    assert "not running" in capsys.readouterr().out.lower()


def test_status_exits_0_when_nothing_pending(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())
    assert _run(["status"], tmp_path) == 0


def test_status_exits_1_when_dms_are_pending(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _serve(monkeypatch, _dm(101), _dm(102, frm="watchtower"))
    _run(["start", "--once"], tmp_path)
    capsys.readouterr()                      # drop start's own observation log

    rc = _run(["status"], tmp_path)
    out = capsys.readouterr().out

    assert rc == 1
    assert "2" in out and "unread" in out.lower()
    assert "droplet" in out and "watchtower" in out


def test_status_brief_is_one_line_when_there_is_something(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _serve(monkeypatch, _dm(201), _dm(202, frm="watchtower"))
    _run(["start", "--once"], tmp_path)
    capsys.readouterr()                      # drop start's own observation log

    rc = _run(["status", "--brief"], tmp_path)
    out = capsys.readouterr().out

    assert rc == 1
    assert len(out.strip().splitlines()) == 1, f"--brief must be ONE line, got {out!r}"
    assert "unread" in out and "swarph mesh inbox" in out


def test_status_brief_is_silent_when_there_is_nothing(monkeypatch, tmp_path, capsys):
    """It lives in a SessionStart hook. Silence is the whole point."""
    _env(monkeypatch)
    _write_pidfile(tmp_path, _own_record())

    rc = _run(["status", "--brief"], tmp_path)

    assert rc == 0
    assert capsys.readouterr().out == ""


# ── the defect this card removes: an absence that reads as evidence ──────────

def test_status_under_none_refuses_to_report_zero_unread(monkeypatch, tmp_path, capsys):
    """`none` keeps NO ledger, so it has nothing to subtract. Printing "0 unread"
    would recreate the exact bug being fixed: an absence that reads as evidence."""
    _env(monkeypatch)
    _serve(monkeypatch, _dm(301))
    _run(["start", "--deliver", "none", "--once"], tmp_path)
    capsys.readouterr()                      # drop start's own observation log

    rc = _run(["status"], tmp_path)
    out = capsys.readouterr().out

    assert rc == 0
    assert "0 unread" not in out
    assert "cannot" in out.lower() and "unread" in out.lower()


def test_status_brief_under_none_still_says_it_cannot_tell(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _serve(monkeypatch, _dm(302))
    _run(["start", "--deliver", "none", "--once"], tmp_path)
    capsys.readouterr()                      # drop start's own observation log

    rc = _run(["status", "--brief"], tmp_path)
    out = capsys.readouterr().out

    assert rc == 0
    assert out.strip(), "silence here would read as '0 unread' — the defect itself"
    assert "cannot" in out.lower()


def test_status_json_marks_unread_as_not_reportable_under_none(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _serve(monkeypatch, _dm(303))
    _run(["start", "--deliver", "none", "--once"], tmp_path)
    capsys.readouterr()                      # drop start's own observation log

    rc = _run(["status", "--json"], tmp_path)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["unread_reportable"] is False
    assert payload["observation_cursor"] == 303
    assert payload["sinks"] == [] or all(not s["keeps_ledger"] for s in payload["sinks"])


def test_status_json_reports_per_sink_lag(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _serve(monkeypatch, _dm(401))
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: False)
    _run(["start", "--deliver", "pull", "--deliver", "tmux:gone:0.0", "--once"], tmp_path)
    capsys.readouterr()                      # drop start's own observation log

    rc = _run(["status", "--json"], tmp_path)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    by_name = {s["name"]: s for s in payload["sinks"]}
    assert by_name["pull"]["pending"] == 1
    assert by_name["tmux:gone:0.0"]["pending"] == 1
    assert by_name["tmux:gone:0.0"]["consecutive_failures"] == 1


def test_status_flags_a_ledger_that_does_not_exist_yet(monkeypatch, tmp_path, capsys):
    """Keying a ledger by the sink STRING means renaming a pane creates a fresh
    ledger that replays from zero. Make that visible rather than mysterious."""
    _env(monkeypatch)
    _serve(monkeypatch, _dm(501))
    _run(["start", "--deliver", "none", "--once"], tmp_path)
    _write_pidfile(tmp_path, _own_record(sinks=["tmux:brand-new:0.0"]))
    capsys.readouterr()                      # drop start's own observation log

    _run(["status", "--json"], tmp_path)
    payload = json.loads(capsys.readouterr().out)

    assert payload["sinks"][0]["ledger_missing"] is True


# ── the deprecated alias peers are running RIGHT NOW ─────────────────────────

def test_mesh_sidecar_alias_still_works_and_warns_on_stderr(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    _serve(monkeypatch, _dm(601))
    woke = []
    monkeypatch.setattr(mesh, "_tmux_wake", lambda t: woke.append(t) or True)

    rc = mesh.run_mesh([
        "sidecar", "--tmux-target", "lab:0.0", "--state-dir", str(tmp_path),
        "--wake-min-interval", "0", "--once",
    ])
    out = capsys.readouterr()

    assert rc == 0
    assert woke == ["lab:0.0"], "peers are running this right now; it must not break"
    assert "deprecat" in out.err.lower() and "monitor start" in out.err
    assert "deprecat" not in out.out.lower(), (
        "the notice goes to STDERR — stdout is a sink"
    )


def test_monitor_verb_is_registered(monkeypatch):
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["monitor"] == "swarph_cli.commands.monitor.run_monitor"
