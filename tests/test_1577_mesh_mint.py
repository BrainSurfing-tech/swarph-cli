"""#1577 — ``swarph mesh mint <name> --out FILE`` against a scratch gateway.

The bundled server has no /whoami. The route added here is test-only; it is
not a change to gateway/server.py. A fresh mesh ignores defer_token_mint
while zero peers are ratified, so a minted token does not prove the flag —
the recorded register body does.
"""
from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import socket
import stat
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")
pytest.importorskip("uvicorn")

from fastapi import Header  # noqa: E402


class _CaptureRegister:
    """Replay POST /peers/register so the endpoint still sees the body."""

    posts: list[dict] = []

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/peers/register"
        ):
            await self.app(scope, receive, send)
            return
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        try:
            self.posts.append(json.loads(body.decode() or "{}"))
        except json.JSONDecodeError:
            self.posts.append({})
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


def _load_server(monkeypatch, db_path: Path):
    import importlib

    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("MESH_DB_PATH", str(db_path))
    monkeypatch.setenv("MESH_CALLER_BINDING_ENFORCE", "1")
    from swarph_cli.gateway import server

    importlib.reload(server)
    monkeypatch.setattr(server, "MESH_CALLER_BINDING_ENFORCE", True, raising=False)
    return server


def _install_test_whoami(server) -> None:
    @server.app.get("/whoami")
    async def _whoami(authorization: Optional[str] = Header(None)):
        auth = server._authorize(authorization)
        return {"peer": auth.peer, "key_generation": auth.key_generation}


