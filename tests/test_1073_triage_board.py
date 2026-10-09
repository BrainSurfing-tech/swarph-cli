"""card #1073 rework (#1446): the router's first live run died on the
board it never wrote to in tests.

0.72.14 POSTed a card create with title+body only; the gateway refused
it (BoardCardCreate requires project_id); the old code carried the
refusal forward as a row with id None and died on the bare 'accept
line not added' assertion. These tests pin the fix: the create sends
project_id + assignee, every non-2xx names its status and detail, the
run exits non-zero saying so, and the double rejects what the gateway
rejects (a create without project_id, a post to a missing thread).
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import triage as _triage_cmd
from swarph_cli.triage import backend as _backend
from swarph_cli.triage import router as _router

NOW = 1_790_000_000.0


def _iso(age_s):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW - age_s, timezone.utc).isoformat()


class _FakeHttp:
    """Records bodies; scripted (status, body) per URL suffix."""

    def __init__(self):
        self.posts = []
        self.scripts = {}

    def on_post(self, suffix, status, body):
        self.scripts[("POST", suffix)] = (status, body)

    def on_get(self, suffix, status, body):
        self.scripts[("GET", suffix)] = (status, body)

    def post(self, url, payload, token):
        self.posts.append((url, dict(payload)))
        for (method, suffix), response in self.scripts.items():
            if method == "POST" and url.endswith(suffix):
                return response
        raise AssertionError(f"unscripted POST {url}")

    def get(self, url, token):
        for (method, suffix), response in self.scripts.items():
            if method == "GET" and url.endswith(suffix):
                return response
        raise AssertionError(f"unscripted GET {url}")


def _board(project_id=9):
    http = _FakeHttp()
    return _backend.BoardBackend("http://gw:9", "tok", "lab-ovh",
                                 http.get, http.post,
                                 project_id=project_id), http


def test_create_sends_project_id_and_assignee():
    backend, http = _board(project_id=9)
    http.on_post("/board/cards", 200, {"id": 77})
    row = backend.create_row("lab-ovh triage", "lab-ovh")
    assert row["id"] == 77
    _url, payload = http.posts[-1]
    assert payload["project_id"] == 9
    assert payload["assignee"] == "lab-ovh"
    assert payload["title"] == "lab-ovh triage"


def test_create_422_names_status_and_detail():
    backend, http = _board()
    http.on_post("/board/cards", 422, {"detail": "project_id: field required"})
    with pytest.raises(_backend.BackendError) as exc:
        backend.create_row("lab-ovh triage", "lab-ovh")
    assert "422" in str(exc.value)
    assert "project_id: field required" in str(exc.value)


def test_append_422_names_status_and_detail():
    backend, http = _board()
    http.on_get("/board/cards/77", 200, {"id": 77,
                                         "thread_uuid": "thread-1"})
    http.on_post("/messages", 422, {"detail": "unknown thread"})
    row = {"id": 77, "title": "lab-ovh triage", "owner": "lab-ovh",
           "status": "open"}
    with pytest.raises(_backend.BackendError) as exc:
        backend.append(row, "ACCEPT x y")
    assert "422" in str(exc.value) and "unknown thread" in str(exc.value)


def test_failed_create_is_never_a_row_with_id_none():
    backend, http = _board()
    http.on_post("/board/cards", 500, {"detail": "boom"})
    with pytest.raises(_backend.BackendError):
        backend.create_row("lab-ovh triage", "lab-ovh")


def test_memory_double_rejects_create_without_project_id():
    backend = _backend.MemoryBackend(project_id=None)
    with pytest.raises(_backend.BackendError) as exc:
        backend.create_row("lab-ovh triage", "lab-ovh")
    assert "project_id" in str(exc.value)


def test_memory_double_rejects_post_to_missing_thread():
    backend = _backend.MemoryBackend()
    with pytest.raises(_backend.BackendError) as exc:
        backend.append({"id": 999, "title": "ghost triage",
                        "owner": "lab-ovh", "status": "open"}, "ACCEPT x y")
    assert "no such thread" in str(exc.value)


def _board_fixture():
    old = _iso(10 * 86400)
    waiter = {"id": 1, "status": "open", "taken_at": old, "step": "build",
              "card_id": 7, "holder": "waiter"}
    dep = {"id": 2, "status": "open", "holder": "lab-ovh",
           "holder_known": True, "created_at": old, "card_id": 8}
    return [waiter], {1: waiter, 2: dep}, {"lab-ovh"}


def _live_http():
    http = _FakeHttp()
    http.on_get("/board/cards/7/graph", 200, {"steps": [
        {"step": "build", "needs": [
            {"step": "dep", "row_id": 2, "satisfied": False}]}]})
    http.on_get("/board/cards/8", 200, {"id": 8, "due_at": None})
    return http


def test_run_names_the_refusal_and_exits_nonzero(capsys, tmp_path):
    """The 0.72.14 shape end to end: the create is refused, the run
    says so (status + detail) and exits 1 — no bare assertion."""
    backend, http = _board()
    http.on_post("/board/cards", 422,
                 {"detail": "project_id: field required"})
    live = _live_http()
    rows, by_id, peers = _board_fixture()
    rc, summary = _triage_cmd.run_triage(
        gateway="http://gw:9", token="tok", sender="lab-ovh",
        owners_map=_router.load_owners(_triage_cmd._OWNERS_DEFAULT),
        state_file=str(tmp_path / "state.json"), store_backend=backend,
        peers_fetcher=None, board_fetcher=lambda: (rows, by_id, peers),
        http_get=live.get, http_post=http.post, now=NOW)
    err = capsys.readouterr().err
    assert rc == 1
    assert "422" in err and "project_id: field required" in err
    assert "accept line not added" not in err
    assert "board_error" in summary
