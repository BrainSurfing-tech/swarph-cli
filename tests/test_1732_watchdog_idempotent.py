"""Card #1732: idempotent self-outage minting, fail-closed listing, and single recovery note."""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from swarph_cli.scripts import wake_watchdog
from swarph_cli.scripts.wake_watchdog import (
    REALERT_SECONDS,
    _handle_self_outage,
    _list_open_dead_wake_rows,
    _note_argv,
    _parse_minted_row_id,
    _realert_note_argv,
)

NOW = 1_700_000_000.0


def _yaml(config: Path, name: str = "lab-ovh", role: str = "lab") -> None:
    directory = config / "swarph" / "cells"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{role}.yaml").write_text(
        "schema_version: v1\n"
        f"name: {name}\n"
        f"role: {role}\n"
        f'cwd: "{Path.cwd().as_posix()}"\n'
        "provider: claude\n",
        encoding="utf-8",
    )


def _cell(root: Path, name: str = "lab-ovh", *, alive: bool = False,
          proc_root: Path | None = None) -> Path:
    inbox = root / name / "mesh-sidecar" / "inbox.log"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("", encoding="utf-8")
    if alive:
        (inbox.parent / "channel_heartbeat.json").write_text(
            json.dumps({"pid": 4242, "ts": time.time()}), encoding="utf-8")
        if proc_root is not None:
            (proc_root / "4242").mkdir(parents=True, exist_ok=True)
            (proc_root / "4242" / "stat").write_text(
                "4242 (child) S 111 0 0\n", encoding="utf-8")
            (proc_root / "111").mkdir(parents=True, exist_ok=True)
            (proc_root / "111" / "cmdline").write_bytes(
                b"\0".join(a.encode() for a in ["claude", "--name", "lab"]) + b"\0")
    return inbox


