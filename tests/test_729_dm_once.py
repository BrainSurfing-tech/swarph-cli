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
