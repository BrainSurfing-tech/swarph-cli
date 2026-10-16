#!/usr/bin/env python3
"""Card #1195: prove a cell's monitor runs under launchd on this Mac.

Against a stub gateway bound to 127.0.0.1. No mesh token, no tailnet, no
Actions secret. The stub serves one DM; the monitor must write it to
inbox.log, die to kill -9, come back as a different PID, and stay down
after bootout.

Each step prints PASS, FAIL, or CANNOT_EVALUATE. Anything but PASS exits
non-zero. A step that did not run is not a pass.

BROKEN_BIN is the discriminator. Point it at a path that does not exist
and the drain step must FAIL. A green run with it set means the smoke
never executed the plist.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Card #1195 discriminator. None runs the installed swarph. An absolute path
# that does not exist must make this workflow FAIL.
BROKEN_BIN: str | None = None

PEER = "smoke-cell"
LABEL = f"tech.brainsurfing.swarph.monitor.{PEER}"


class Stub(BaseHTTPRequestHandler):
    server: "StubServer"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/peers":
            body = {"peers": [{"name": PEER}]}
        elif parsed.path == "/channels":
            body = {"channels": []}
        elif parsed.path == "/messages" and query.get("to") == [PEER]:
            if not self.server.served_dm:
                self.server.served_dm = True
                body = {"messages": [{
                    "id": 1,
                    "from_node": "stub",
                    "to_node": PEER,
                    "kind": "fyi",
                    "content": "one stub DM for card #1195",
                }]}
            else:
                body = {"messages": []}
        else:
            body = {"messages": []}
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class StubServer(ThreadingHTTPServer):
    served_dm = False


def step(name: str, verdict: str, detail: str) -> str:
    print(f"STEP {name} {verdict} {detail}", flush=True)
    return verdict


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def pid_of(domain: str, uid: int) -> int | None:
    target = f"system/{LABEL}" if domain == "system" else f"{domain}/{uid}/{LABEL}"
    cmd = ["launchctl", "print", target]
    if domain == "system":
        cmd = ["sudo", "-n", *cmd]
    proc = run(cmd)
    for line in (proc.stdout or "").splitlines():
        if "pid =" in line:
            raw = line.split("=", 1)[1].strip()
            if raw.isdigit():
                return int(raw)
    return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_pid(domain: str, uid: int, seconds: float) -> int | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        pid = pid_of(domain, uid)
        if pid:
            return pid
        time.sleep(0.5)
    return None


def bootout(domain: str, uid: int) -> None:
    if domain == "system":
        run(["sudo", "-n", "launchctl", "bootout", f"system/{LABEL}"])
        return
    run(["launchctl", "bootout", f"{domain}/{uid}/{LABEL}"])


def try_domains(plist: Path, uid: int) -> tuple[str, str]:
    """Return (domain, error). domain is '' when every attempt failed."""
    errors: list[str] = []
    attempts = [
        ("gui", ["launchctl", "bootstrap", f"gui/{uid}", str(plist)]),
        ("user", ["launchctl", "bootstrap", f"user/{uid}", str(plist)]),
        ("system", ["sudo", "-n", "launchctl", "bootstrap", "system", str(plist)]),
    ]
    for domain, cmd in attempts:
        proc = run(cmd)
        if proc.returncode == 0:
            return domain, ""
        bootout(domain, uid)
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        errors.append(f"{domain}: {err or 'exit ' + str(proc.returncode)}")
    return "", " | ".join(errors)


def main() -> int:
    if sys.platform != "darwin":
        step("platform", "CANNOT_EVALUATE", f"sys.platform={sys.platform}")
        return 2
    if not shutil.which("launchctl"):
        step("launchctl", "CANNOT_EVALUATE", "launchctl is not on PATH")
        return 2

    os.environ.pop("MESH_GATEWAY_TOKEN", None)
    os.environ.pop("MESH_GATEWAY_URL", None)

    from swarph_cli.launchd_render import render_template, substitute

    bin_path = BROKEN_BIN or shutil.which("swarph")
    if not bin_path:
        step("swarph", "FAIL", "swarph is not on PATH and BROKEN_BIN is unset")
        return 1
    if BROKEN_BIN:
        print(f"DISCRIMINATOR broken bin {BROKEN_BIN}", flush=True)

    work = Path(tempfile.mkdtemp(prefix="swarph-cell-smoke-"))
    home = work / "home"
    log_dir = work / "log"
    home.mkdir()
    log_dir.mkdir()
    token = home / ".config" / "swarph" / f"{PEER}.peer_token"
    token.parent.mkdir(parents=True)
    token.write_text("stub-not-a-mesh-credential\n", encoding="utf-8")
    token.chmod(0o600)

    httpd = StubServer(("127.0.0.1", 0), Stub)
    httpd.served_dm = False
    port = httpd.server_address[1]
    gateway = f"http://127.0.0.1:{port}"
    if not gateway.startswith("http://127.0.0.1:"):
        step("gateway", "FAIL", f"refusing non-loopback gateway {gateway}")
        return 1

    import threading
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    plist_text = substitute(render_template("swarph-monitor.plist"), {
        "SWARPH_BIN": bin_path,
        "PEER": PEER,
        "HOME": str(home),
        "GATEWAY": gateway,
        "LOG_DIR": str(log_dir),
    })
    if "127.0.0.1" not in plist_text or "MESH_GATEWAY_URL" not in plist_text:
        step("plist", "FAIL", "rendered plist has no loopback gateway")
        return 1
    plist = work / "swarph-monitor.plist"
    plist.write_text(plist_text, encoding="utf-8")
    print(f"PLIST {plist}", flush=True)
    print(plist_text, flush=True)

    uid = os.getuid()
    verdicts: list[str] = []
    domain, err = try_domains(plist, uid)
    if not domain:
        verdicts.append(step("load", "CANNOT_EVALUATE", err))
        httpd.shutdown()
        return 2
    verdicts.append(step("load", "PASS", f"domain={domain}"))

    before = wait_pid(domain, uid, 20)
    inbox = home / "swarph_state" / PEER / "mesh-sidecar" / "inbox.log"
    drained = False
    deadline = time.time() + 25
    while time.time() < deadline:
        if inbox.is_file() and '"id": 1' in inbox.read_text(encoding="utf-8"):
            drained = True
            break
        time.sleep(0.5)
    if drained:
        verdicts.append(step("drain", "PASS", f"inbox.log has the stub DM pid={before}"))
    else:
        err_log = log_dir / "launchd.err.log"
        tail = err_log.read_text(encoding="utf-8")[-500:] if err_log.is_file() else ""
        verdicts.append(step(
            "drain", "FAIL",
            f"inbox.log missing the stub DM pid={before} stderr={tail!r}",
        ))

    if before is None:
        verdicts.append(step("restart", "FAIL", "no pid before kill"))
    else:
        os.kill(before, 9)
        after = None
        deadline = time.time() + 25
        while time.time() < deadline:
            current = pid_of(domain, uid)
            if current and current != before:
                after = current
                break
            time.sleep(0.5)
        print(f"PIDS before={before} after={after}", flush=True)
        if after and after != before:
            verdicts.append(step("restart", "PASS", f"before_pid={before} after_pid={after}"))
        else:
            verdicts.append(step("restart", "FAIL", f"before_pid={before} after_pid={after}"))

    bootout(domain, uid)
    time.sleep(8)
    later = pid_of(domain, uid)
    stayed = later is None and (before is None or not alive(before))
    if stayed:
        verdicts.append(step("unload", "PASS", "bootout held; no pid after 8s"))
    else:
        verdicts.append(step("unload", "FAIL", f"still up pid={later}"))

    httpd.shutdown()
    if any(v != "PASS" for v in verdicts):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
