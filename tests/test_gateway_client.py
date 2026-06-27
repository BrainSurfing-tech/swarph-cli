"""Tests for the shared gateway-client foundation."""

from __future__ import annotations

import io
import urllib.error

from swarph_cli.commands import _gateway_client as gc


def test_reexports_present():
    # the mesh auth/HTTP layer is re-exported for the control-plane verbs
    for name in ("resolve_self_name", "resolve_token", "post_json",
                 "get_json", "delete_json", "add_common_args"):
        assert hasattr(gc, name)


def test_delete_json_success(monkeypatch):
    class _Resp:
        status = 200

        def read(self):
            return b'{"deleted": "x"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gc.urllib.request, "urlopen", lambda *a, **k: _Resp())
    status, body = gc.delete_json("http://gw/lanes/x", "tok")
    assert status == 200 and body == {"deleted": "x"}


def test_delete_json_http_error_returns_code_and_detail(monkeypatch):
    def _raise(*a, **k):
        raise urllib.error.HTTPError("http://gw/x", 404, "nope", {},
                                     io.BytesIO(b'{"detail": "not found"}'))

    monkeypatch.setattr(gc.urllib.request, "urlopen", _raise)
    status, body = gc.delete_json("http://gw/x", "tok")
    assert status == 404 and body["detail"] == "not found"


def test_delete_json_urlerror_returns_zero(monkeypatch):
    def _raise(*a, **k):
        raise urllib.error.URLError("conn refused")

    monkeypatch.setattr(gc.urllib.request, "urlopen", _raise)
    status, _ = gc.delete_json("http://gw/x", "tok")
    assert status == 0
