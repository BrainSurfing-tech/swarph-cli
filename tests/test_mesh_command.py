"""Tests for ``swarph mesh`` direct DM and registration commands."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

from swarph_cli.commands import mesh


def test_mesh_send_posts_message_shape(monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "gpt-ops")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    captured = {}

    def fake_post(url, body, token, *, timeout=10.0):
        captured["url"] = url
        captured["body"] = body
        captured["token"] = token
        return 200, {
            "id": 123,
            "from_node": body["from_node"],
            "to_node": body["to_node"],
            "kind": body["kind"],
        }

    monkeypatch.setattr(mesh, "_post_json", fake_post)
    rc = mesh.run_mesh(
        [
            "send",
            "lab-ovh",
            "--kind",
            "answer",
            "--content",
            "plan ready",
        ]
    )
    assert rc == 0
    assert captured["url"] == "http://localhost:8788/messages"
    assert captured["token"] == "tok"
    assert captured["body"] == {
        "from_node": "gpt-ops",
        "to_node": "lab-ovh",
        "kind": "answer",
        "content": "plan ready",
    }
    out = capsys.readouterr().out
    assert "sent id=123" in out
    assert "tok" not in out


def test_mesh_send_gateway_error_is_nonsecret(monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "gpt-ops")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "secret-token")
    monkeypatch.setattr(
        mesh,
        "_post_json",
        lambda url, body, token, *, timeout=10.0: (403, {"detail": "bad actor"}),
    )
    rc = mesh.run_mesh(
        ["send", "lab-ovh", "--kind", "answer", "--content", "x"]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "403" in err
    assert "bad actor" in err
    assert "secret-token" not in err


def test_mesh_gateway_error_without_detail_does_not_echo_payload(monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "gpt-ops")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "secret-token")
    monkeypatch.setattr(
        mesh,
        "_post_json",
        lambda url, body, token, *, timeout=10.0: (
            403,
            {"authorization": "secret-token", "body": {"peer_token": "minted-secret"}},
        ),
    )

    rc = mesh.run_mesh(["send", "lab-ovh", "--kind", "answer", "--content", "x"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "<gateway error>" in err
    assert "secret-token" not in err
    assert "minted-secret" not in err


def test_mesh_inbox_uses_to_query_and_unread(monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "gpt-ops")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    captured = {}

    def fake_get(url, token, *, timeout=10.0):
        captured["url"] = url
        captured["token"] = token
        return 200, {
            "messages": [
                {
                    "id": 7,
                    "from_node": "lab-ovh",
                    "kind": "question",
                    "content": "check mesh",
                    "read_at": None,
                }
            ],
            "n": 1,
        }

    monkeypatch.setattr(mesh, "_http_get_json", fake_get)
    rc = mesh.run_mesh(["inbox", "--unread", "--limit", "5"])
    assert rc == 0
    assert "to=gpt-ops" in captured["url"]
    assert "to_node=" not in captured["url"]
    assert "unread_only=true" in captured["url"]
    assert "limit=5" in captured["url"]
    out = capsys.readouterr().out
    assert "id=7 unread from=lab-ovh kind=question" in out


def test_mesh_inbox_json_output(monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "gpt-ops")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(
        mesh,
        "_http_get_json",
        lambda url, token, *, timeout=10.0: (200, {"messages": [], "n": 0}),
    )
    rc = mesh.run_mesh(["inbox", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {"messages": [], "n": 0}


def test_mesh_register_mints_token_file_mode_600(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    captured = {}

    def fake_post(url, body, token, *, timeout=10.0):
        captured["url"] = url
        captured["body"] = body
        captured["token"] = token
        return 200, {
            "status": "registered",
            "name": body["name"],
            "peer_token": "minted-secret-token",
            "token_status": "minted",
        }

    monkeypatch.setattr(mesh, "_post_json", fake_post)
    rc = mesh.run_mesh(
        [
            "register",
            "--as",
            "gpt-ops",
            "--capability",
            "can_claim_tasks=true",
            "--capability",
            "role=codex",
        ]
    )
    assert rc == 0
    assert captured["url"] == "http://localhost:8788/peers/register"
    assert captured["token"] == "shared-auth"
    assert captured["body"] == {
        "name": "gpt-ops",
        "url": "http://gpt-ops:8787",
        "capabilities": {"can_claim_tasks": True, "role": "codex"},
    }
    token_file = tmp_path / ".config" / "swarph" / "gpt-ops.peer_token"
    assert token_file.read_text().strip() == "minted-secret-token"
    if sys.platform != "win32":  # POSIX file-mode bits not representable on Windows
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "registered gpt-ops" in out
    assert str(token_file) in out
    assert "minted-secret-token" not in out


def test_mesh_register_existing_token_hard_stops_without_force(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    token_file = tmp_path / ".config" / "swarph" / "gpt-ops.peer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("existing\n")
    called = False

    def fake_post(url, body, token, *, timeout=10.0):
        nonlocal called
        called = True
        return 200, {}

    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    monkeypatch.setattr(mesh, "_post_json", fake_post)
    rc = mesh.run_mesh(["register", "--as", "gpt-ops"])
    assert rc == 1
    assert called is False
    assert "already exists" in capsys.readouterr().err


def test_mesh_register_force_allows_existing_without_echoing_token(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    token_file = tmp_path / ".config" / "swarph" / "gpt-ops.peer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("existing\n")
    monkeypatch.setattr(
        mesh,
        "_post_json",
        lambda url, body, token, *, timeout=10.0: (
            200,
            {
                "status": "registered",
                "name": "gpt-ops",
                "peer_token": None,
                "token_status": "existing",
            },
        ),
    )
    rc = mesh.run_mesh(["register", "--as", "gpt-ops", "--force"])
    assert rc == 0
    assert token_file.read_text() == "existing\n"
    out = capsys.readouterr().out
    assert "token_status=existing" in out
    assert "existing\n" not in out


def test_mesh_resolves_per_peer_token_file(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    token_file = tmp_path / ".config" / "swarph" / "gpt-ops.peer_token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("peer-token\n")
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    assert mesh._resolve_token("gpt-ops", None) == "peer-token"


def test_main_dispatches_mesh_verb(monkeypatch):
    from swarph_cli import main as main_mod

    captured = {}

    def fake_run(argv):
        captured["argv"] = argv
        return 0

    monkeypatch.setattr("swarph_cli.commands.mesh.run_mesh", fake_run)
    rc = main_mod.main(["mesh", "inbox", "--as", "gpt-ops"])
    assert rc == 0
    assert captured["argv"] == ["inbox", "--as", "gpt-ops"]
