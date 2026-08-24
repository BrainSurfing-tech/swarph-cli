"""#566 — `swarph install-postcompact-hook` ships the #549 wiring as a repo
payload: harness-correct scripts + registration, per box.

Until this verb existed the three cursor hook scripts lived only in
~/.cursor/hooks/ on one box and were DM'd to cursor-win by hand (msgs
26516-26518). These tests pin: the payload is package data (the 0.39.3
lesson — undeclared data passes every in-tree test and fails on every
installed wheel), the per-harness registration shapes, the #527-shaped
refusal of an explicit --cell into a box-global config, idempotency,
uninstall, and the scripts' actual behavior under bash (flag handshake,
BOM-tolerant parsing — the PS5.1 double-BOM cursor-win measured).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from swarph_cli.commands import install_postcompact_hook as M


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if os.name == "nt":
        # Path.home() on Windows reads USERPROFILE, not HOME — without this
        # the verb writes to the real profile while the test reads the fake
        # one (measured on the windows-latest lane).
        monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _run(argv, monkeypatch, **env):
    for k in ("SWARPH_SELF", "SWARPH_CELL", "SWARPH_MEMORY_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return M.run_install_postcompact_hook(argv)


# ── payload packaging (the 0.39.3 regression class) ─────────────────────────

def test_payloads_are_package_data():
    for name in ("precompact-flag.sh", "posttooluse-recall.sh",
                 "posttooluse-emit.sh", "sessionstart-recall.sh",
                 "posttooluse-emit-claude.sh", "_shim.cmd"):
        text = M._payload_text(name)
        assert text.strip(), f"{name} is empty or missing from the package"


def test_payloads_carry_the_failure_mode_invariant():
    """Every hook script must exit 0 on every path — a hook that fails a tool
    result because the timeline is unreachable inverts every priority."""
    for name in ("precompact-flag.sh", "posttooluse-recall.sh",
                 "posttooluse-emit.sh", "sessionstart-recall.sh",
                 "posttooluse-emit-claude.sh"):
        assert M._payload_text(name).rstrip().endswith("exit 0"), name


# ── cursor install ───────────────────────────────────────────────────────────

def test_cursor_install_writes_scripts_and_registration(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    rc = _run(["--harness", "cursor"], monkeypatch, SWARPH_SELF="test-cell")
    assert rc == 0

    hooks_json = home / ".cursor" / "hooks.json"
    cfg = json.loads(hooks_json.read_text())
    assert cfg["version"] == 1
    assert len(cfg["hooks"]["preCompact"]) == 1
    post = cfg["hooks"]["postToolUse"]
    assert len(post) == 2
    assert post[1]["matcher"] == "Write|Edit|StrReplace"

    scripts = home / ".cursor" / "hooks" / "swarph-postcompact"
    for name in ("precompact-flag.sh", "posttooluse-recall.sh",
                 "posttooluse-emit.sh"):
        body = (scripts / name).read_text()
        assert "@PYTHON@" not in body and "@ENV_PREFIX@" not in body
        assert os.access(scripts / name, os.X_OK), f"{name} not executable"
    emit = (scripts / "posttooluse-emit.sh").read_text()
    assert "SWARPH_SELF=test-cell" in emit
    # the cursor→claude shape adapter survived installation
    assert 'ti.get("path")' in emit


def test_install_is_idempotent(tmp_path, monkeypatch, capsys):
    _home(tmp_path, monkeypatch)
    _run(["--harness", "cursor"], monkeypatch)
    first = json.loads((tmp_path / "home/.cursor/hooks.json").read_text())
    capsys.readouterr()
    rc = _run(["--harness", "cursor"], monkeypatch)
    assert rc == 0
    assert "already up to date" in capsys.readouterr().err
    assert json.loads(
        (tmp_path / "home/.cursor/hooks.json").read_text()) == first


def test_uninstall_removes_registration_and_scripts(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _run(["--harness", "cursor"], monkeypatch)
    rc = _run(["--harness", "cursor", "--uninstall"], monkeypatch)
    assert rc == 0
    cfg = json.loads((home / ".cursor/hooks.json").read_text())
    assert cfg["hooks"]["preCompact"] == []
    assert cfg["hooks"]["postToolUse"] == []
    assert not (home / ".cursor/hooks/swarph-postcompact").exists()


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    rc = _run(["--harness", "cursor", "--dry-run"], monkeypatch)
    assert rc == 0
    assert not (home / ".cursor").exists()


def test_cell_with_box_global_scope_is_refused(tmp_path, monkeypatch):
    """The #527 shape, one verb over: an explicit --cell baked into a
    box-global file arms one cell's identity for every session on the box."""
    home = _home(tmp_path, monkeypatch)
    rc = _run(["--harness", "cursor", "--cell", "someone-else"], monkeypatch)
    assert rc == 2
    assert not (home / ".cursor").exists()


