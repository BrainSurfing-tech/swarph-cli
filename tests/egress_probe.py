#!/usr/bin/env python3
"""Runtime egress probe (card #1219).

Runs a smoke subset inside ``bwrap --unshare-net`` and fails if strace
sees a direct connect, or an exec of curl/wget/ssh/scp, to a host that
EGRESS.md does not name. When isolation cannot be proven the process
exits 2 and writes ``isolation-unproven: <reason>`` to stderr.

Smoke subset (what ``make egress-probe`` runs): one control connect that
must fail, one network-free snippet that must stay quiet, and three
unlisted mutants (os.system curl, an absolute-path curl, and
asyncio.create_subprocess_exec ssh).

Idea after xcb qualification/linux-egress.ts. No code copied.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EGRESS = ROOT / "EGRESS.md"
CONTROL_IP = "203.0.113.1"
TOOLS = {"curl", "wget", "ssh", "scp"}

# The os.system mutant is a fixed literal, the form card #1219 requires
# the probe to catch. It is not built from external input.
MUTANTS = {
    "os-system-curl": (
        "203.0.113.2",
        "import os\n"
        "os.system('curl --max-time 1 -s -o /dev/null http://203.0.113.2/')\n",
    ),
    "abs-curl": (
        "203.0.113.3",
        "import subprocess\n"
        "subprocess.run(['/usr/bin/curl', '--max-time', '1', '-s', '-o',\n"
        "                 '/dev/null', 'http://203.0.113.3/'], check=False)\n",
    ),
    "asyncio-ssh": (
        "203.0.113.4",
        "import asyncio\n"
        "async def go():\n"
        "    proc = await asyncio.create_subprocess_exec(\n"
        "        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=1',\n"
        "        '203.0.113.4', 'true',\n"
        "        stdout=asyncio.subprocess.DEVNULL,\n"
        "        stderr=asyncio.subprocess.DEVNULL)\n"
        "    await proc.wait()\n"
        "asyncio.run(go())\n",
    ),
}

_CONNECT_IP = re.compile(r'inet_addr\("([0-9.]+)"\)')
_EXEC = re.compile(r'execve\("([^"]+)"')
_HOST = re.compile(r"https?://([^/:\s'\"]+)|(?:^|[\s\"'])([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)")


def _die(reason: str) -> int:
    print(f"isolation-unproven: {reason}", file=sys.stderr)
    return 2


def allowed_hosts(text: str) -> set[str]:
    hosts = set()
    for match in re.finditer(r"://([^/\s:]+)", text):
        hosts.add(match.group(1).lower().strip("[]"))
    return hosts


def _tool(path: str) -> str | None:
    base = os.path.basename(path)
    return base if base in TOOLS else None


def violations(trace: str, allowed: set[str]) -> list[str]:
    """Unlisted connects and unlisted curl/ssh/wget/scp execs.

    The probe's own control address is ignored here; the isolation check
    looks at it separately.
    """
    found = []
    for line in trace.splitlines():
        for ip in _CONNECT_IP.findall(line):
            if ip == CONTROL_IP or ip in allowed:
                continue
            found.append(f"connect {ip}")
        exec_match = _EXEC.search(line)
        if not exec_match:
            continue
        tool = _tool(exec_match.group(1))
        if tool is None:
            continue
        dests = [a or b for a, b in _HOST.findall(line)]
        dests = [d for d in dests if d and d != CONTROL_IP]
        if not dests:
            found.append(f"exec {tool}")
            continue
        for dest in dests:
            if dest.lower() not in allowed:
                found.append(f"exec {tool} {dest}")
    # one line per distinct finding, stable order
    return list(dict.fromkeys(found))


def _bwrap() -> str | None:
    override = os.environ.get("EGRESS_PROBE_BWRAP")
    if override is not None:
        return override if override and os.path.isfile(override) else None
    return shutil.which("bwrap")


def _strace() -> str | None:
    override = os.environ.get("EGRESS_PROBE_STRACE")
    if override is not None:
        return override if override and os.path.isfile(override) else None
    return shutil.which("strace")


def run_script(source: str, allowed: set[str]) -> tuple[int, list[str]]:
    """Run source inside the netns. Returns (exit, violations).

    Exit 2 means isolation could not be proven (reason already printed).
    Exit 0 means the script ran and ``violations`` lists what was seen.
    """
    bwrap = _bwrap()
    if not bwrap:
        return _die("bwrap missing"), []
    strace = _strace()
    if not strace:
        return _die("strace missing"), []
    work = tempfile.mkdtemp(prefix="egress-probe-")
    script = os.path.join(work, "snippet.py")
    trace_path = os.path.join(work, "trace")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(source)
    cmd = [
        bwrap, "--unshare-net", "--die-with-parent",
        "--ro-bind", "/", "/",
        "--bind", work, work,
        "--dev", "/dev", "--proc", "/proc",
        strace, "-f", "-e", "trace=connect,execve", "-s", "400",
        "-o", trace_path,
        sys.executable, script,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    trace = ""
    if os.path.isfile(trace_path):
        with open(trace_path, encoding="utf-8", errors="replace") as fh:
            trace = fh.read()
    if not trace:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        return _die(f"bwrap --unshare-net failed ({tail})"), []
    return 0, violations(trace, allowed)


def prove_isolation() -> int:
    source = (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('{CONTROL_IP}', 80), timeout=1)\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(3)\n"
    )
    bwrap = _bwrap()
    if not bwrap:
        return _die("bwrap missing")
    strace = _strace()
    if not strace:
        return _die("strace missing")
    work = tempfile.mkdtemp(prefix="egress-probe-")
    script = os.path.join(work, "control.py")
    trace_path = os.path.join(work, "trace")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(source)
    cmd = [
        bwrap, "--unshare-net", "--die-with-parent",
        "--ro-bind", "/", "/",
        "--bind", work, work,
        "--dev", "/dev", "--proc", "/proc",
        strace, "-f", "-e", "trace=connect", "-s", "200",
        "-o", trace_path,
        sys.executable, script,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    trace = ""
    if os.path.isfile(trace_path):
        with open(trace_path, encoding="utf-8", errors="replace") as fh:
            trace = fh.read()
    if proc.returncode != 0 or not trace:
        return _die("bwrap --unshare-net failed")
    if CONTROL_IP not in trace or "connect(" not in trace:
        return _die("control connect not observed")
    if re.search(rf'inet_addr\("{re.escape(CONTROL_IP)}"\).*= 0\b', trace):
        return _die("control connect succeeded")
    if not re.search(rf'inet_addr\("{re.escape(CONTROL_IP)}"\)', trace):
        return _die("control connect not observed")
    return 0


def smoke(allowed: set[str]) -> int:
    code = prove_isolation()
    if code != 0:
        return code
    code, found = run_script("print('clean')\n", allowed)
    if code != 0:
        return code
    if found:
        print("unlisted connect in the clean snippet: " + ", ".join(found),
              file=sys.stderr)
        return 1
    for name, (host, source) in MUTANTS.items():
        code, found = run_script(source, allowed)
        if code != 0:
            return code
        if not any(host in item for item in found):
            print(f"mutant {name} was not caught: {found}", file=sys.stderr)
            return 1
        print(f"caught {name}: {', '.join(found)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    allowed = allowed_hosts(EGRESS.read_text(encoding="utf-8")) if EGRESS.is_file() else set()
    if argv == ["--smoke"]:
        return smoke(allowed)
    if argv == ["--isolation"]:
        return prove_isolation()
    if len(argv) == 2 and argv[0] == "--mutant":
        name = argv[1]
        if name not in MUTANTS:
            print(f"unknown mutant {name}", file=sys.stderr)
            return 2
        host, source = MUTANTS[name]
        code, found = run_script(source, allowed)
        if code != 0:
            return code
        if not any(host in item for item in found):
            print(f"mutant {name} produced no unlisted connect", file=sys.stderr)
            return 0
        print("unlisted connect: " + ", ".join(found), file=sys.stderr)
        return 1
    print("usage: egress_probe.py --smoke | --isolation | --mutant NAME",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
