"""Card #965. swarph mesh wait --once. Each test fails on main because there is no wait command."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _row(i, to, kind="status", body="hello", frm="lab-ovh"):
    return json.dumps({
        "id": i, "from_node": frm, "to_node": to, "kind": kind, "content": body, "card": 1,
    })


def _side(tmp_path: Path, cell: str = "cursor-lin") -> Path:
    side = tmp_path / cell / "mesh-sidecar"
    side.mkdir(parents=True)
    return side


def _run(tmp_path: Path, argv: list[str], *, self_env: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SWARPH_STATE"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO / "src")
    if self_env:
        env["SWARPH_SELF"] = "cursor-lin"
    else:
        env.pop("SWARPH_SELF", None)
    return subprocess.run(
        [sys.executable, "-m", "swarph_cli", "mesh", *argv],
        env=env, capture_output=True, text=True, timeout=15,
    )


def test_pending_dm_exits_immediately_and_skips_receipt_and_foreign(tmp_path):
    """If false this reads: no wait command, or a receipt/foreign row is printed."""
    side = _side(tmp_path)
    (side / "wait_cursor.json").write_text(
        json.dumps({"last_delivered_id": 0}) + "\n", encoding="utf-8")
    (side / "inbox.log").write_text("\n".join([
        _row(1, "cursor-lin", body="real"),
        json.dumps({
            "id": 2, "from_node": "lab-ovh", "to_node": "cursor-lin",
            "kind": "status", "content": "receipt: delivered",
        }),
        _row(3, "other-cell", body="foreign"),
    ]) + "\n", encoding="utf-8")
    proc = _run(tmp_path, ["wait", "--once", "--as", "cursor-lin", "--max-wait-s", "5"])
    assert proc.returncode == 0
    assert "id=1 " in proc.stdout and "real" in proc.stdout
    assert "id=2 " not in proc.stdout
    assert "id=3 " not in proc.stdout
    assert "other-cell" not in proc.stdout
    assert proc.stdout.strip().splitlines()[-1] == "swarph mesh wait --once --as cursor-lin"


def test_appended_dm_exits_within_2s_and_cursor_follows_the_print(tmp_path):
    """If false this reads: the cursor file holds the id before stdout has the DM."""
    side = _side(tmp_path)
    inbox = side / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["SWARPH_STATE"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO / "src")
    env.pop("SWARPH_SELF", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarph_cli", "mesh", "wait", "--once", "--as", "cursor-lin"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(1.0)
    assert not (side / "wait_cursor.json").exists()
    inbox.write_text(_row(7, "cursor-lin", body="later") + "\n", encoding="utf-8")
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert "id=7 " in out
    cursor = json.loads((side / "wait_cursor.json").read_text(encoding="utf-8"))
    assert cursor["last_delivered_id"] == 7


def test_two_dms_are_both_printed_and_survive_rearm(tmp_path):
    """If false this reads: the second id is missing, or the re-arm wait prints it again."""
    side = _side(tmp_path)
    inbox = side / "inbox.log"
    inbox.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["SWARPH_STATE"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO / "src")
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarph_cli", "mesh", "wait", "--once", "--as", "cursor-lin"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(1.0)
    inbox.write_text(_row(4, "cursor-lin", body="one") + "\n" + _row(5, "cursor-lin", body="two") + "\n", encoding="utf-8")
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert "id=4 " in out and "id=5 " in out
    again = _run(tmp_path, ["wait", "--once", "--as", "cursor-lin", "--max-wait-s", "1"])
    assert "id=4 " not in again.stdout and "id=5 " not in again.stdout
    assert again.returncode == 0


def test_timeout_prints_the_rearm_line_and_exits_0(tmp_path):
    """If false this reads: exit nonzero, or no re-arm line."""
    side = _side(tmp_path)
    (side / "inbox.log").write_text("", encoding="utf-8")
    proc = _run(tmp_path, ["wait", "--once", "--as", "cursor-lin", "--max-wait-s", "2"])
    assert proc.returncode == 0
    assert "no DM in 2s; re-arm: swarph mesh wait --once --as cursor-lin" in proc.stdout
    assert proc.stdout.strip().splitlines()[-1] == "swarph mesh wait --once --as cursor-lin"


def test_missing_as_exits_2_even_with_swarph_self(tmp_path):
    """If false this reads: exit 0 because SWARPH_SELF was used."""
    side = _side(tmp_path)
    (side / "inbox.log").write_text(_row(1, "cursor-lin") + "\n", encoding="utf-8")
    proc = _run(tmp_path, ["wait", "--once"], self_env=True)
    assert proc.returncode == 2
    assert "invalid choice" not in proc.stderr
    assert "--as" in proc.stderr
    assert "id=1 " not in proc.stdout


def test_first_start_seeks_to_end_and_prints_only_the_appended_dm(tmp_path):
    """If false this reads: a pre-existing row is delivered on first start."""
    side = _side(tmp_path)
    inbox = side / "inbox.log"
    old = "\n".join(_row(i, "cursor-lin", body=f"old-{i}") for i in range(1, 26))
    inbox.write_text(old + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["SWARPH_STATE"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO / "src")
    env.pop("SWARPH_SELF", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarph_cli", "mesh", "wait", "--once", "--as", "cursor-lin"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(1.0)
    inbox.write_text(old + "\n" + _row(26, "cursor-lin", body="fresh") + "\n", encoding="utf-8")
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert "id=26 " in out and "fresh" in out
    for i in range(1, 26):
        assert f"id={i} " not in out
    assert "more, read the inbox" not in out


def test_missing_inbox_exits_2(tmp_path):
    """If false this reads: exit 0 with a re-arm line and no inbox."""
    _side(tmp_path)
    proc = _run(tmp_path, ["wait", "--once", "--as", "cursor-lin", "--max-wait-s", "2"])
    assert proc.returncode == 2
    assert "invalid choice" not in proc.stderr
    assert "inbox.log" in proc.stderr
