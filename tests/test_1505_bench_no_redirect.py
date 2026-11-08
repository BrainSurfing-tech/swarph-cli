"""Card #1505: authed bench HTTP must not follow redirects with the Bearer header."""
from __future__ import annotations

import http.client
import io
import json
import urllib.request

from swarph_cli.bench import backends

TOKEN = "jev-test-token-not-a-real-key"
LOOPBACK = "http://127.0.0.1:9"


class _Fake302(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, seen):
        self._seen = seen

    def _open(self, req):
        self._seen.append((req.full_url, req.get_header("Authorization")))
        r = io.BytesIO(b"")
        r.code, r.msg = 302, "Found"
        r.headers = http.client.HTTPMessage()
        r.headers["Location"] = f"{LOOPBACK}/steal"
        r.info = lambda: r.headers
        r.geturl = lambda: req.full_url
        return r

    https_open = http_open = _open


def _test_opener(seen):
    redirect = [
        type(h)()
        for h in backends._AUTH_HTTP_OPENER.handlers
        if isinstance(h, urllib.request.HTTPRedirectHandler)
    ]
    return urllib.request.build_opener(*redirect, _Fake302(seen))


def test_no_redirect_handler_refuses():
    assert backends._NoRedirect().redirect_request(None, None, 302, "", {}, "http://x") is None


def test_http_backend_single_request_on_fake_302(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    seen = []
    monkeypatch.setattr(backends, "_AUTH_HTTP_OPENER", _test_opener(seen))

    arm = backends.HttpBackend(
        name="jev", kind="typed", base_url=LOOPBACK,
        path="/v1/systemone",
        auth={"env": "JEV_API_KEY", "header": "Authorization", "scheme": "Bearer"},
        price={"in_per_mtok": 0.042, "out_per_mtok": 0, "source": "example"},
    )
    body = json.dumps({"state": {}, "questions": {"answer": {"type": "choice"}}})
    result = arm.generate("jev", body)
    assert len(seen) == 1
    assert seen[0][1] == f"Bearer {TOKEN}"
    assert result.error and "302" in result.error
    assert TOKEN not in (result.error or "")
    assert not any("steal" in u for u, _ in seen)


def test_urlopen_revert_still_single_request_offline(monkeypatch):
    """Validate #4605 guard: a urlopen revert stays offline and still fails."""
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    seen = []
    stub = _test_opener(seen)
    monkeypatch.setattr(urllib.request, "urlopen", stub.open)

    class _UsesGlobalUrlopen:
        def open(self, req, timeout=None):
            return urllib.request.urlopen(req, timeout=timeout)

    monkeypatch.setattr(backends, "_AUTH_HTTP_OPENER", _UsesGlobalUrlopen())

    arm = backends.HttpBackend(
        name="jev", kind="typed", base_url=LOOPBACK,
        path="/v1/systemone",
        auth={"env": "JEV_API_KEY", "header": "Authorization", "scheme": "Bearer"},
        price={"in_per_mtok": 0.042, "out_per_mtok": 0, "source": "example"},
    )
    result = arm.generate("jev", json.dumps({"state": {}, "questions": {"answer": {"type": "choice"}}}))
    assert len(seen) == 1
    assert not any("steal" in u for u, _ in seen)
    assert result.error and "302" in result.error