def _fake_swarph(tmp_path: Path, gateway_path: Path) -> Path:
    """Stateful fake `swarph` CLI executable backed by a JSON gateway state file."""
    stub = tmp_path / "swarph"
    stub.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json
        import os
        import sys
        from pathlib import Path

        gw_path = Path({repr(str(gateway_path))})
        gw = json.loads(gw_path.read_text(encoding="utf-8"))
        args = sys.argv[1:]
        gw.setdefault("calls", []).append(args)

        def save():
            gw_path.write_text(json.dumps(gw), encoding="utf-8")

        def flag_val(flag, default=""):
            for i, a in enumerate(args):
                if a == flag and i + 1 < len(args):
                    return args[i + 1]
                if a.startswith(flag + "="):
                    return a.split("=", 1)[1]
            return default

        if args[:3] == ["board", "obligations", "list"]:
            save()
            override = gw.get("list_override")
            if override is not None:
                if override.get("stdout"):
                    sys.stdout.write(override["stdout"])
                if override.get("stderr"):
                    sys.stderr.write(override["stderr"])
                raise SystemExit(int(override.get("rc", 0)))
            print(json.dumps({{"obligations": gw.get("obligations", [])}}))
            raise SystemExit(0)

        if args[:4] == ["board", "cards", "ask", "729"]:
            next_id = int(gw.get("next_id", 1))
            gw["next_id"] = next_id + 1
            row = {{
                "id": next_id,
                "card_id": 729,
                "holder": flag_val("--holder"),
                "status": "open",
                "created_by": flag_val("--as"),
                "accept": flag_val("--accept"),
                "taken_at": "2026-11-21T00:00:00Z",
            }}
            gw.setdefault("obligations", []).append(row)
            save()
            print(f"minted #{{next_id}}")
            raise SystemExit(int(gw.get("ask_rc", 0)))

        if args[:4] == ["board", "cards", "say", "729"]:
            gw.setdefault("notes", []).append({{
                "kind": flag_val("--kind"),
                "to": flag_val("--to"),
                "as": flag_val("--as"),
                "content": flag_val("--content"),
            }})
            save()
            print("posted")
            raise SystemExit(int(gw.get("say_rc", 0)))

        if args[:3] == ["board", "obligations", "withdraw"]:
            oid = args[3]
            for row in gw.get("obligations", []):
                if str(row.get("id")) == str(oid):
                    row["status"] = "withdrawn"
            gw.setdefault("withdrawn", []).append(str(oid))
            save()
            print(f"withdrawn #{{oid}}")
            raise SystemExit(0)

        save()
        raise SystemExit(0)
    """), encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _base_env(tmp_path: Path, root: Path, state_path: Path, stub: Path,
              now: float) -> dict[str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(root),
        "WAKE_WATCHDOG_STATE": str(state_path),
        "WAKE_WATCHDOG_NOW": str(now),
        "WAKE_WATCHDOG_PROC": str(tmp_path / "proc"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SWARPH_BIN": str(stub),
    }
    env.pop("SWARPH_SELF", None)
    env.pop("MESH_GATEWAY_URL", None)
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_ask_commits_then_exits_1_for_70_passes_mints_exactly_one_row(tmp_path, monkeypatch):
    root = tmp_path / "state"
    _cell(root, "lab-ovh", alive=False)
    _yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    state_path.write_text(json.dumps({
        "lab-ovh": {"missing_since": NOW - 120.0},
    }), encoding="utf-8")
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({"obligations": [], "ask_rc": 1, "next_id": 1}), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)

    monkeypatch.setenv("SWARPH_STATE_ROOT", str(root))
    monkeypatch.setenv("WAKE_WATCHDOG_STATE", str(state_path))
    monkeypatch.setenv("WAKE_WATCHDOG_PROC", str(tmp_path / "proc"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SWARPH_BIN", str(stub))
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.setattr(wake_watchdog, "_listing", lambda _argv: "")

    argv = ["--as", "lab-ovh", "--escalate", "drop-on-meta-edge"]
    rcs: list[int] = []
    for i in range(70):
        monkeypatch.setenv("WAKE_WATCHDOG_NOW", str(NOW + i * 120.0))
        rcs.append(wake_watchdog.main(argv))

    assert rcs[0] == 1
    assert rcs[1] == 0
    assert all(rc == 0 for rc in rcs[1:])

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["lab-ovh"]["outage"] is True
    assert saved["lab-ovh"]["outage_row"] == 1
    assert saved["lab-ovh"]["alerted_at"] == NOW + 120.0

    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    assert len(gw["obligations"]) == 1
    ask_calls = [c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]]
    say_calls = [c for c in gw["calls"] if c[:4] == ["board", "cards", "say", "729"]]
    assert len(ask_calls) == 1
    assert len(say_calls) == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_67_preexisting_open_rows_recovery_sends_exactly_one_note(tmp_path):
    root = tmp_path / "state"
    proc_root = tmp_path / "proc"
    _cell(root, "lab-ovh", alive=True, proc_root=proc_root)
    _yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    state_path.write_text(json.dumps({
        "lab-ovh": {"outage": True, "alerted_at": NOW - 100.0, "missing_since": NOW - 200.0},
    }), encoding="utf-8")
    obligations = [
        {
            "id": row_id,
            "card_id": 729,
            "holder": "commander",
            "status": "open",
            "created_by": "lab-ovh",
            "accept": "WAKE-DEAD lab-ovh",
            "taken_at": "2026-11-21T00:00:00Z",
        }
        for row_id in range(6221, 6288)
    ]
    obligations.append({
        "id": 9999,
        "card_id": 729,
        "holder": "commander",
        "status": "open",
        "created_by": "other-cell",
        "accept": "WAKE-DEAD other-cell",
        "taken_at": "2026-11-21T00:00:00Z",
    })
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({"obligations": obligations}), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)
    env = _base_env(tmp_path, root, state_path, stub, NOW)
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
           "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"]

    first = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert first.returncode == 0
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    say_calls = [c for c in gw["calls"] if c[:4] == ["board", "cards", "say", "729"]]
    assert len(say_calls) == 1
    assert len(gw["notes"]) == 1
    note = gw["notes"][0]
    assert note["kind"] == "status"
    assert note["to"] == "commander"
    for row_id in range(6221, 6288):
        assert f"#{row_id}" in note["content"]
    assert "#9999" not in note["content"]

    second = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    third = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert second.returncode == 0
    assert third.returncode == 0
    gw_after = json.loads(gw_path.read_text(encoding="utf-8"))
    assert len([c for c in gw_after["calls"] if c[:4] == ["board", "cards", "say", "729"]]) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_realert_past_six_hours_adds_note_and_mints_nothing_new(tmp_path):
    root = tmp_path / "state"
    _cell(root, "lab-ovh", alive=False)
    _yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    alerted_at = 1000.0
    state_path.write_text(json.dumps({
        "lab-ovh": {
            "missing_since": 0.0,
            "outage": True,
            "alerted_at": alerted_at,
            "outage_row": 42,
        },
    }), encoding="utf-8")
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({
        "obligations": [{
            "id": 42,
            "card_id": 729,
            "holder": "commander",
            "status": "open",
            "created_by": "lab-ovh",
            "accept": "WAKE-DEAD lab-ovh",
            "taken_at": "2026-11-21T00:00:00Z",
        }],
    }), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)
    t1 = alerted_at + REALERT_SECONDS + 1.0
    env = _base_env(tmp_path, root, state_path, stub, t1)
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
           "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"]

    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    assert len(gw["obligations"]) == 1
    assert gw["obligations"][0]["id"] == 42
    assert [c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]] == []
    say_calls = [c for c in gw["calls"] if c[:4] == ["board", "cards", "say", "729"]]
    assert len(say_calls) == 1
    assert gw["notes"][0]["kind"] == "status"
    assert gw["notes"][0]["to"] == "commander"
    assert "#42" in gw["notes"][0]["content"]
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["lab-ovh"]["alerted_at"] == t1

    env["WAKE_WATCHDOG_NOW"] = str(t1 + 120.0)
    follow = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    assert follow.returncode == 0
    gw2 = json.loads(gw_path.read_text(encoding="utf-8"))
    assert [c for c in gw2["calls"] if c[:4] == ["board", "cards", "ask", "729"]] == []
    assert len([c for c in gw2["calls"] if c[:4] == ["board", "cards", "say", "729"]]) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
@pytest.mark.parametrize(
    "override",
    [
        {"rc": 1, "stdout": "", "stderr": "gateway timeout"},
        {"rc": 0, "stdout": "{not-json", "stderr": ""},
        {"rc": 0, "stdout": "", "stderr": ""},
        {"rc": 0, "stdout": "{}", "stderr": ""},
    ],
    ids=["rc1-error", "invalid-json", "empty-stdout", "missing-obligations-key"],
)
def test_failing_listing_mints_nothing_and_logs_why(tmp_path, override):
    root = tmp_path / "state"
    _cell(root, "lab-ovh", alive=False)
    _yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    state_path.write_text(json.dumps({
        "lab-ovh": {"missing_since": NOW - 120.0},
    }), encoding="utf-8")
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({
        "obligations": [],
        "list_override": override,
    }), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)
    env = _base_env(tmp_path, root, state_path, stub, NOW)

    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    assert [c for c in gw["calls"] if c[:4] == ["board", "cards", "ask", "729"]] == []
    assert gw["obligations"] == []
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["lab-ovh"].get("outage") is not True
    assert "not minting dead-wake row" in proc.stderr
    assert "obligations list" in proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_recovery_with_multiple_untaken_rows_withdraws_all_and_sends_one_note(tmp_path):
    root = tmp_path / "state"
    proc_root = tmp_path / "proc"
    _cell(root, "lab-ovh", alive=True, proc_root=proc_root)
    _yaml(tmp_path / "config")
    state_path = tmp_path / "wake.json"
    state_path.write_text(json.dumps({
        "lab-ovh": {"outage": True, "alerted_at": NOW - 100.0, "missing_since": NOW - 200.0},
    }), encoding="utf-8")
    gw_path = tmp_path / "gateway.json"
    gw_path.write_text(json.dumps({
        "obligations": [
            {
                "id": oid,
                "card_id": 729,
                "holder": "commander",
                "status": "open",
                "created_by": "lab-ovh",
                "accept": "WAKE-DEAD lab-ovh",
            }
            for oid in (101, 102, 103)
        ],
    }), encoding="utf-8")
    stub = _fake_swarph(tmp_path, gw_path)
    env = _base_env(tmp_path, root, state_path, stub, NOW)

    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0
    gw = json.loads(gw_path.read_text(encoding="utf-8"))
    say_calls = [c for c in gw["calls"] if c[:4] == ["board", "cards", "say", "729"]]
    assert len(say_calls) == 1
    assert len(gw["notes"]) == 1
    for oid in (101, 102, 103):
        assert f"#{oid}" in gw["notes"][0]["content"]
    assert gw["withdrawn"] == ["101", "102", "103"]


# ── Cross-platform unit tests (run on Windows and POSIX) ────────────────────


def test_parse_minted_row_id_variants():
    assert _parse_minted_row_id("minted #42\nmore") == 42
    assert _parse_minted_row_id("\n  #907 created\n") == 907
    assert _parse_minted_row_id("created without hash") is None
    assert _parse_minted_row_id("") is None


def test_note_argv_single_and_multi_row_formatting():
    when = datetime.datetime.fromtimestamp(
        NOW, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    single = _note_argv("swarph", "cell-a", "/tmp/tok", 42, when)
    assert f"cell-a DM wake recovered at {when}; dead-wake row #42 can be dismissed" in single
    assert single[-2:] == ["--token-file", "/tmp/tok"]

    multi = _note_argv("swarph", "cell-a", "", [10, 11, 12], when)
    assert (f"cell-a DM wake recovered at {when}; "
            "dead-wake rows #10, #11, #12 can be dismissed") in multi
    assert "--token-file" not in multi


def test_realert_note_argv_includes_row_and_token_file():
    argv = _realert_note_argv("swarph", "cell-a", "/tmp/tok", 55, "2026-11-21 00:00:00Z")
    assert argv[:6] == ["swarph", "board", "cards", "say", "729", "--kind"]
    content = argv[argv.index("--content") + 1]
    assert content.startswith("still open (row #55): cell-a DM wake dead since 2026-11-21 00:00:00Z")
    assert argv[-2:] == ["--token-file", "/tmp/tok"]


@pytest.mark.parametrize(
    "outcome",
    [
        OSError("missing binary"),
        subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
        subprocess.CompletedProcess([], 0, stdout="   ", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="{bad", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="[]", stderr=""),
        subprocess.CompletedProcess([], 0, stdout='{"obligations": "nope"}', stderr=""),
    ],
)
def test_list_open_dead_wake_rows_fails_closed(monkeypatch, capsys, outcome):
    def fake_run(argv, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(wake_watchdog.subprocess, "run", fake_run)
    assert _list_open_dead_wake_rows("swarph", "cell-a", "") is None
    assert capsys.readouterr().err.strip() != ""


def test_list_open_dead_wake_rows_filters_to_matching_rows(monkeypatch):
    payload = {
        "obligations": [
            {"id": 1, "card_id": 729, "holder": "commander", "status": "open",
             "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"},
            {"id": 2, "card_id": "729", "holder": "commander", "status": None,
             "created_by": "cell-a", "accept": "prefix WAKE-DEAD cell-a"},
            {"id": 3, "card_id": 730, "holder": "commander", "status": "open",
             "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"},
            {"id": 4, "card_id": 729, "holder": "cell-a", "status": "open",
             "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"},
            {"id": 5, "card_id": 729, "holder": "commander", "status": "closed",
             "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"},
            {"id": 6, "card_id": 729, "holder": "commander", "status": "open",
             "created_by": "other", "accept": "WAKE-DEAD cell-a"},
            {"id": 7, "card_id": 729, "holder": "commander", "status": "open",
             "created_by": "cell-a", "accept": "WAKE-DEAD other"},
            {"id": 0, "card_id": 729, "holder": "commander", "status": "open",
             "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"},
        ]
    }
    monkeypatch.setattr(
        wake_watchdog.subprocess, "run",
        lambda argv, **_kw: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr=""),
    )
    rows = _list_open_dead_wake_rows("swarph", "cell-a", "")
    assert [r["id"] for r in rows] == [1, 2]


def test_handle_self_outage_adopt_realert_and_fail_closed(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "wake.json"
    inbox = str(tmp_path / "cell-a" / "mesh-sidecar" / "inbox.log")

    # 1. Fail closed when listing fails
    monkeypatch.setattr(wake_watchdog, "_list_open_dead_wake_rows", lambda *_a: None)
    state: dict = {}
    assert _handle_self_outage("swarph", "cell-a", "", inbox, 100.0, state, state_path) is False
    assert state["cell-a"].get("outage") is not True
    assert "open-row listing failed" in capsys.readouterr().err

    # 2. Adoption when an existing open row is already present and outage is False
    calls: list[list[str]] = []
    monkeypatch.setattr(
        wake_watchdog, "_list_open_dead_wake_rows",
        lambda *_a: [{"id": 77, "card_id": 729, "holder": "commander",
                      "status": "open", "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"}],
    )
    monkeypatch.setattr(
        wake_watchdog.subprocess, "run",
        lambda argv, **_kw: (calls.append(list(argv)), subprocess.CompletedProcess(argv, 0, stdout="", stderr=""))[1],
    )
    assert _handle_self_outage("swarph", "cell-a", "", inbox, 200.0, state, state_path) is True
    assert calls == []
    assert state["cell-a"]["outage"] is True
    assert state["cell-a"]["outage_row"] == 77
    assert state["cell-a"]["alerted_at"] == 200.0
    assert "already open" in capsys.readouterr().err

    # 3. Re-alert when outage is already True sends a note referencing outage_row
    monkeypatch.setattr(
        wake_watchdog, "_list_open_dead_wake_rows",
        lambda *_a: [
            {"id": 60, "card_id": 729, "holder": "commander", "status": "open",
             "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"},
            {"id": 77, "card_id": 729, "holder": "commander", "status": "open",
             "created_by": "cell-a", "accept": "WAKE-DEAD cell-a"},
        ],
    )
    assert _handle_self_outage("swarph", "cell-a", "", inbox, 300.0, state, state_path) is True
    assert len(calls) == 1
    assert calls[0][:4] == ["swarph", "board", "cards", "say"]
    assert "#77" in calls[0][calls[0].index("--content") + 1]
    assert state["cell-a"]["alerted_at"] == 300.0
