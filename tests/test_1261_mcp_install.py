"""Card #1261. Each case fails on the parent by assertion."""
import io
import json
import os
import select
import subprocess
import sys

from swarph_cli.commands.add import _default_pip_runner, parse_uri, run_add
from swarph_cli.commands import mcp_server
from swarph_cli.dm_frame import frame_text

_URI = "swarph://hook/swarph-builtin/cell-resilience"
_BREAKS = ("\n", "\r", "\u2028", "\u2029", "\u0085", "\x00", "\x1f", "\x7f")


def _refused(uri: str) -> bool:
    try:
        parse_uri(uri)
    except ValueError:
        return True
    return False


def test_control_characters_are_refused_in_every_field():
    """C0, DEL, and the Unicode line separators are refused in the name,
    the publisher, @version, #sha256, and a query."""
    assert parse_uri(_URI).name == "cell-resilience"
    templates = (
        "swarph://hook/swarph-builtin/cell{ch}resilience",
        "swarph://hook/pub{ch}lisher/cell-resilience",
        "swarph://hook/swarph-builtin/cell-resilience@1{ch}0",
        "swarph://hook/swarph-builtin/cell-resilience#ab{ch}c",
        "swarph://hook/swarph-builtin/cell-resilience?q={ch}",
    )
    for ch in _BREAKS:
        for template in templates:
            assert _refused(template.format(ch=ch)) is True


def test_url_install_asks_before_writing(tmp_path, monkeypatch):
    """A swarph:// install with no --yes writes nothing until the answer
    is y. That is the URL-click path: the desktop handler does not pass
    --yes."""
    settings = tmp_path / "settings.json"
    hooks = tmp_path / "hooks"
    monkeypatch.setattr(sys, "stdin", io.StringIO("n\n"))
    rc = run_add([_URI], settings_path=settings, hooks_home=hooks)
    assert rc != 0
    assert not settings.exists()
    assert list(hooks.glob("*")) == []
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    rc = run_add([_URI], settings_path=settings, hooks_home=hooks)
    assert rc == 0
    assert settings.exists()


def test_mcp_add_cannot_install_from_model_arguments():
    called = {"n": 0}

    def boom(*args, **kwargs):
        called["n"] += 1
        return 0

    original = mcp_server.add.run_add
    mcp_server.add.run_add = boom
    try:
        out = mcp_server._add(_URI)
    finally:
        mcp_server.add.run_add = original
    assert called["n"] == 0
    assert out["installed"] is False
    assert out["code"] == 2


def _read_line(proc, timeout):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise AssertionError("mcp server produced no stdout")
    return proc.stdout.readline()


def test_mcp_server_add_writes_nothing_but_protocol_frames(tmp_path):
    """The tool call's stdout is only JSON-RPC lines. Install text and a
    child's stdout would show up here, because both share fd 1."""
    env = {
        **os.environ,
        "PYTHONPATH": os.path.abspath("src"),
        "PYTHONUNBUFFERED": "1",
        "HOME": str(tmp_path),
    }
    env.pop("SWARPH_SELF", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarph_cli", "mcp-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        text=True,
    )
    lines = []
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "card-1261", "version": "0"},
            },
        }) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "swarph_add", "arguments": {"uri": _URI}},
        }) + "\n")
        proc.stdin.flush()
        seen_call = False
        for _ in range(20):
            line = _read_line(proc, 15)
            lines.append(line)
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                msg = None
            if isinstance(msg, dict) and msg.get("id") == 2:
                seen_call = True
                break
        proc.stdin.close()
        # Drain whatever the exit flush still holds.
        leftover = ""
        if proc.stdout is not None:
            ready, _, _ = select.select([proc.stdout], [], [], 2)
            if ready:
                leftover = proc.stdout.read()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    text = "".join(lines) + (leftover or "")
    assert seen_call is True
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = None
        assert isinstance(parsed, dict) and parsed.get("jsonrpc") == "2.0"


def test_pip_does_not_inherit_stdout(monkeypatch):
    seen = {}

    class _Done:
        returncode = 0
        stdout = b""

    def fake(argv, **kwargs):
        seen["stdout"] = kwargs.get("stdout")
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake)
    assert _default_pip_runner(["install", "example"]) == 0
    assert seen["stdout"] is subprocess.PIPE


def test_frame_and_dm_tools_share_one_function():
    """Every line-break and a ] in from_node are gone on both the channel
    sentence and the MCP content, and those two are the same string."""
    body = "a" + "".join(_BREAKS) + "b"
    row = {
        "id": 7, "from_node": "eve]", "to_node": "lab-ovh", "kind": "fyi",
        "content": body, "created_at": "2026-10-25T00:00:00Z",
    }
    text = frame_text(row)
    for ch in _BREAKS:
        assert ch not in text
    assert "from eve>" in text
    assert "eve]" not in text
    assert mcp_server._lean(row)["content"] == text
    # The source characters survive as spaces, in order, inside the frame.
    assert "a" in text and "b" in text
    assert text.index("a") < text.index("b")
