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


def _logger_snapshot(names: tuple[str, ...]) -> list[tuple[logging.Logger, int, bool]]:
    snap = []
    for name in names:
        logger = logging.getLogger(name)
        snap.append((logger, logger.level, logger.disabled))
    return snap


def _logger_restore(snap: list[tuple[logging.Logger, int, bool]]) -> None:
    for logger, level, disabled in snap:
        logger.setLevel(level)
        logger.disabled = disabled


@pytest.fixture
def scratch_gateway(monkeypatch, tmp_path):
    import uvicorn

    # Quiet uvicorn only. The mesh-gateway logger must stay as the suite
    # found it: test_non_utf8_key_file_warns_and_disables_not_escapes captures
    # a WARNING on that logger, and caplog.at_level() moves the root logger,
    # not this one. A leftover CRITICAL level drops the line before capture.
    quiet = ("uvicorn", "uvicorn.error", "uvicorn.access")
    log_snap = _logger_snapshot(("mesh-gateway",) + quiet)
    for name in quiet:
        logging.getLogger(name).setLevel(logging.CRITICAL)

    uv = None
    thread = None
    sock = None
    try:
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
            raise RuntimeError(
                f"scratch gateway did not accept connections ({last_error or 'timeout'})"
            )
        if boom or last_error:
            kind = type(boom[0]).__name__ if boom else last_error
            raise RuntimeError(f"scratch gateway failed to start ({kind})")

        # Never inherit a live gateway URL. The CLI is pointed at this socket.
        monkeypatch.setenv("MESH_GATEWAY_URL", base)
        yield {"base": base, "posts": posts}
    finally:
        if uv is not None:
            uv.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        _logger_restore(log_snap)


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


