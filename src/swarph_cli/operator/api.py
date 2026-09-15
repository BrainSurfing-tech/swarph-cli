"""Stdlib-only HTTP client for the operator surface."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


def request_json(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any] | list[Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"detail": raw or str(exc)}
        if isinstance(parsed, dict):
            parsed.setdefault("_http_status", exc.code)
            return parsed
        return {"detail": parsed, "_http_status": exc.code}
    except urllib.error.URLError as exc:
        return {"detail": str(exc.reason if hasattr(exc, "reason") else exc),
                "_http_status": 0}
