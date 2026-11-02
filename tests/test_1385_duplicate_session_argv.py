"""Card #1385 (Obligation #3604) — duplicate_session argv-position fix.

Ensures duplicate_session.is_session_for() counts the desktop daemon ONLY by
argv POSITION (argv[0] basename swarph-desktop or python argv[0] + argv[1..2] entry point),
matching how _is_swarph_argv() treats swarph residents.

ps_entries() reads true NUL-separated argv from /proc/<pid>/cmdline so that
words inside one quoted argument (e.g. `--accept "PASS = ... swarph-desktop ..."`)
are never split into whitespace tokens.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest
import swarph_cli.duplicate_session as ds


def test_bash_command_quoting_swarph_desktop_is_not_a_session():
    """Test (a): ["/bin/bash", "-c", "gh pr view 34 -R BrainSurfing-tech/swarph-desktop ...; swarph board obligations amend 3597 --as lab-ovh ..."]
    is NOT a session for lab-ovh."""
    argv_a = [
        "/bin/bash",
        "-c",
        "gh pr view 34 -R BrainSurfing-tech/swarph-desktop ...; swarph board obligations amend 3597 --as lab-ovh ...",
    ]
    assert not ds.is_session_for(argv_a, "lab-ovh")
    assert ds.duplicate_session_pids([(1754451, argv_a)], "lab-ovh", my_pid=100) == []

    # Even if whitespace-split, it must STILL not be counted as a session
    argv_a_split = " ".join(argv_a).split()
    assert not ds.is_session_for(argv_a_split, "lab-ovh")
    assert ds.duplicate_session_pids([(1754451, argv_a_split)], "lab-ovh", my_pid=100) == []


def test_board_cards_ask_mentioning_swarph_desktop_is_not_a_session():
    """Test (b): ["swarph", "board", "cards", "ask", "1380", "--accept", "PASS = ... swarph-desktop PR 32 ...", "--as", "lab-ovh"]
    is NOT a session for lab-ovh."""
    argv_b = [
        "swarph",
        "board",
        "cards",
        "ask",
        "1380",
        "--accept",
        "PASS = ... swarph-desktop PR 32 ...",
        "--as",
        "lab-ovh",
    ]
    assert not ds.is_session_for(argv_b, "lab-ovh")
    assert ds.duplicate_session_pids([(1833816, argv_b)], "lab-ovh", my_pid=100) == []

    # Even if whitespace-split, it must STILL not be counted as a session
    argv_b_split = " ".join(argv_b).split()
    assert not ds.is_session_for(argv_b_split, "lab-ovh")
    assert ds.duplicate_session_pids([(1833816, argv_b_split)], "lab-ovh", my_pid=100) == []


def test_synthetic_desktop_launcher_argv_with_as_appended_is_a_session():
    """Test (c): SYNTHETIC launcher argv with --as appended. The live daemon on lab-ovh runs ["/home/ubuntu/swarph-desktop/venv/bin/python", "run-desktop-8766.py"] with no --as; appending --as lab-ovh is a synthetic launcher argv that counts as a session for lab-ovh."""
    # Live daemon argv on lab-ovh with no --as is NOT a session for lab-ovh
    assert not ds.is_session_for(
        ["/home/ubuntu/swarph-desktop/venv/bin/python", "run-desktop-8766.py"],
        "lab-ovh",
    )

    argv_c = [
        "/home/ubuntu/swarph-desktop/venv/bin/python",
        "run-desktop-8766.py",
        "--as",
        "lab-ovh",
    ]
    assert ds.is_session_for(argv_c, "lab-ovh")
    assert not ds.is_session_for(argv_c, "other-cell")
    assert ds.duplicate_session_pids([(1800578, argv_c)], "lab-ovh", my_pid=100) == [1800578]
    assert ds.duplicate_session_pids([(1800578, argv_c)], "other-cell", my_pid=100) == []

    # Other valid desktop daemon entrypoint forms
    valid_forms = [
        ["swarph-desktop", "--as", "lab-ovh"],
        ["/home/ubuntu/swarph-desktop/venv/bin/swarph-desktop", "--as", "lab-ovh"],
        ["python3", "-m", "swarph_desktop", "--as", "lab-ovh"],
        ["/home/ubuntu/swarph-desktop/venv/bin/python", "-m", "swarph_desktop", "--as", "lab-ovh"],
        ["python3", "/opt/swarph/swarph-desktop", "--as", "lab-ovh"],
        ["python3", "/opt/swarph/swarph_desktop", "--as", "lab-ovh"],
        ["/usr/bin/python3", "/home/ubuntu/swarph-desktop/run-desktop-8766.py", "--as", "lab-ovh"],
    ]
    for form in valid_forms:
        assert ds.is_session_for(form, "lab-ovh"), f"Failed for {form}"
        assert not ds.is_session_for(form, "other-cell"), f"Unexpected match for {form}"


def test_swarph_monitor_start_is_a_session():
    """Test (d): swarph monitor start --as lab-ovh still IS a session."""
    argv_d = ["swarph", "monitor", "start", "--as", "lab-ovh"]
    assert ds.is_session_for(argv_d, "lab-ovh")
    assert not ds.is_session_for(argv_d, "other-cell")
    assert ds.duplicate_session_pids([(200, argv_d)], "lab-ovh", my_pid=100) == [200]
    assert ds.duplicate_session_pids([(200, argv_d)], "other-cell", my_pid=100) == []


def test_ps_entries_proc_cmdline_nul_split(tmp_path, monkeypatch):
    """Test reading true NUL-separated argv from /proc/<pid>/cmdline via synthetic proc_root."""
    # (a) bash command
    p_a = tmp_path / "1754451"
    p_a.mkdir()
    argv_a = [
        "/bin/bash",
        "-c",
        "gh pr view 34 -R BrainSurfing-tech/swarph-desktop ...; swarph board obligations amend 3597 --as lab-ovh ...",
    ]
    (p_a / "cmdline").write_bytes(b"\x00".join(tok.encode("utf-8") for tok in argv_a) + b"\x00")

    # (b) swarph board cards ask
    p_b = tmp_path / "1833816"
    p_b.mkdir()
    argv_b = [
        "swarph",
        "board",
        "cards",
        "ask",
        "1380",
        "--accept",
        "PASS = ... swarph-desktop PR 32 ...",
        "--as",
        "lab-ovh",
    ]
    (p_b / "cmdline").write_bytes(b"\x00".join(tok.encode("utf-8") for tok in argv_b) + b"\x00")

    # (c) synthetic desktop launcher argv with --as appended
    p_c = tmp_path / "1800578"
    p_c.mkdir()
    argv_c = [
        "/home/ubuntu/swarph-desktop/venv/bin/python",
        "run-desktop-8766.py",
        "--as",
        "lab-ovh",
    ]
    (p_c / "cmdline").write_bytes(b"\x00".join(tok.encode("utf-8") for tok in argv_c) + b"\x00")

    # Non-digit dir and empty cmdline (kernel thread) to verify edge cases
    (tmp_path / "self").mkdir()
    p_kthread = tmp_path / "2"
    p_kthread.mkdir()
    (p_kthread / "cmdline").write_bytes(b"")

    # 1. Call via explicit proc_root parameter
    entries = ds.ps_entries(proc_root=tmp_path)
    by_pid = dict(entries)
    assert 1833816 in by_pid
    assert by_pid[1833816] == argv_b
    assert "PASS = ... swarph-desktop PR 32 ..." in by_pid[1833816]
    assert 2 not in by_pid  # empty cmdline skipped
    assert ds.duplicate_session_pids(entries, "lab-ovh", my_pid=100) == [1800578]

    # 2. Call via monkeypatched _PROC_ROOT
    monkeypatch.setattr(ds, "_PROC_ROOT", str(tmp_path))
    entries_mp = ds.ps_entries()
    assert ds.duplicate_session_pids(entries_mp, "lab-ovh", my_pid=100) == [1800578]


def test_live_proc_preserves_quoted_args():
    """Test against live /proc on Linux with a spawned subprocess."""
    if not os.path.isdir("/proc"):
        pytest.skip("/proc not available on this platform")

    quoted_arg = "PASS = ... swarph-desktop PR 32 ..."
    proc = subprocess.Popen([
        sys.executable,
        "-c",
        "import time; time.sleep(10)",
        "--accept",
        quoted_arg,
        "--as",
        "lab-ovh",
    ])
    try:
        entries = ds.ps_entries()
        match = [argv for pid, argv in entries if pid == proc.pid]
        assert match, f"Process {proc.pid} not found in ps_entries"
        found_argv = match[0]
        assert quoted_arg in found_argv, f"Quoted argument was split: {found_argv}"
        assert not ds.is_session_for(found_argv, "lab-ovh")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
