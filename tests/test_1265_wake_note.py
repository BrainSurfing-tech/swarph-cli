"""Card #1265. A recovered wake posts one note on card 729. Each case
fails on the parent by assertion: the parent posts no note."""
import datetime
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

NOW = 1_700_000_000.0


def _yaml(config: Path) -> None:
    directory = config / "swarph" / "cells"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "lab.yaml").write_text(
        "schema_version: v1\n"
        "name: lab-ovh\n"
        "role: lab\n"
        f"cwd: \"{Path.cwd().as_posix()}\"\n"
        "provider: claude\n",
        encoding="utf-8",
    )


def _alive(tmp_path: Path) -> None:
    inbox = tmp_path / "state" / "lab-ovh" / "mesh-sidecar" / "inbox.log"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text("", encoding="utf-8")
    (inbox.parent / "channel_heartbeat.json").write_text(
        json.dumps({"pid": 4242, "ts": time.time()}), encoding="utf-8")
    proc = tmp_path / "proc"
    for pid, ppid in ((4242, 111), (111, 1)):
        child = proc / str(pid)
        child.mkdir(parents=True, exist_ok=True)
    (proc / "4242" / "stat").write_text("4242 (child) S 111 0 0\n", encoding="utf-8")
    (proc / "111" / "cmdline").write_bytes(
        b"\0".join(a.encode() for a in ["claude", "--name", "lab"]) + b"\0")


def _stub(tmp_path: Path) -> Path:
    stub = tmp_path / "swarph"
    stub.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        from pathlib import Path
        log = Path(os.environ["STUB_LOG"])
        with log.open("a", encoding="utf-8") as fh:
            fh.write(" ".join(sys.argv[1:]) + "\\n")
        if "obligations" in sys.argv and "list" in sys.argv:
            print(json.dumps({{"obligations": [{{
                "id": 42, "holder": "commander", "card_id": 729,
                "status": "open", "created_by": "lab-ovh",
                "accept": "WAKE-DEAD lab-ovh",
                "taken_at": "2026-10-25T00:00:00Z",
            }}]}}))
            raise SystemExit(0)
        if "cards" in sys.argv and "say" in sys.argv:
            print(os.environ.get("STUB_SAY_LINE", "posted"))
            raise SystemExit(int(os.environ.get("STUB_SAY_RC", "0")))
        raise SystemExit(0)
    """), encoding="utf-8")
    stub.chmod(0o755)
    return stub


def _env(tmp_path: Path, *, say_rc: str = "0", say_line: str = "posted") -> dict:
    _alive(tmp_path)
    _yaml(tmp_path / "config")
    state = tmp_path / "wake.json"
    state.write_text(json.dumps({
        "lab-ovh": {"outage": True, "alerted_at": NOW - 100,
                    "missing_since": NOW - 200},
    }), encoding="utf-8")
    log = tmp_path / "stub.log"
    log.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "SWARPH_STATE_ROOT": str(tmp_path / "state"),
        "WAKE_WATCHDOG_STATE": str(state),
        "WAKE_WATCHDOG_NOW": str(NOW),
        "WAKE_WATCHDOG_PROC": str(tmp_path / "proc"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "SWARPH_BIN": str(_stub(tmp_path)),
        "STUB_LOG": str(log),
        "STUB_SAY_RC": say_rc,
        "STUB_SAY_LINE": say_line,
    }
    env.pop("SWARPH_SELF", None)
    env.pop("MESH_GATEWAY_URL", None)
    return env


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog",
         "--as", "lab-ovh", "--escalate", "drop-on-meta-edge"],
        env=env, capture_output=True, text=True, check=False)


def _when() -> str:
    return datetime.datetime.fromtimestamp(
        NOW, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_recovery_posts_one_note_naming_the_row(tmp_path):
    env = _env(tmp_path)
    proc = _run(env)
    assert proc.returncode == 0
    text = Path(env["STUB_LOG"]).read_text(encoding="utf-8")
    assert text.count("cards say 729") == 1
    assert "--kind status" in text
    assert "--to commander" in text
    assert f"lab-ovh DM wake recovered at {_when()}" in text
    assert "dead-wake row #42 can be dismissed" in text
    assert "cards say 42 rc=0" in proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_a_second_pass_posts_nothing(tmp_path):
    env = _env(tmp_path)
    assert _run(env).returncode == 0
    log = Path(env["STUB_LOG"])
    assert log.read_text(encoding="utf-8").count("cards say 729") == 1
    log.write_text("", encoding="utf-8")
    second = _run(env)
    assert second.returncode == 0
    assert "cards say" not in log.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX swarph stub")
def test_a_failed_note_is_logged_and_the_pass_continues(tmp_path):
    env = _env(tmp_path, say_rc="1", say_line="gateway 403: refused")
    proc = _run(env)
    assert proc.returncode == 0
    assert "cards say 42 rc=1 gateway 403: refused" in proc.stderr
    assert "cards say 729" in Path(env["STUB_LOG"]).read_text(encoding="utf-8")
