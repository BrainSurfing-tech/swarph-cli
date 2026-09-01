"""Orphan ``claude daemon run --origin transient`` detector (#666).

Read-only. Enumerate Anthropic's ``claude daemon run`` processes, classify
each as LIVE / ORPHANED / UNKNOWN from ``--spawned-by`` + tmux-scope
evidence, and print a report. No signals.

UNKNOWN must never be treated as ORPHANED
([[feedback_absent_feature_looks_like_broken_feature]]). The reaper (T3)
is a separate opt-in; this module does not kill anything.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

# Exactly three states. Do not invent a fourth that collapses into ORPHANED.
STATE_LIVE = "LIVE"
STATE_ORPHANED = "ORPHANED"
STATE_UNKNOWN = "UNKNOWN"

_SPAWNED_BY_RE = re.compile(
    r"--spawned-by\s+(\{.*?\})(?:\s+--|\s*$)",
    re.DOTALL,
)
_ORIGIN_RE = re.compile(r"--origin\s+(\S+)")
_TMUX_SCOPE_RE = re.compile(r"(tmux-spawn-[^/\s]+\.scope)")


@dataclass
class DaemonReport:
    pid: int
    cmdline: str
    state: str
    spawner_pid: Optional[int] = None
    spawner_alive: Optional[bool] = None
    origin: Optional[str] = None
    scope: Optional[str] = None
    scope_live: Optional[bool] = None
    child_count: int = 0
    rss_kb: int = 0
    reason: str = ""
    self_excluded: bool = False


@dataclass
class ScanResult:
    daemons: list[DaemonReport] = field(default_factory=list)

    @property
    def orphans(self) -> list[DaemonReport]:
        return [d for d in self.daemons if d.state == STATE_ORPHANED]

    @property
    def unknowns(self) -> list[DaemonReport]:
        return [d for d in self.daemons if d.state == STATE_UNKNOWN]


def is_claude_daemon_cmdline(cmdline: str) -> bool:
    """True when argv looks like Anthropic's ``claude daemon run``."""
    # Avoid matching shells/scripts that merely mention the string.
    parts = cmdline.split()
    try:
        i = next(i for i, p in enumerate(parts) if p.endswith("claude") or p == "claude")
    except StopIteration:
        return False
    return (
        i + 2 < len(parts)
        and parts[i + 1] == "daemon"
        and parts[i + 2] == "run"
    )


def parse_spawned_by(cmdline: str) -> Optional[dict]:
    """Return the ``--spawned-by`` JSON object, or None if absent/unparseable."""
    m = _SPAWNED_BY_RE.search(cmdline)
    if not m:
        return None
    raw = m.group(1)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_origin(cmdline: str) -> Optional[str]:
    m = _ORIGIN_RE.search(cmdline)
    return m.group(1) if m else None


def extract_tmux_scope(cgroup_text: Optional[str]) -> Optional[str]:
    if not cgroup_text:
        return None
    m = _TMUX_SCOPE_RE.search(cgroup_text)
    return m.group(1) if m else None


def classify_daemon(
    *,
    spawner_pid: Optional[int],
    spawner_alive: Optional[bool],
    scope: Optional[str],
    scope_live: Optional[bool],
    self_related: bool = False,
) -> tuple[str, str]:
    """Return ``(state, reason)``. Pure: no /proc, no signals.

    Rules (card #666 T2):
      LIVE      spawner alive → leave alone
                (also: spawner dead but scope still live → leave alone)
      ORPHANED  spawner dead AND scope has no live tmux session
      UNKNOWN   spawner pid missing / unparseable, OR evidence incomplete

    UNKNOWN is never ORPHANED. Self-related daemons (A3) are never ORPHANED.
    """
    if self_related:
        return STATE_LIVE, "self-exclusion: caller is in this daemon's ancestry"
    if spawner_pid is None or spawner_alive is None:
        return STATE_UNKNOWN, "spawned-by pid missing or unparseable"
    if spawner_alive:
        return STATE_LIVE, "spawner alive"
    # spawner dead — need affirmative scope-dead evidence to orphan
    if scope is None or scope_live is None:
        return STATE_UNKNOWN, "spawner dead but scope evidence incomplete"
    if scope_live:
        return STATE_LIVE, "spawner dead but scope still maps to a live tmux session"
    return STATE_ORPHANED, "spawner dead and scope has no live tmux session"