def test_cell_with_project_scope_is_accepted(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    rc = _run(["--harness", "cursor", "--scope", "project",
               "--cell", "proj-cell"], monkeypatch)
    assert rc == 0
    emit = (proj / ".cursor/hooks/swarph-postcompact/posttooluse-emit.sh"
            ).read_text()
    assert "SWARPH_SELF=proj-cell" in emit


def test_project_scope_from_home_dir_is_still_box_global(tmp_path, monkeypatch):
    """--scope project run from $HOME lands on the SAME box-global file —
    compare paths, not flags (#527's path-comparison rule)."""
    home = _home(tmp_path, monkeypatch)
    monkeypatch.chdir(home)
    rc = _run(["--harness", "cursor", "--scope", "project",
               "--cell", "sneaky"], monkeypatch)
    assert rc == 2


def test_memory_dir_derivation_for_cursor(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    slug = M._cwd_slug()
    mem = home / ".cursor-cell" / "projects" / slug / "memories"
    mem.mkdir(parents=True)
    rc = _run(["--harness", "cursor", "--scope", "project"], monkeypatch)
    assert rc == 0
    emit = (proj / ".cursor/hooks/swarph-postcompact/posttooluse-emit.sh"
            ).read_text()
    assert f"SWARPH_MEMORY_DIR={shlex.quote(str(mem))}" in emit


def test_memory_dir_env_beats_convention_flag_beats_env(tmp_path, monkeypatch):
    """Precedence: --memory-dir > SWARPH_MEMORY_DIR env > cursor-cell
    convention. The env source mirrors how the emit hook itself resolves, so
    cells that already export it get it baked without a flag."""
    home = _home(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    slug = M._cwd_slug()
    (home / ".cursor-cell" / "projects" / slug / "memories").mkdir(parents=True)
    envdir = tmp_path / "envmem"
    envdir.mkdir()
    rc = _run(["--harness", "cursor", "--scope", "project"], monkeypatch,
              SWARPH_MEMORY_DIR=str(envdir))
    assert rc == 0
    emit = (proj / ".cursor/hooks/swarph-postcompact/posttooluse-emit.sh"
            ).read_text()
    assert f"SWARPH_MEMORY_DIR={shlex.quote(str(envdir))}" in emit
    flagdir = tmp_path / "flagmem"
    flagdir.mkdir()
    rc = _run(["--harness", "cursor", "--scope", "project",
               "--memory-dir", str(flagdir)], monkeypatch,
              SWARPH_MEMORY_DIR=str(envdir))
    assert rc == 0
    emit = (proj / ".cursor/hooks/swarph-postcompact/posttooluse-emit.sh"
            ).read_text()
    assert f"SWARPH_MEMORY_DIR={shlex.quote(str(flagdir))}" in emit


# ── claude install ───────────────────────────────────────────────────────────

def test_claude_install_shape(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    rc = _run(["--harness", "claude"], monkeypatch, SWARPH_SELF="claude-cell")
    assert rc == 0
    cfg = json.loads((home / ".claude/settings.json").read_text())
    # Windows harnesses spawn hooks via cmd and never run the shebang, so
    # registration points at the .cmd shim there; elsewhere the .sh.
    ext = ".cmd" if os.name == "nt" else ".sh"
    ss = cfg["hooks"]["SessionStart"]
    assert f"sessionstart-recall{ext}" in ss[0]["hooks"][0]["command"]
    ptu = cfg["hooks"]["PostToolUse"]
    assert ptu[0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    assert f"posttooluse-emit-claude{ext}" in ptu[0]["hooks"][0]["command"]
    assert "preCompact" not in cfg["hooks"]  # claude needs no flag handshake


def test_unknown_harness_refused(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    rc = _run(["--harness", "muse"], monkeypatch)
    assert rc == 2
    assert not (home / ".cursor").exists() and not (home / ".claude").exists()


# ── the scripts actually work (bash required) ────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="payload scripts are bash")
def test_flag_handshake_end_to_end(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _run(["--harness", "cursor"], monkeypatch)
    scripts = home / ".cursor" / "hooks" / "swarph-postcompact"
    env = dict(os.environ, TMPDIR=str(tmp_path))

    conv = "conv-566"
    r = subprocess.run(["bash", str(scripts / "precompact-flag.sh")],
                       input=json.dumps({"conversation_id": conv}),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and r.stdout.strip() == "{}"
    assert (tmp_path / f"cursor-compact-pending-{conv}").exists()

    # recall WITHOUT a pending flag: no-op
    r = subprocess.run(["bash", str(scripts / "posttooluse-recall.sh")],
                       input=json.dumps({"conversation_id": "other"}),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout) == {}
    # WITH the flag: consumed, and the output is the cursor envelope
    r = subprocess.run(["bash", str(scripts / "posttooluse-recall.sh")],
                       input=json.dumps({"conversation_id": conv}),
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert not (tmp_path / f"cursor-compact-pending-{conv}").exists()
    out = json.loads(r.stdout)
    assert set(out) <= {"additional_context"}


@pytest.mark.skipif(os.name == "nt", reason="payload scripts are bash")
def test_emit_script_tolerates_bomd_stdin(tmp_path, monkeypatch):
    """cursor-win measured PS 5.1 native pipes prefixing a (double) UTF-8 BOM;
    json.loads raises on it and the exit-0 invariant made that an INVISIBLE
    no-op. The adapter strips before parse — and this test can TELL, because
    a successful parse emits to a stub gateway while a broken one emits
    nothing."""
    import http.server
    import threading

    received = []

    class Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, json.loads(body)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ts": "2026-08-23T00:00:00Z"}).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    home = _home(tmp_path, monkeypatch)
    mem = tmp_path / "memories"
    mem.mkdir()
    (mem / "foo.md").write_text("# foo\na test memory\n")
    monkeypatch.chdir(tmp_path)
    rc = _run(["--harness", "cursor", "--scope", "project"], monkeypatch,
              SWARPH_SELF="emit-cell")
    assert rc == 0
    script = tmp_path / ".cursor/hooks/swarph-postcompact/posttooluse-emit.sh"
    assert script.exists()
    # per-identity token so _log_via_gateway can authenticate to the stub
    tokdir = home / ".config" / "swarph"
    tokdir.mkdir(parents=True)
    (tokdir / "emit-cell.peer_token").write_text("test-token")

    env = dict(os.environ,
               SWARPH_SELF="emit-cell",
               SWARPH_MEMORY_DIR=str(mem),
               SWARPH_GATEWAY=f"http://127.0.0.1:{srv.server_port}",
               SWARPH_EMIT_STATE=str(tmp_path / "emit-state.json"),
               HOME=str(home))
    envelope = json.dumps({"tool_input": {"path": str(mem / "foo.md")}})
    r = subprocess.run(
        ["bash", str(script)],
        input="\ufeff" + "\ufeff" + envelope,  # the measured double BOM
        capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert json.loads(r.stdout) == {}
    srv.shutdown()

    assert received, "BOM'd stdin never reached the gateway — the adapter's " \
        "parse failed silently (the cursor-win regression)"
    assert received[0][0] == "/highlights"
    assert received[0][1]["memory"] == "[[foo]]"
    assert received[0][1]["cell"] == "emit-cell"


@pytest.mark.skipif(os.name == "nt", reason="payload scripts are bash")
def test_claude_recall_fires_only_on_compact_source(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _run(["--harness", "claude"], monkeypatch)
    script = home / ".claude/hooks/swarph-postcompact/sessionstart-recall.sh"
    for source, fires in (("startup", False), ("resume", False),
                          ("clear", False), ("compact", True)):
        r = subprocess.run(["bash", str(script)],
                           input=json.dumps({"session_id": "s",
                                             "source": source}),
                           capture_output=True, text=True)
        assert r.returncode == 0
        out = json.loads(r.stdout)
        if fires:
            # recall content depends on the local timeline; the ENVELOPE is
            # what this test pins
            assert set(out) <= {"hookSpecificOutput"}
            if out:
                assert out["hookSpecificOutput"]["hookEventName"] == \
                    "SessionStart"
        else:
            assert out == {}


# ── Windows shim bash resolution (cursor-win accept run, 2026-08-24) ───────

def test_windows_shim_bakes_the_resolved_git_bash(tmp_path, monkeypatch):
    """The shim must invoke the ABSOLUTE Git-bash resolved at install time.
    Bare 'bash' resolves to the System32 WSL launcher on WSL boxes and the
    hook silently no-ops — cursor-win's accept evidence, DM 27816."""
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(M, "_is_windows", lambda: True)
    monkeypatch.setattr(
        "swarph_cli.commands.hooks._find_windows_bash",
        lambda: "C:/Program Files/Git/bin/bash.exe",
    )
    rc = _run(["--harness", "cursor"], monkeypatch, SWARPH_SELF="test-cell")
    assert rc == 0

    scripts = home / ".cursor" / "hooks" / "swarph-postcompact"
    shim = (scripts / "precompact-flag.cmd").read_text()
    assert '"C:/Program Files/Git/bin/bash.exe"' in shim
    assert "@BASH@" not in shim
    # the registered command points at the shim, not the .sh
    cfg = json.loads((home / ".cursor" / "hooks.json").read_text())
    assert cfg["hooks"]["preCompact"][0]["command"].endswith("precompact-flag.cmd")


def test_windows_install_refuses_when_no_usable_bash(tmp_path, monkeypatch, capsys):
    """Fail closed on the write (hooks.py's rule): an unresolvable bash means
    every hook would silently no-op, so the install refuses BEFORE writing."""
    home = _home(tmp_path, monkeypatch)
    monkeypatch.setattr(M, "_is_windows", lambda: True)
    monkeypatch.setattr(
        "swarph_cli.commands.hooks._find_windows_bash", lambda: None
    )
    rc = _run(["--harness", "cursor"], monkeypatch, SWARPH_SELF="test-cell")
    assert rc == 2
    assert "no usable bash" in capsys.readouterr().err
    assert not (home / ".cursor" / "hooks.json").exists()
    assert not (home / ".cursor" / "hooks" / "swarph-postcompact").exists()