def test_mint_opens_exclusive_file_before_register():
    from swarph_cli.commands.mesh import _run_mint

    src = inspect.getsource(_run_mint)
    for banned in ("SWARPH_SELF", "LOGNAME", "getpass", "write_secret_file(", "O_TRUNC"):
        assert banned not in src
    assert '"defer_token_mint": False' in src
    assert "os.unlink" in src

    preamble, sep, rest = src.partition("os.open")
    assert sep == "os.open"
    assert 'os.name == "nt"' in preamble
    assert 'sys.platform == "win32"' in preamble
    assert "_post_json(" not in preamble
    assert "os.O_CREAT | os.O_EXCL | os.O_WRONLY" in rest
    fchmod_at = rest.index("os.fchmod")
    post_at = rest.index("_post_json(")
    write_at = rest.index("fp.write")
    assert fchmod_at < post_at < write_at
    assert rest.index("os.fsync") > write_at


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

    real_post = mesh._post_json
    watch: dict = {"path": None, "shots": []}

    def watching_post(url, body, token, *, timeout=10.0, encoded=None):
        path = watch["path"]
        if path is not None:
            st = path.stat()
            watch["shots"].append(
                (st.st_size, stat.S_IMODE(st.st_mode), path.read_bytes())
            )
        return real_post(url, body, token, timeout=timeout, encoded=encoded)

    monkeypatch.setattr(mesh, "_post_json", watching_post)

    old_umask = os.umask(0)
    secrets: list[str] = []
    try:
        capsys.readouterr()

        def invoke(argv: list[str]) -> tuple[int, str, str]:
            rc = mesh.run_mesh(argv)
            captured = capsys.readouterr()
            return rc, captured.out, captured.err

        blocked = tmp_path / "unwritable"
        blocked.mkdir()
        os.chmod(blocked, 0o500)
        blocked_out = blocked / "cell.peer_token"
        try:
            rc, out, err = invoke([
                "mint", name, "--out", str(blocked_out), "--gateway", base,
            ])
            assert rc != 0
            assert posts == []
            assert not blocked_out.exists()
            assert list(blocked.iterdir()) == []
        finally:
            os.chmod(blocked, 0o700)

        rc, out, err = invoke([
            "mint", name, "--out", str(preexisting), "--gateway", base,
        ])
        assert rc != 0
        assert preexisting.read_bytes() == original
        assert posts == []
        assert "already exists" in err
        blob = out + err

        link_target = tmp_path / "symlink-target.token"
        link_target.write_bytes(b"sentinel-symlink\n")
        link = tmp_path / "via-symlink.token"
        link.symlink_to(link_target)
        rc, out, err = invoke([
            "mint", name, "--out", str(link), "--gateway", base,
        ])
        assert rc != 0
        assert posts == []
        assert link_target.read_bytes() == b"sentinel-symlink\n"
        assert link.is_symlink()
        assert "already exists" in err
        blob += out + err

        # A dangling symlink is still an existing path. Refuse before register,
        # leave the link in place, and do not create the missing target.
        dangling_target = tmp_path / "missing-target.token"
        dangling = tmp_path / "dangling.token"
        dangling.symlink_to(dangling_target)
        assert not dangling_target.exists()
        rc, out, err = invoke([
            "mint", name, "--out", str(dangling), "--gateway", base,
        ])
        assert rc != 0
        assert posts == []
        assert dangling.is_symlink()
        assert os.readlink(dangling) == str(dangling_target)
        assert not dangling_target.exists()
        assert not dangling_target.is_symlink()
        assert "already exists" in err
        blob += out + err

        watch["path"] = out_path
        watch["shots"].clear()
        rc, out, err = invoke([
            "mint", name, "--out", str(out_path), "--gateway", base,
        ])
        text = out + err
        if rc != 0:
            raise AssertionError("mint failed")
        assert len(posts) == 1
        assert watch["shots"] == [(0, 0o600, b"")]
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

        watch["path"] = second
        watch["shots"].clear()
        rc, out, err = invoke([
            "mint", name, "--out", str(second), "--gateway", base,
        ])
        text = out + err
        assert rc != 0
        assert watch["shots"] == [(0, 0o600, b"")]
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

        watch["path"] = other
        watch["shots"].clear()
        rc, out, err = invoke([
            "mint", "other-cell", "--out", str(other), "--gateway", base,
            "--token-file", str(out_path),
        ])
        text = out + err
        assert rc != 0
        assert watch["shots"] == [(0, 0o600, b"")]
        assert not other.exists()
        assert "this credential is a per-peer token" in text
        assert "SSO identity" not in text
        assert "operator credential is required" in text
        assert "--token-file <operator-token>" in text
        assert "not a cell's own peer token" in text
        _assert_silent(text, secret)
        blob += text

        # Deregister writes a revocation row and purges the live token. The
        # next register mints one past that high-water mark, so this fresh
        # mesh's second mint is generation 2 — not a constant printed as 1.
        op = os.environ["MESH_GATEWAY_TOKEN"]
        delete = urllib.request.Request(
            f"{base}/peers/{name}",
            method="DELETE",
            headers={"Authorization": f"Bearer {op}"},
        )
        with urllib.request.urlopen(delete, timeout=5) as resp:
            assert resp.status == 200
        gen2 = tmp_path / "gen2.token"
        watch["path"] = gen2
        watch["shots"].clear()
        rc, out, err = invoke([
            "mint", name, "--out", str(gen2), "--gateway", base,
        ])
        text = out + err
        if rc != 0:
            raise AssertionError(f"generation-2 mint failed\n{text}")
        assert any(line.endswith("key_generation=2") for line in out.splitlines())
        raw2 = gen2.read_text(encoding="utf-8")
        if not raw2.endswith("\n") or "\n" in raw2[:-1]:
            raise AssertionError("token file must be the token plus one trailing newline")
        secret2 = raw2[:-1]
        assert secret2 != secret
        who2 = _whoami(base, secret2)
        assert who2["peer"] == name
        assert type(who2["key_generation"]) is int
        assert who2["key_generation"] == 2
        assert stat.S_IMODE(gen2.stat().st_mode) == 0o600
        if out_path.read_bytes() != before:
            raise AssertionError("first token file changed")
        _assert_silent(text, secret)
        _assert_silent(text, secret2)
        blob += text

        for post in posts:
            assert post.get("defer_token_mint") is False
        _assert_silent(blob, secret)
    finally:
        os.umask(old_umask)
        for path, existed in suspect_existed.items():
            if path.exists() != existed:
                raise AssertionError(f"mint wrote outside --out: {path}")


