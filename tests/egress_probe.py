#!/usr/bin/env python3
"""Runtime egress probe (card #1219).

``make egress-probe`` runs the swarph-cli suite (every file under
``tests/`` except this probe's own tests) under ``bwrap --unshare-net``
and fails if strace sees an unlisted IP-literal TCP connect, or an exec
of curl/wget/ssh/scp to a destination EGRESS.md does not name.

Scope of a connect: IP literals only, IPv4 and IPv6. A hostname lookup
shows up as a connect to the stub resolver (127.0.0.53); that is not a
violation, because the allowlist holds names and the trace holds an IP,
so a listed urlopen and an unlisted hostname cannot be told apart.
Loopback addresses are ignored. Exec of curl/wget/ssh/scp is judged from
the destination written in its argv.

The operator's live gateway URL is unset inside the suite so a
developer machine does not dial it. Isolation is real only when the
control connect fails with ENETUNREACH.
A connect that merely fails (TEST-NET on a host that still has a route)
is not proof. When isolation cannot be proven the process exits 2 and
writes ``isolation-unproven: <reason>`` to stderr.

Idea after xcb qualification/linux-egress.ts. No code copied.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
    "ipv6": (
        "2001:db8::1",
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('2001:db8::1', 80), timeout=1)\n"
        "except OSError:\n"
        "    pass\n",
    ),
}

_CONNECT_IP = re.compile(
    r'inet_addr\("([0-9.]+)"\)|inet_pton\(AF_INET6,\s*"([^"]+)"')
_EXEC = re.compile(r'execve\("([^"]+)"')
_HOST = re.compile(r"https?://([^/:\s'\"]+)|(?:^|[\s\"'])([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)")
_CONNECT_PORT = re.compile(r"sin6?_port=htons\((\d+)\)")
_TRACE_PREFIX = re.compile(r"^(\d+)\s+(\S+)\s+(.*)$")
_EXECVE_ARGV = re.compile(r'execve\("[^"]*",\s*\[([^\]]*)\]')
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')

_ACTIVITY_PLUGIN = (
    "import os\n"
    "import time\n"
    "\n"
    "def pytest_runtest_logstart(nodeid, location):\n"
    "    path = os.environ.get('EGRESS_PROBE_ACTIVITY')\n"
    "    if not path:\n"
    "        return\n"
    "    try:\n"
    "        with open(path, 'a', encoding='utf-8') as fh:\n"
    "            fh.write('%.6f\\t%s\\n' % (time.time(), nodeid))\n"
    "    except OSError:\n"
    "        pass\n"
)


def _parse_timestamp(token: str) -> float | None:
    try:
        if ":" in token:
            hour, minute, rest = token.split(":", 2)
            return int(hour) * 3600 + int(minute) * 60 + float(rest)
        return float(token)
    except ValueError:
        return None


def _split_trace_line(line: str):
    """(pid, timestamp, rest) from a ``strace -f -ttt -o`` line.

    With -f and -o, strace writes one combined file whose lines start
    with the pid, then the -ttt epoch timestamp (or -tt time-of-day).
    Older/unprefixed lines yield (None, None, line).
    """
    match = _TRACE_PREFIX.match(line)
    if match:
        ts = _parse_timestamp(match.group(2))
        if ts is not None:
            return int(match.group(1)), ts, match.group(3)
    return None, None, line


def parse_activity(source) -> list:
    """Normalise a test timeline to sorted [(epoch_float, nodeid)].

    Accepts raw ``"<ts>\\t<nodeid>"`` text or an iterable of pairs.
    """
    entries = []
    if source is None:
        return entries
    if isinstance(source, (str, bytes)):
        text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
        for line in text.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                parts = line.strip().split("\t", 1)
            if len(parts) != 2:
                continue
            try:
                entries.append((float(parts[0]), parts[1].strip()))
            except ValueError:
                continue
    else:
        for item in source:
            try:
                entries.append((float(item[0]), str(item[1])))
            except (TypeError, ValueError, IndexError):
                continue
    return sorted(entries)


def _test_at(ts, timeline) -> str:
    if ts is None or not timeline:
        return "outside pytest"
    entries = timeline
    # Reconcile epoch timestamps with time-of-day timestamps.
    if (ts < 86400) != (entries[0][0] < 86400):
        if ts < 86400:
            def _tod(epoch):
                local = time.localtime(epoch)
                return local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
            entries = [(_tod(when), nodeid) for when, nodeid in entries]
        else:
            local = time.localtime(ts)
            ts = local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
    current = "outside pytest"
    for when, nodeid in entries:
        if when <= ts:
            current = nodeid
        else:
            break
    return current


def _format_argv(argv) -> str:
    if not argv:
        return "unknown"
    if isinstance(argv, (list, tuple)):
        text = " ".join(str(part) for part in argv)
    else:
        text = str(argv)
    return text if text else "unknown"


def _snapshot_entry(snapshot, pid):
    if not snapshot or pid is None:
        return {}
    entry = snapshot.get(pid, snapshot.get(str(pid), {}))
    if isinstance(entry, dict):
        return entry
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return {"argv": entry[0], "cwd": entry[1]}
    return {}


def parse_connects(trace: str) -> list:
    """Per-connect records: ip, port, pid, timestamp, argv-at-connect."""
    records = []
    argv_by_pid: dict = {}
    for line in trace.splitlines():
        pid, ts, rest = _split_trace_line(line)
        exec_argv = _EXECVE_ARGV.search(rest)
        if exec_argv and ("= 0" in rest or "= -1" not in rest):
            argv = [p.replace('\\"', '"') for p in _QUOTED.findall(exec_argv.group(1))]
            if pid is not None:
                argv_by_pid[pid] = argv
        if "connect(" not in rest:
            continue
        port_match = _CONNECT_PORT.search(rest)
        port = int(port_match.group(1)) if port_match else None
        for v4, v6 in _CONNECT_IP.findall(rest):
            records.append({
                "ip": v4 or v6, "port": port, "pid": pid, "timestamp": ts,
                "argv": list(argv_by_pid.get(pid, [])) if pid is not None else [],
            })
    return records


def attribution_report(trace: str, allowed: set, timeline=None,
                       proc_snapshot=None) -> list:
    """One attribution line per unlisted connect, on top of violations().

    Every line always carries destination, port, pid, test, argv and
    cwd; unrecoverable values are the literal 'unknown' (test falls
    back to 'outside pytest'), never an omitted field.
    """
    entries = parse_activity(timeline)
    lines = []
    for record in parse_connects(trace):
        ip = record["ip"]
        if _ignored_ip(ip, allowed):
            continue
        pid = record["pid"]
        snap = _snapshot_entry(proc_snapshot, pid)
        argv = snap.get("argv") or record.get("argv") or None
        cwd = snap.get("cwd") or "unknown"
        port = record["port"]
        lines.append(
            "attribution: destination={} port={} pid={} test={} "
            "argv={} cwd={}".format(
                ip,
                port if port is not None else "unknown",
                pid if pid is not None else "unknown",
                _test_at(record["timestamp"], entries),
                _format_argv(argv),
                cwd,
            ))
    return lines


def _live_proc_snapshot(pids) -> dict:
    snapshot = {}
    for pid in pids:
        if pid is None:
            continue
        entry = {}
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [p.decode("utf-8", "replace")
                        for p in fh.read().split(b"\0") if p]
            if argv:
                entry["argv"] = argv
        except OSError:
            pass
        try:
            entry["cwd"] = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        if entry:
            snapshot[pid] = entry
    return snapshot


class _ProcWatcher(threading.Thread):
    """Poll /proc while the suite runs (pids are host-visible: bwrap
    here unshares only user+net namespaces, not pid)."""

    def __init__(self, interval: float = 0.1):
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self._interval = interval
        self.snapshot: dict = {}

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            self._poll()
            self._stop_event.wait(self._interval)

    def _poll(self) -> None:
        try:
            names = os.listdir("/proc")
        except OSError:
            return
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    argv = [p.decode("utf-8", "replace")
                            for p in fh.read().split(b"\0") if p]
            except OSError:
                continue
            if not argv:
                continue
            entry = {"argv": argv}
            try:
                entry["cwd"] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                entry["cwd"] = self.snapshot.get(pid, {}).get("cwd", "unknown")
            self.snapshot[pid] = entry


def _trace_out_path(cwd) -> Path:
    override = os.environ.get("EGRESS_PROBE_TRACE_OUT")
    if override:
        return Path(override)
    return Path(cwd) / "egress-probe-trace.txt"


def _remove_trace_out(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _retain_trace(path: Path, trace: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(trace, encoding="utf-8")
    except OSError as exc:
        print(f"egress-probe: could not retain raw trace at {path}: {exc}",
              file=sys.stderr)


def _merged_snapshot(watcher, trace: str) -> dict:
    merged = dict(watcher.snapshot) if watcher is not None else {}
    pids = {r["pid"] for r in parse_connects(trace)}
    for pid, entry in _live_proc_snapshot(pids).items():
        current = dict(merged.get(pid, {}))
        current.update({k: v for k, v in entry.items() if v})
        merged[pid] = current
    return merged


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


def _ignored_ip(ip: str, allowed: set[str]) -> bool:
    if ip == CONTROL_IP or ip.lower() in allowed:
        return True
    if ip == "::1" or ip.startswith("127."):
        return True
    return False


def violations(trace: str, allowed: set[str]) -> list[str]:
    """Unlisted IP-literal connects and unlisted curl/ssh/wget/scp execs.

    The probe's own control address is ignored here; the isolation check
    looks at it separately. Loopback, including the stub resolver, is
    not a route out.
    """
    found = []
    for line in trace.splitlines():
        for v4, v6 in _CONNECT_IP.findall(line):
            ip = v4 or v6
            if _ignored_ip(ip, allowed):
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
    with tempfile.TemporaryDirectory(prefix="egress-probe-") as work:
        script = os.path.join(work, "snippet.py")
        trace_path = os.path.join(work, "trace")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(source)
        cmd = [
            bwrap, "--unshare-user", "--unshare-net", "--die-with-parent",
            "--ro-bind", "/", "/",
            "--bind", work, work,
            "--dev", "/dev", "--proc", "/proc",
            strace, "-f", "-ttt", "-e", "trace=connect,execve", "-s", "400",
            "-o", trace_path,
            sys.executable, script,
        ]
        watcher = _ProcWatcher()
        watcher.start()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        finally:
            watcher.stop()
            watcher.join(timeout=2)
        trace = ""
        if os.path.isfile(trace_path):
            with open(trace_path, encoding="utf-8", errors="replace") as fh:
                trace = fh.read()
        if not trace:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else f"exit {proc.returncode}"
            return _die(f"bwrap --unshare-net failed ({tail})"), []
        found = violations(trace, allowed)
        if found:
            # No pytest timeline here: test is 'outside pytest'.
            for line in attribution_report(
                    trace, allowed, [], _merged_snapshot(watcher, trace)):
                print(line, file=sys.stderr)
        return 0, found


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
    with tempfile.TemporaryDirectory(prefix="egress-probe-") as work:
        script = os.path.join(work, "control.py")
        trace_path = os.path.join(work, "trace")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write(source)
        cmd = [
            bwrap, "--unshare-user", "--unshare-net", "--die-with-parent",
            "--ro-bind", "/", "/",
            "--bind", work, work,
            "--dev", "/dev", "--proc", "/proc",
            strace, "-f", "-ttt", "-e", "trace=connect", "-s", "200",
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
        lines = [ln for ln in trace.splitlines()
                 if CONTROL_IP in ln and "connect(" in ln]
        if not lines:
            return _die("control connect not observed")
        if any(re.search(r"= 0\b", ln) for ln in lines):
            return _die("control connect succeeded")
        if not any("ENETUNREACH" in ln for ln in lines):
            return _die("control connect was not ENETUNREACH")
        return 0


def run_pytest(cwd: Path, paths: list[str], allowed: set[str]) -> int:
    """Run pytest paths inside the netns.

    A clean trace with pytest exit 1 is not a leak: the trace is the
    verdict for a real run. Pytest exit 2 through 5 means the run did
    not happen (interrupted, internal error, usage error, nothing
    collected), and that is isolation-unproven rather than a pass.

    Attribution lines (destination, port, pid, test, argv, cwd) are
    additional output on stderr, after the existing summary. On failure
    the raw trace is retained at $EGRESS_PROBE_TRACE_OUT or
    ./egress-probe-trace.txt under cwd; on success nothing is written
    there and any stale file is removed at run start.
    """
    trace_out = _trace_out_path(cwd)
    _remove_trace_out(trace_out)
    code = prove_isolation()
    if code != 0:
        print("pytest isolation was not proven", file=sys.stderr)
        return code
    bwrap = _bwrap()
    strace = _strace()
    if not bwrap:
        return _die("bwrap missing")
    if not strace:
        return _die("strace missing")
    with tempfile.TemporaryDirectory(prefix="egress-probe-") as work:
        trace_path = os.path.join(work, "trace")
        activity_path = os.path.join(work, "activity.log")
        plugin_path = os.path.join(work, "egress_activity_plugin.py")
        with open(plugin_path, "w", encoding="utf-8") as fh:
            fh.write(_ACTIVITY_PLUGIN)
        pythonpath = work
        if os.environ.get("PYTHONPATH"):
            pythonpath = work + os.pathsep + os.environ["PYTHONPATH"]
        cmd = [
            bwrap, "--unshare-user", "--unshare-net", "--die-with-parent",
            "--ro-bind", "/", "/",
            "--tmpfs", "/tmp",
            "--setenv", "TMPDIR", "/tmp",
            "--bind", work, work,
            "--bind", str(cwd), str(cwd),
            "--chdir", str(cwd),
            "--dev", "/dev", "--proc", "/proc",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--setenv", "EGRESS_PROBE_ACTIVITY", activity_path,
            "--setenv", "PYTHONPATH", pythonpath,
            # The operator's live gateway must not ride into the suite.
            "--unsetenv", "MESH_GATEWAY_URL",
            "--unsetenv", "MESH_GATEWAY_TOKEN",
            "--unsetenv", "SWARPH_BRAIN_GATEWAY",
            "--unsetenv", "SWARPH_BRAIN_MCP",
            strace, "-f", "-ttt", "-e", "trace=connect,execve", "-s", "400",
            "-o", trace_path,
            sys.executable, "-m", "pytest", "-q", "--tb=line",
            "-p", "egress_activity_plugin", *paths,
        ]
        watcher = _ProcWatcher()
        watcher.start()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        finally:
            watcher.stop()
            watcher.join(timeout=2)
        trace = ""
        if os.path.isfile(trace_path):
            with open(trace_path, encoding="utf-8", errors="replace") as fh:
                trace = fh.read()
        activity = ""
        if os.path.isfile(activity_path):
            with open(activity_path, encoding="utf-8", errors="replace") as fh:
                activity = fh.read()
        if not trace:
            return _die("bwrap --unshare-net failed")
        # 2 interrupted, 3 internal, 4 usage, 5 nothing collected.
        if proc.returncode in (2, 3, 4, 5):
            return _die(f"pytest exited {proc.returncode}")
        found = violations(trace, allowed)
        if found:
            print("unlisted-connect", file=sys.stderr)
            print("unlisted connect: " + ", ".join(found), file=sys.stderr)
            for line in attribution_report(
                    trace, allowed, activity,
                    _merged_snapshot(watcher, trace)):
                print(line, file=sys.stderr)
            _retain_trace(trace_out, trace)
            return 1
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
    if argv and argv[0] == "--pytest":
        if len(argv) < 2:
            print("usage: egress_probe.py --pytest PATH [PATH...]", file=sys.stderr)
            return 2
        return run_pytest(ROOT, argv[1:], allowed)
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
    print("usage: egress_probe.py --smoke | --isolation | "
          "--mutant NAME | --pytest PATH...",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