@pytest.fixture
def scratch_gateway(monkeypatch, tmp_path):
    import uvicorn

    for name in ("mesh-gateway", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    db_path = tmp_path / "gw" / "mesh.db"
    server = _load_server(monkeypatch, db_path)
    _install_test_whoami(server)
    posts: list[dict] = []
    _CaptureRegister.posts = posts
    server.app.add_middleware(_CaptureRegister)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        server.app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        access_log=False,
    )

    class _ThreadServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    uv = _ThreadServer(config)
    boom: list[BaseException] = []

    def _serve() -> None:
        try:
            uv.run(sockets=[sock])
        except BaseException as exc:  # noqa: BLE001 — surface startup death to the test
            boom.append(exc)

    thread = threading.Thread(target=_serve, name="mint-1577-gateway", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 10
    last_error = ""
    while time.time() < deadline:
        if boom:
            break
        try:
            with urllib.request.urlopen(base + "/health", timeout=0.3) as resp:
                if resp.status == 200:
                    last_error = ""
                    break
        except Exception as exc:  # noqa: BLE001 — poll until the socket accepts
            last_error = f"{type(exc).__name__}"
        time.sleep(0.05)
    else:
        uv.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError(
            f"scratch gateway did not accept connections ({last_error or 'timeout'})"
        )
    if boom or last_error:
        uv.should_exit = True
        thread.join(timeout=3)
        kind = type(boom[0]).__name__ if boom else last_error
        raise RuntimeError(f"scratch gateway failed to start ({kind})")

    # Never inherit a live gateway URL. The CLI is pointed at this socket.
    monkeypatch.setenv("MESH_GATEWAY_URL", base)
    try:
        yield {"base": base, "posts": posts}
    finally:
        uv.should_exit = True
        thread.join(timeout=5)
        try:
            sock.close()
        except OSError:
            pass


def _assert_silent(text: str, secret: str) -> None:
    if secret and secret in text:
        raise AssertionError("invocation output contained the peer token")


def _whoami(base: str, token: str) -> dict:
    req = urllib.request.Request(
        base + "/whoami",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def test_mint_writer_is_exclusive_and_does_not_truncate():
    from swarph_cli.commands.mesh import _run_mint, _write_minted_token_exclusive

    writer = inspect.getsource(_write_minted_token_exclusive)
    assert "os.O_CREAT | os.O_EXCL | os.O_WRONLY" in writer
    assert "O_TRUNC" not in writer
    assert "write_secret_file(" not in writer
    assert writer.index("os.fchmod") < writer.index("fp.write")

    src = inspect.getsource(_run_mint)
    for banned in ("SWARPH_SELF", "LOGNAME", "getpass", "write_secret_file(", "O_TRUNC"):
        assert banned not in src
    assert '"defer_token_mint": False' in src


def test_mesh_mint_operator_verb(monkeypatch, tmp_path, capsys, scratch_gateway):
    from swarph_cli.commands import mesh

    base = scratch_gateway["base"]
    posts: list[dict] = scratch_gateway["posts"]
    home = tmp_path / "decoy-home"
    home.mkdir()
    real_home = Path(os.environ.get("HOME", str(Path.home())))
    # HOME may already have been the real one; record suspect paths before
    # the command runs so a stray write into the operator's config is visible.
    suspects = [
        real_home / ".config" / "swarph" / "mint-cell.peer_token",
        real_home / ".config" / "swarph" / "other-cell.peer_token",
    ]
    suspect_existed = {p: p.exists() for p in suspects}

    monkeypatch.setenv("SWARPH_SELF", "decoy-self")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOGNAME", "decoy-logname")
    monkeypatch.setenv("USER", "decoy-user")
    monkeypatch.setenv("MESH_GATEWAY_URL", base)

    name = "mint-cell"
    preexisting = tmp_path / "already.token"
    preexisting.write_text("sentinel-not-a-token\n", encoding="utf-8")
    original = preexisting.read_bytes()
    out_path = tmp_path / "mint-out" / "cell.peer_token"
    second = tmp_path / "second.token"
    other = tmp_path / "other.token"

    old_umask = os.umask(0)
    secrets: list[str] = []
    try:
        capsys.readouterr()

        def invoke(argv: list[str]) -> tuple[int, str, str]:
            rc = mesh.run_mesh(argv)
            captured = capsys.readouterr()
            return rc, captured.out, captured.err

        rc, out, err = invoke([
            "mint", name, "--out", str(preexisting), "--gateway", base,
        ])
        assert rc != 0
        assert preexisting.read_bytes() == original
        assert posts == []
        assert "already exists" in err
        blob = out + err

        rc, out, err = invoke([
            "mint", name, "--out", str(out_path), "--gateway", base,
        ])
        text = out + err
        if rc != 0:
            raise AssertionError("mint failed")
        assert len(posts) == 1
        # A minted token does not prove defer_token_mint: a fresh mesh mints
        # anyway while nobody is ratified. The POST body is the proof.
        assert posts[0]["defer_token_mint"] is False
        assert posts[0]["name"] == name
        assert posts[0]["name"] != "decoy-self"
        assert posts[0]["url"] == f"http://{name}:8787"
        assert posts[0]["capabilities"] == {"can_claim_tasks": True}
        assert "swarph_cli_version" not in posts[0].get("capabilities", {})

        assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(out_path.parent.stat().st_mode) == 0o700
        raw = out_path.read_text(encoding="utf-8")
        if not raw.endswith("\n") or "\n" in raw[:-1]:
            raise AssertionError("token file must be the token plus one trailing newline")
        secret = raw[:-1]
        secrets.append(secret)
        _assert_silent(text, secret)
        assert str(out_path) in out
        who = _whoami(base, secret)
        assert who["peer"] == name
        assert type(who["key_generation"]) is int
        assert f"key_generation={who['key_generation']}" in out
        home_files = [p for p in home.rglob("*") if p.is_file()]
        assert home_files == [], "token was written under HOME"
        before = out_path.read_bytes()
        blob += text

        rc, out, err = invoke([
            "mint", name, "--out", str(second), "--gateway", base,
        ])
        text = out + err
        assert rc != 0
        assert not second.exists()
        if out_path.read_bytes() != before:
            raise AssertionError("first token file changed")
        assert "DELETE /peers/" in text
        assert (
            f"An operator must DELETE /peers/{name} first, which purges the "
            "old token and writes a revocation row; the next register then "
            "mints a fresh generation."
        ) in text
        _assert_silent(text, secret)
        blob += text

        rc, out, err = invoke([
            "mint", "other-cell", "--out", str(other), "--gateway", base,
            "--token-file", str(out_path),
        ])
        text = out + err
        assert rc != 0
        assert not other.exists()
        assert "per-peer token" in text
        assert "operator credential" in text
        assert "--token-file <operator-token>" in text
        assert "not a cell's own peer token" in text
        _assert_silent(text, secret)
        blob += text

        for post in posts:
            assert post.get("defer_token_mint") is False
        _assert_silent(blob, secret)
    finally:
        os.umask(old_umask)
        for path, existed in suspect_existed.items():
            if path.exists() != existed:
                raise AssertionError(f"mint wrote outside --out: {path}")
