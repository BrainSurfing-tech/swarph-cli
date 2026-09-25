"""card #729: --once follows the inbox file and exits on the first real DM."""
import json
import os
import subprocess
import sys
import time

from swarph_cli.scripts import dm_notify_filter as filt


def test_once_skips_a_receipt_and_exits_on_the_dm(tmp_path):
    inbox = tmp_path / "inbox.log"
    inbox.write_text("")
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarph_cli.scripts.dm_notify_filter",
         "--once", "--inbox", str(inbox)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": os.path.abspath("src")},
    )
    time.sleep(0.2)
    receipt = {"id": 1, "from_node": "lab-ovh", "kind": "fyi", "content": "receipt: ok"}
    dm = {"id": 2, "from_node": "lab-ovh", "kind": "question", "content": "hello mesh"}
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt) + "\n")
        fh.write(json.dumps(dm) + "\n")
    try:
        out, err = proc.communicate(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert out.strip().startswith("[MESH DM] id=2")
    assert "receipt" not in out
    kids = subprocess.run(["ps", "--ppid", str(proc.pid), "-o", "pid="],
                          capture_output=True, text=True)
    assert kids.stdout.strip() == ""


def test_once_ignores_a_preexisting_dm_and_prints_one_appended_later(tmp_path):
    inbox = tmp_path / "inbox.log"
    old = {"id": 9, "from_node": "lab-ovh", "kind": "fyi", "content": "old mail"}
    inbox.write_text(json.dumps(old) + "\n")
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarph_cli.scripts.dm_notify_filter",
         "--once", "--inbox", str(inbox), "--timeout", "8"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": os.path.abspath("src")},
    )
    time.sleep(4)
    fresh = {"id": 10, "from_node": "lab-ovh", "kind": "question", "content": "new mail"}
    receipt = {"id": 11, "from_node": "lab-ovh", "kind": "fyi", "content": "receipt: later"}
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt) + "\n")
        fh.write(json.dumps(fresh) + "\n")
    try:
        out, err = proc.communicate(timeout=6)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert "old mail" not in out
    assert "receipt" not in out
    assert out.strip().startswith("[MESH DM] id=10")
    kids = subprocess.run(["ps", "--ppid", str(proc.pid), "-o", "pid="],
                          capture_output=True, text=True)
    assert kids.stdout.strip() == ""


def test_once_timeout_exits_nonzero_when_nothing_arrives(tmp_path):
    inbox = tmp_path / "inbox.log"
    inbox.write_text(json.dumps(
        {"id": 1, "from_node": "lab-ovh", "kind": "fyi", "content": "already there"}) + "\n")
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "swarph_cli.scripts.dm_notify_filter",
         "--once", "--inbox", str(inbox), "--timeout", "3"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": os.path.abspath("src")},
        timeout=8,
    )
    elapsed = time.monotonic() - t0
    assert proc.returncode != 0
    assert proc.stdout.strip() == ""
    assert 2.5 <= elapsed <= 5
