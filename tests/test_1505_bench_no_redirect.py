"""Card #1505: authed bench HTTP must not follow redirects with the Bearer header."""
from __future__ import annotations

import copy
import http.client
import io
import json
import urllib.request

import pytest

from swarph_cli.bench import backends

TOKEN = "jev-test-token-not-a-real-key"


def test_no_redirect_handler_refuses():
    assert backends._NoRedirect().redirect_request(None, None, 302, "", {}, "http://x") is None


def test_http_backend_single_request_on_fake_302(monkeypatch):
    monkeypatch.setenv("JEV_API_KEY", TOKEN)
    seen = []

    class _Fake302(urllib.request.BaseHandler):
        handler_order = 100

        def _open(self, req):
            seen.append((req.full_url, req.get_header("Authorization")))
            r = io.BytesIO(b"")
            r.code, r.msg = 302, "Found"
            r.headers = http.client.HTTPMessage()
            r.headers["Location"] = "http://127.0.0.1:9/steal"
            r.info = lambda: r.headers
            r.geturl = lambda: req.full_url
            return r

        https_open = http_open = _open

    opener = copy.deepcopy(backends._AUTH_HTTP_OPENER)
    opener.add_handler(_Fake302())
    monkeypatch.setattr(backends, "_AUTH_HTTP_OPENER", opener)

    arm = backends.HttpBackend(
        name="jev", kind="typed", base_url="https://api.typesafe.ai",
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
    assert not any(u == "http://127.0.0.1:9/steal" for u, _ in seen)
