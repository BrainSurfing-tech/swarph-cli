"""Card #195 — "could not determine" must never be reported as "dead".

workstation-lc, measured against a monitor that was demonstrably draining on
Windows (python 3.14.3 / Win11):

    pid 6120 alive per Windows ................. True
    os.kill(6120, 0) from another process ...... OSError [WinError 87]
                                                 -> `except OSError: return False`
    pid 6120 after the probe ................... STILL ALIVE

`os.kill(pid, 0)` raises when probing a process the caller did NOT spawn — the
monitor worker is a grandchild of a detached cmd.exe, a different session and
handle-rights context. His first throwaway test PASSED because he probed his own
child; a child-process control cannot surface this, which is exactly why it read
safe.

ONE SWALLOW PRODUCED BOTH SYMPTOMS: `monitor status` reported "not running" about
every healthy monitor, and the single-instance guard believed nothing was running,
so it never blocked a second start.
"""
import os
import sys

import pytest

from swarph_cli.commands import mesh


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


def test_undetermined_is_unknown_not_dead(monkeypatch):
    """THE REGRESSION. A bare OSError means we could not tell — not that it died."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(mesh.os, "kill", _raise(OSError(87, "Parametre incorrect")))
    assert mesh._process_liveness(1234) == mesh.LIVENESS_UNKNOWN


def test_undetermined_counts_as_occupied_for_the_guard(monkeypatch):
    """A blocked start is visible and recoverable; a duplicate is silent and gives
    two writers over one cursor. So UNKNOWN must read as alive to the guard."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(mesh.os, "kill", _raise(OSError(87, "Parametre incorrect")))
    assert mesh._process_alive(1234) is True, (
        "an undetermined probe was reported as dead — this is the swallow that made "
        "`monitor status` claim every healthy Windows monitor was not running")


def test_genuinely_absent_process_is_dead(monkeypatch):
    """The fix must not blunt the real negative: a missing pid is still dead."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(mesh.os, "kill", _raise(ProcessLookupError()))
    assert mesh._process_liveness(1234) == mesh.LIVENESS_DEAD
    assert mesh._process_alive(1234) is False


def test_permission_denied_is_alive(monkeypatch):
    """A denied probe PROVES the pid exists."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(mesh.os, "kill", _raise(PermissionError()))
    assert mesh._process_liveness(1234) == mesh.LIVENESS_ALIVE


def test_live_process_is_alive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert mesh._process_liveness(os.getpid()) == mesh.LIVENESS_ALIVE


def test_windows_never_uses_os_kill(monkeypatch):
    """os.kill must not be on the Windows path at all — it is neither a reliable
    probe there nor, historically, a safe one."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(mesh.os, "kill", _raise(AssertionError("os.kill used on win32")))
    monkeypatch.setattr(mesh, "_windows_liveness", lambda pid: mesh.LIVENESS_ALIVE)
    assert mesh._process_liveness(1234) == mesh.LIVENESS_ALIVE


def test_windows_probe_degrades_to_unknown_never_dead(monkeypatch):
    """If ctypes/kernel32 is unavailable or misbehaves, the answer is UNKNOWN.

    lab-ovh is Linux and cannot execute the real OpenProcess path; what is pinned
    here is the DEGRADATION DIRECTION, which is the property that matters — an
    unavailable probe must not manufacture a 'dead'.
    """
    import builtins
    real_import = builtins.__import__

    def _no_ctypes(name, *a, **k):
        if name == "ctypes":
            raise ImportError("no ctypes")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_ctypes)
    assert mesh._windows_liveness(1234) == mesh.LIVENESS_UNKNOWN
