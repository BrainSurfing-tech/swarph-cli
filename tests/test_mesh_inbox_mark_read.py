"""`swarph mesh inbox` must mark what it shows you as read.

INCIDENT 2026-07-10: the gateway has POST /messages/{id}/read but the CLI never
called it, so a peer's request stayed "unread" no matter how many times it was
answered. grok-researcher answered one request 67 times (~410k tokens) because
its loop's exit condition — "is it still unread?" — could never become false.

Reading your inbox consumes it. `--peek` opts out.

Run: venv/bin/python -m pytest tests/test_mesh_inbox_mark_read.py -v
"""
import argparse

from swarph_cli.commands import mesh


def _args(**kw):
    base = dict(self_name="grok-researcher", token_file=None, gateway="http://gw:8788",
                limit=20, unread=False, json=False, peek=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _inbox(messages):
    return (200, {"messages": messages})


def _wire(monkeypatch, messages):
    """Stub the GET; record every POST the command makes."""
    posts = []
    monkeypatch.setenv("SWARPH_SELF", "grok-researcher")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(mesh, "_http_get_json", lambda url, tok, **k: _inbox(messages))
    def fake_post(url, body, token, **k):
        posts.append((url, body))
        return (200, {"ok": True})
    monkeypatch.setattr(mesh, "_post_json", fake_post)
    return posts


UNREAD = {"id": 4444, "from_node": "gridiron", "kind": "question",
          "content": "buzz please", "read_at": None}
ALSO_UNREAD = {"id": 4474, "from_node": "gridiron", "kind": "question",
               "content": "you are looping", "read_at": None}
ALREADY_READ = {"id": 4000, "from_node": "lab-ovh", "kind": "fyi",
                "content": "old news", "read_at": "2026-07-01T00:00:00Z"}


def test_inbox_marks_displayed_unread_messages_read(monkeypatch, capsys):
    posts = _wire(monkeypatch, [UNREAD, ALSO_UNREAD])
    assert mesh._run_inbox(_args()) == 0
    assert posts == [("http://gw:8788/messages/read", {"ids": [4444, 4474]})], \
        "unread DMs are one bulk mark-read, not one POST per id"


def test_inbox_does_not_remark_already_read(monkeypatch, capsys):
    posts = _wire(monkeypatch, [ALREADY_READ, UNREAD])
    mesh._run_inbox(_args())
    assert posts == [("http://gw:8788/messages/read", {"ids": [4444]})], \
        "a message already read must not be in the bulk body"


def test_peek_marks_nothing(monkeypatch, capsys):
    posts = _wire(monkeypatch, [UNREAD, ALSO_UNREAD])
    assert mesh._run_inbox(_args(peek=True)) == 0
    assert posts == [], "--peek must not consume the inbox"
    assert "unread" in capsys.readouterr().out, "--peek still shows the messages"


def test_json_output_also_marks_read(monkeypatch, capsys):
    """A script consuming --json has consumed the messages just as surely."""
    posts = _wire(monkeypatch, [UNREAD])
    assert mesh._run_inbox(_args(json=True)) == 0
    assert posts == [("http://gw:8788/messages/read", {"ids": [4444]})]


def test_mark_read_failure_never_breaks_the_listing(monkeypatch, capsys):
    """Telemetry-ish side effect: a failing mark-read must not fail the command."""
    monkeypatch.setenv("SWARPH_SELF", "grok-researcher")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(mesh, "_http_get_json", lambda url, tok, **k: _inbox([UNREAD]))
    def boom(url, body, token, **k):
        raise OSError("gateway unreachable")
    monkeypatch.setattr(mesh, "_post_json", boom)
    assert mesh._run_inbox(_args()) == 0, "listing still succeeds"
    out, err = capsys.readouterr()
    assert "id=4444" in out, "the message was still shown"
    assert "mark-read" in err.lower(), "the failure is surfaced, not swallowed"


def test_752_ids_are_two_bulk_posts_and_zero_per_id(monkeypatch):
    posts = []

    def fake(url, body, token, **k):
        posts.append((url, body))
        return 200, {}

    monkeypatch.setattr(mesh, "_post_json", fake)
    failed = mesh._mark_read("http://gw:8788", "tok", [{"id": i} for i in range(752)])
    assert failed == []
    assert [url for url, _ in posts] == [
        "http://gw:8788/messages/read",
        "http://gw:8788/messages/read",
    ]
    assert len(posts[0][1]["ids"]) == 500
    assert len(posts[1][1]["ids"]) == 252
    assert all("/messages/" + str(i) + "/read" not in url for url, _ in posts for i in range(752))


def test_a_404_on_the_bulk_route_falls_back_to_per_id(monkeypatch):
    posts = []

    def fake(url, body, token, **k):
        posts.append(url)
        if url.endswith("/messages/read"):
            return 404, {"detail": "Not Found"}
        return 200, {}

    monkeypatch.setattr(mesh, "_post_json", fake)
    assert mesh._mark_read("http://gw:8788", "tok", [{"id": 1}, {"id": 2}]) == []
    assert posts == [
        "http://gw:8788/messages/read",
        "http://gw:8788/messages/1/read",
        "http://gw:8788/messages/2/read",
    ]


def test_the_per_id_url_is_only_the_fallback():
    from pathlib import Path
    root = Path(mesh.__file__).resolve().parents[2]
    hits = []
    for path in root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "/messages/{msg_id}/read" in line or "/messages/{int(message_id)}/read" in line:
                if "@app." in line:
                    continue
                hits.append((str(path), lineno, line))
    assert len(hits) == 1, hits
    path, lineno, line = hits[0]
    assert path == str(Path(mesh.__file__).resolve())
    window = Path(path).read_text(encoding="utf-8").splitlines()[max(0, lineno - 12):lineno]
    assert any("404" in earlier for earlier in window), window


def test_empty_inbox_posts_nothing(monkeypatch, capsys):
    posts = _wire(monkeypatch, [])
    assert mesh._run_inbox(_args()) == 0
    assert posts == []


def test_gateway_error_marks_nothing(monkeypatch, capsys):
    posts = []
    monkeypatch.setenv("SWARPH_SELF", "grok-researcher")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(mesh, "_http_get_json", lambda url, tok, **k: (500, {"detail": "boom"}))
    monkeypatch.setattr(mesh, "_post_json", lambda *a, **k: posts.append(a) or (200, {}))
    assert mesh._run_inbox(_args()) == 1
    assert posts == [], "a failed listing consumes nothing"