def _operator_token(directory: Path, name: str = "operator.token") -> Path:
    path = directory / name
    path.write_text("operator-credential\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_sso_caller_binding_names_sso_and_unlinks(monkeypatch, tmp_path, capsys):
    """403 detail matches the live gateway's SSO refusal, not a per-peer token.

    mesh-gateway raises "caller-binding: SSO identity {node!r} may not act as
    {field}={actor!r}". The file is created empty at mode 0600 before that
    POST and removed when the mint does not happen.
    """
    from swarph_cli.commands import mesh

    out_path = tmp_path / "sso-out" / "cell.peer_token"
    token_file = _operator_token(tmp_path)
    monkeypatch.setenv("SWARPH_SELF", "decoy-self")
    seen: dict = {}

    def stub(url, body, token, *, timeout=10.0, encoded=None):
        st = out_path.stat()
        seen["exists"] = out_path.is_file()
        seen["size"] = st.st_size
        seen["mode"] = stat.S_IMODE(st.st_mode)
        seen["data"] = out_path.read_bytes()
        seen["name"] = body.get("name")
        seen["defer"] = body.get("defer_token_mint")
        assert str(url).endswith("/peers/register")
        return 403, {
            "detail": (
                "caller-binding: SSO identity 'alex' may not act as "
                "register_mint_target='other-cell'"
            ),
        }

    monkeypatch.setattr(mesh, "_post_json", stub)
    rc = mesh.run_mesh([
        "mint", "other-cell",
        "--out", str(out_path),
        "--gateway", "http://127.0.0.1:9",
        "--token-file", str(token_file),
    ])
    captured = capsys.readouterr()
    err = captured.err
    assert rc != 0
    assert seen["exists"] is True
    assert seen["size"] == 0
    assert seen["data"] == b""
    assert seen["mode"] == 0o600
    assert seen["name"] == "other-cell"
    assert seen["defer"] is False
    assert "this credential is an SSO identity" in err
    assert "operator credential is required" in err
    assert "--token-file <operator-token>" in err
    assert "per-peer token" not in err
    assert "operator-credential" not in captured.out + err
    assert not out_path.exists()
    assert stat.S_IMODE(out_path.parent.stat().st_mode) == 0o700


def test_cell_join_binding_is_not_called_a_per_peer_token(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import mesh

    out_path = tmp_path / "join.token"
    token_file = _operator_token(tmp_path)
    seen: dict = {}

    def stub(url, body, token, *, timeout=10.0, encoded=None):
        st = out_path.stat()
        seen["size"] = st.st_size
        seen["mode"] = stat.S_IMODE(st.st_mode)
        seen["data"] = out_path.read_bytes()
        return 403, {
            "detail": (
                "caller-binding: a cell-join credential carries no peer identity "
                "and may not act as register_mint_target='other-cell'"
            ),
        }

    monkeypatch.setattr(mesh, "_post_json", stub)
    rc = mesh.run_mesh([
        "mint", "other-cell",
        "--out", str(out_path),
        "--gateway", "http://127.0.0.1:9",
        "--token-file", str(token_file),
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert seen["size"] == 0 and seen["data"] == b"" and seen["mode"] == 0o600
    assert "cell-join credential" in err
    assert "per-peer token" not in err
    assert "operator credential is required" in err
    assert "--token-file <operator-token>" in err
    assert not out_path.exists()


def test_whoami_failure_keeps_the_minted_file(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import mesh

    out_path = tmp_path / "kept.token"
    token_file = _operator_token(tmp_path)
    secret = "minted-peer-secret"
    seen: dict = {}

    def stub_post(url, body, token, *, timeout=10.0, encoded=None):
        st = out_path.stat()
        seen["size"] = st.st_size
        seen["mode"] = stat.S_IMODE(st.st_mode)
        return 200, {
            "peer_token": secret,
            "token_status": "minted",
            "name": "kept-cell",
        }

    def stub_get(url, token, *, timeout=10.0):
        raise OSError("whoami unavailable")

    monkeypatch.setattr(mesh, "_post_json", stub_post)
    monkeypatch.setattr(mesh, "_http_get_json", stub_get)
    rc = mesh.run_mesh([
        "mint", "kept-cell",
        "--out", str(out_path),
        "--gateway", "http://127.0.0.1:9",
        "--token-file", str(token_file),
    ])
    captured = capsys.readouterr()
    blob = captured.out + captured.err
    assert rc != 0
    assert seen["size"] == 0 and seen["mode"] == 0o600
    assert out_path.read_text(encoding="utf-8") == secret + "\n"
    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    assert secret not in blob
    assert "operator-credential" not in blob
    assert "key generation could not be read" in captured.err


def test_gateway_not_configured_unlinks_empty_file(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import mesh

    out_path = tmp_path / "nested" / "cell.token"
    token_file = _operator_token(tmp_path)
    seen: dict = {}
    real_post = mesh._post_json

    def wrapping(url, body, token, *, timeout=10.0, encoded=None):
        st = out_path.stat()
        seen["size"] = st.st_size
        seen["mode"] = stat.S_IMODE(st.st_mode)
        seen["data"] = out_path.read_bytes()
        return real_post(url, body, token, timeout=timeout, encoded=encoded)

    monkeypatch.setattr(mesh, "_post_json", wrapping)
    rc = mesh.run_mesh([
        "mint", "cell",
        "--out", str(out_path),
        "--gateway", "not-a-url",
        "--token-file", str(token_file),
    ])
    err = capsys.readouterr().err
    assert rc == 2
    assert seen["size"] == 0 and seen["data"] == b"" and seen["mode"] == 0o600
    assert "MESH_GATEWAY_URL" in err
    assert "operator-credential" not in err
    assert not out_path.exists()
    assert stat.S_IMODE(out_path.parent.stat().st_mode) == 0o700


def test_failed_register_unlinks_empty_file(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import mesh

    token_file = _operator_token(tmp_path)

    def _run(out_name: str, poster) -> tuple[int, str, str, dict]:
        out_path = tmp_path / out_name
        seen: dict = {}

        def wrapping(url, body, token, *, timeout=10.0, encoded=None):
            st = out_path.stat()
            seen["size"] = st.st_size
            seen["mode"] = stat.S_IMODE(st.st_mode)
            seen["data"] = out_path.read_bytes()
            return poster(url, body, token)

        monkeypatch.setattr(mesh, "_post_json", wrapping)
        rc = mesh.run_mesh([
            "mint", "cell",
            "--out", str(out_path),
            "--gateway", "http://127.0.0.1:9",
            "--token-file", str(token_file),
        ])
        captured = capsys.readouterr()
        assert not out_path.exists()
        assert seen["size"] == 0 and seen["data"] == b"" and seen["mode"] == 0o600
        assert "operator-credential" not in captured.out + captured.err
        return rc, captured.out, captured.err, seen

    def explode(url, body, token):
        raise RuntimeError("boom")

    rc, _out, err, _seen = _run("explode.token", explode)
    assert rc != 0
    assert "gateway register request failed" in err

    def http_500(url, body, token):
        return 500, {"detail": "leaked peer_token=abcsecret"}

    rc, _out, err, _seen = _run("http.token", http_500)
    assert rc != 0
    assert "gateway 500" in err
    assert "abcsecret" not in err
    assert "gateway error" in err

    def bearer_detail(url, body, token):
        return 502, {"detail": "upstream said Bearer not-a-credential"}

    rc, out, err, _seen = _run("bearer.token", bearer_detail)
    assert rc != 0
    assert "not-a-credential" not in out
    assert "not-a-credential" not in err
    assert "gateway error" in err

    def no_token(url, body, token):
        return 200, {"token_status": "minted", "peer_token": None, "name": "cell"}

    rc, _out, err, _seen = _run("empty.token", no_token)
    assert rc != 0
    assert "minted no token" in err

    def existing_with_secret(url, body, token):
        return 200, {
            "token_status": "existing",
            "peer_token": "should-not-leak",
            "name": "cell",
        }

    rc, _out, err, _seen = _run("existing.token", existing_with_secret)
    assert rc != 0
    assert "should-not-leak" not in err
    assert "DELETE /peers/cell" in err


def test_network_error_unlinks_empty_file(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import mesh

    out_path = tmp_path / "net.token"
    token_file = _operator_token(tmp_path)
    seen: dict = {}
    real_post = mesh._post_json

    def wrapping(url, body, token, *, timeout=10.0, encoded=None):
        st = out_path.stat()
        seen["size"] = st.st_size
        seen["mode"] = stat.S_IMODE(st.st_mode)
        seen["data"] = out_path.read_bytes()
        return real_post(url, body, token, timeout=1.0, encoded=encoded)

    monkeypatch.setattr(mesh, "_post_json", wrapping)
    rc = mesh.run_mesh([
        "mint", "cell",
        "--out", str(out_path),
        "--gateway", "http://127.0.0.1:1",
        "--token-file", str(token_file),
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert seen["size"] == 0 and seen["data"] == b"" and seen["mode"] == 0o600
    assert not out_path.exists()
    assert "operator-credential" not in err


def test_missing_credential_creates_no_file_and_does_not_post(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import mesh

    posted: list = []

    def boom(*args, **kwargs):
        posted.append(args)
        raise AssertionError("register POST must not run without a credential")

    monkeypatch.setattr(mesh, "_post_json", boom)
    out_path = tmp_path / "no-cred.token"
    rc = mesh.run_mesh([
        "mint", "cell",
        "--out", str(out_path),
        "--gateway", "http://127.0.0.1:9",
    ])
    err = capsys.readouterr().err
    assert rc != 0
    assert posted == []
    assert not out_path.exists()
    assert "no operator credential" in err