def pid_alive(pid: int, *, proc_root: Path = Path("/proc")) -> Optional[bool]:
    """True/False if determinable; None if we cannot tell (not Linux, etc.)."""
    if pid <= 0:
        return False
    try:
        (proc_root / str(pid)).stat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def read_cmdline(pid: int, *, proc_root: Path = Path("/proc")) -> Optional[str]:
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip() or None


def read_cgroup(pid: int, *, proc_root: Path = Path("/proc")) -> Optional[str]:
    try:
        return (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None


def read_rss_kb(pid: int, *, proc_root: Path = Path("/proc")) -> int:
    try:
        text = (proc_root / str(pid) / "status").read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return 0


def read_ppid(pid: int, *, proc_root: Path = Path("/proc")) -> Optional[int]:
    try:
        data = (proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        after = data[data.rfind(")") + 2:].split()
        return int(after[1])
    except (OSError, ValueError, IndexError):
        return None


def list_children(pid: int, *, proc_root: Path = Path("/proc")) -> list[int]:
    """Direct children of ``pid`` (one level)."""
    out: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        child = int(entry.name)
        if read_ppid(child, proc_root=proc_root) == pid:
            out.append(child)
    return out


def process_tree(root: int, *, proc_root: Path = Path("/proc"), limit: int = 256) -> list[int]:
    """BFS descendants of ``root`` (excluding root). Bounded."""
    seen: set[int] = set()
    queue = list(list_children(root, proc_root=proc_root))
    while queue and len(seen) < limit:
        cur = queue.pop(0)
        if cur in seen or cur == root:
            continue
        seen.add(cur)
        queue.extend(list_children(cur, proc_root=proc_root))
    return sorted(seen)


def ancestry_pids(pid: int, *, proc_root: Path = Path("/proc"), max_depth: int = 40) -> set[int]:
    """``pid`` plus every PPID up to init."""
    out: set[int] = set()
    cur = pid
    for _ in range(max_depth):
        if cur in out or cur <= 0:
            break
        out.add(cur)
        ppid = read_ppid(cur, proc_root=proc_root)
        if ppid is None or ppid == cur:
            break
        cur = ppid
    return out


def self_related(
    daemon_pid: int,
    caller_pid: int,
    *,
    proc_root: Path = Path("/proc"),
) -> bool:
    """A3: True if the caller sits in the daemon's ancestry or process tree.

    The session that runs the detector must never classify its own serving
    daemon as ORPHANED.
    """
    if daemon_pid == caller_pid:
        return True
    caller_anc = ancestry_pids(caller_pid, proc_root=proc_root)
    if daemon_pid in caller_anc:
        return True
    daemon_tree = set(process_tree(daemon_pid, proc_root=proc_root)) | {daemon_pid}
    if caller_pid in daemon_tree:
        return True
    if caller_anc & daemon_tree:
        return True
    return False


def list_tmux_sessions(
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Optional[list[str]]:
    """Session names from ``tmux list-sessions``, or None if tmux unreachable."""
    try:
        r = run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def pane_pids_for_session(
    session: str,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[int]:
    try:
        r = run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    return [int(p) for p in r.stdout.split() if p.strip().isdigit()]


def live_scopes(
    sessions: Iterable[str],
    *,
    proc_root: Path = Path("/proc"),
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> set[str]:
    """tmux-spawn-*.scope names still hosting at least one live tmux pane."""
    scopes: set[str] = set()
    for session in sessions:
        for pane in pane_pids_for_session(session, run=run):
            # Pane shell + a few descendants — enough to catch the agent.
            candidates = [pane] + process_tree(pane, proc_root=proc_root, limit=64)
            for pid in candidates:
                scope = extract_tmux_scope(read_cgroup(pid, proc_root=proc_root))
                if scope:
                    scopes.add(scope)
    return scopes


def enumerate_claude_daemon_pids(
    *,
    proc_root: Path = Path("/proc"),
) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return out
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmdline = read_cmdline(pid, proc_root=proc_root)
        if cmdline and is_claude_daemon_cmdline(cmdline):
            out.append((pid, cmdline))
    return sorted(out, key=lambda t: t[0])


def scan_orphan_daemons(
    *,
    proc_root: Path = Path("/proc"),
    caller_pid: Optional[int] = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ScanResult:
    """Walk /proc, classify every ``claude daemon run``, return the report."""
    if caller_pid is None:
        caller_pid = os.getpid()

    sessions = list_tmux_sessions(run=run)
    # None → tmux unreachable: scope_live becomes None → UNKNOWN, never ORPHANED.
    scopes: Optional[set[str]]
    if sessions is None:
        scopes = None
    else:
        scopes = live_scopes(sessions, proc_root=proc_root, run=run)

    result = ScanResult()
    for pid, cmdline in enumerate_claude_daemon_pids(proc_root=proc_root):
        spawned = parse_spawned_by(cmdline)
        origin = parse_origin(cmdline)
        scope = extract_tmux_scope(read_cgroup(pid, proc_root=proc_root))

        spawner_pid: Optional[int] = None
        spawner_alive: Optional[bool] = None
        if isinstance(spawned, dict) and "pid" in spawned:
            try:
                spawner_pid = int(spawned["pid"])
            except (TypeError, ValueError):
                spawner_pid = None
            if spawner_pid is not None:
                spawner_alive = pid_alive(spawner_pid, proc_root=proc_root)

        if scopes is None:
            scope_live: Optional[bool] = None
        elif scope is None:
            scope_live = None
        else:
            scope_live = scope in scopes

        related = self_related(pid, caller_pid, proc_root=proc_root)
        state, reason = classify_daemon(
            spawner_pid=spawner_pid,
            spawner_alive=spawner_alive,
            scope=scope,
            scope_live=scope_live,
            self_related=related,
        )

        tree = process_tree(pid, proc_root=proc_root)
        rss = read_rss_kb(pid, proc_root=proc_root) + sum(
            read_rss_kb(c, proc_root=proc_root) for c in tree
        )
        result.daemons.append(
            DaemonReport(
                pid=pid,
                cmdline=cmdline,
                state=state,
                spawner_pid=spawner_pid,
                spawner_alive=spawner_alive,
                origin=origin,
                scope=scope,
                scope_live=scope_live,
                child_count=len(tree),
                rss_kb=rss,
                reason=reason,
                self_excluded=related,
            )
        )
    return result


def format_report(result: ScanResult) -> str:
    lines: list[str] = []
    if not result.daemons:
        # Explicit none — distinguishable from "did not run" (T1 accept).
        lines.append("orphan-daemons: none")
        lines.append("  (no `claude daemon run` processes found)")
        return "\n".join(lines)

    n_orph = len(result.orphans)
    n_unk = len(result.unknowns)
    n_live = sum(1 for d in result.daemons if d.state == STATE_LIVE)
    lines.append(
        f"orphan-daemons: {len(result.daemons)} daemon(s) — "
        f"{n_orph} ORPHANED, {n_live} LIVE, {n_unk} UNKNOWN"
    )
    for d in result.daemons:
        spawner = (
            f"spawner={d.spawner_pid}"
            f"({'alive' if d.spawner_alive else 'DEAD' if d.spawner_alive is False else '?'})"
            if d.spawner_pid is not None
            else "spawner=?"
        )
        scope = d.scope or "scope=?"
        scope_bit = (
            "live" if d.scope_live else "dead" if d.scope_live is False else "?"
        )
        excl = " SELF-EXCLUDED" if d.self_excluded else ""
        lines.append(
            f"  [{d.state}] pid={d.pid} origin={d.origin or '?'} {spawner} "
            f"{scope}({scope_bit}) children={d.child_count} "
            f"rss={d.rss_kb}kB{excl}"
        )
        lines.append(f"           {d.reason}")
    if n_orph == 0:
        lines.append("orphans: none")
    return "\n".join(lines)


def run_orphan_daemons_report(
    *,
    proc_root: Path = Path("/proc"),
    caller_pid: Optional[int] = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Print the T1 report. Always exit 0 on a completed scan (read-only)."""
    result = scan_orphan_daemons(
        proc_root=proc_root, caller_pid=caller_pid, run=run,
    )
    print(format_report(result))
    return 0
