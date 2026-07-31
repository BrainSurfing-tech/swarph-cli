"""Card #195 — the pidfile must name the process that actually polls.

workstation-lc, while the monitor was demonstrably draining on Windows:
    monitor.pid            -> "pid": 28936
    live swarph.exe monitor -> 39608, 23740
    28936 matched nothing alive
So `monitor status` reported "not running (stale pidfile: the recorded pid is
gone)" about a HEALTHY monitor. The liveness probe answered CORRECTLY about a pid
that really was gone -- the defect is upstream, in whatever pid got written.

The same divergence explains the duplicate spawns: a pidfile that never names the
live worker cannot enforce single-instance, so it neither blocks a second start nor
reports the first.

>>> THESE TESTS CANNOT FAIL ON LINUX FOR THE REAL BUG, AND THAT IS THE POINT. <<<
write_pidfile records os.getpid() and the same process enters the loop, so the
invariant holds here by construction. What is pinned instead is that the SELF-CHECK
itself works -- that a mismatch is detected and reported, and that the check can
never wedge the monitor -- so the next Windows run produces evidence rather than
lab guessing at the mechanism from a box without the platform.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarph_cli.commands import mesh


def _state(tmp_path):
    return SimpleNamespace(state_dir=Path(tmp_path), log_prefix="[monitor]")


def _write_pid(tmp_path, pid):
    p = Path(tmp_path) / mesh._MONITOR_PIDFILE
    p.write_text(json.dumps({"pid": pid, "self": "lab-ovh", "sinks": ["pull"],
                             "poll_s": 30, "started_at": 0, "cmdline": None}),
                 encoding="utf-8")
    return p


def test_matching_pid_is_silent(tmp_path):
    """The healthy case must produce NO output -- a warning nobody can act on is noise."""
    _write_pid(tmp_path, os.getpid())
    assert mesh._pidfile_identity_selfcheck(_state(tmp_path)) is None


def test_mismatched_pid_is_reported(tmp_path):
    """THE REGRESSION the Windows cells hit: recorded pid != the polling process."""
    _write_pid(tmp_path, os.getpid() + 999_000)
    msg = mesh._pidfile_identity_selfcheck(_state(tmp_path))
    assert msg is not None, "a pidfile naming another process was not reported"
    assert "PIDFILE IDENTITY MISMATCH" in msg
    assert str(os.getpid()) in msg, "the live pid must be in the message to be actionable"
    assert "#195" in msg, "the report must name the card so a peer knows where to send it"


def test_absent_pidfile_is_not_an_error(tmp_path):
    """`--once` writes no pidfile; that is not a mismatch."""
    assert mesh._pidfile_identity_selfcheck(_state(tmp_path)) is None


def test_corrupt_pidfile_never_raises(tmp_path):
    """A diagnostic that can wedge the monitor is worse than the bug it hunts."""
    (Path(tmp_path) / mesh._MONITOR_PIDFILE).write_text("{not json", encoding="utf-8")
    assert mesh._pidfile_identity_selfcheck(_state(tmp_path)) is None


def test_unreadable_state_dir_never_raises(tmp_path):
    bad = SimpleNamespace(state_dir=Path("/nonexistent/nope"), log_prefix="[monitor]")
    assert mesh._pidfile_identity_selfcheck(bad) is None
