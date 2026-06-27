"""Shared mesh-gateway HTTP-client helpers for the control-plane client verbs
(``channel`` / ``schedule`` / ``lane``).

Thin re-export of the ``mesh`` verb's proven auth + HTTP layer (so all gateway
clients resolve identity/token and shape requests identically), plus a DELETE
helper that ``mesh`` didn't need. Importing the underlying ``mesh`` helpers keeps
one battle-tested implementation rather than four divergent copies.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .mesh import _add_common as add_common_args  # noqa: F401
from .mesh import _http_get_json as get_json  # noqa: F401
from .mesh import _post_json as post_json  # noqa: F401
from .mesh import _resolve_self_name as resolve_self_name  # noqa: F401
from .mesh import _resolve_token as resolve_token  # noqa: F401


def delete_json(url: str, token: str, *, timeout: float = 10.0):
    """DELETE ``url`` with a bearer token, returning ``(status, body)`` like the
    mesh GET/POST helpers (status 0 on a connection-level URLError)."""
    req = urllib.request.Request(
        url, method="DELETE", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            err_body = {"detail": str(exc)}
        return exc.code, err_body
    except urllib.error.URLError as exc:
        return 0, {"detail": str(exc)}
