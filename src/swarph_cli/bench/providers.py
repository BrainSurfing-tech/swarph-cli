"""Provider registry for bench arms. TOML holds env-var names, never secrets."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from urllib.parse import urlparse

_FIELDS = {"kind", "base_url", "path", "auth", "usage", "price", "egress"}
_AUTH = {"env", "header", "scheme"}
_USAGE = {"in", "out"}
_PRICE = {"in_per_mtok", "out_per_mtok", "source"}


class RegistryError(ValueError):
    pass


def registry_path(explicit: str | None = None) -> Path | None:
    """``--providers``, then ``$SWARPH_BENCH_PROVIDERS``, then the user config file."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("SWARPH_BENCH_PROVIDERS")
    if env:
        return Path(env)
    home = Path.home() / ".config" / "swarph" / "bench_providers.toml"
    if home.is_file():
        return home
    return None


def _loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in {"localhost", "::1"}:
        return True
    if host.startswith("127."):
        return True
    return False


def egress_of_url(url: str) -> str:
    return "local" if _loopback(url) else "external"


def _check(name: str, entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise RegistryError(f"{name}: entry must be a table")
    unknown = set(entry) - _FIELDS
    if unknown:
        raise RegistryError(f"{name}: unknown keys {sorted(unknown)}")
    kind = entry.get("kind")
    if kind not in {"typed", "semantic"}:
        raise RegistryError(f"{name}: kind must be typed or semantic")
    base = entry.get("base_url")
    if not isinstance(base, str) or not base.strip():
        raise RegistryError(f"{name}: base_url is required")
    path = entry.get("path")
    if path is None:
        path = "/v1/systemone" if kind == "typed" else "/v1/chat/completions"
    if not isinstance(path, str) or not path.startswith("/"):
        raise RegistryError(f"{name}: path must start with /")
    egress = entry.get("egress", "external")
    if egress not in {"external", "local"}:
        raise RegistryError(f"{name}: egress must be external or local")
    if egress == "local" and not _loopback(base):
        raise RegistryError(
            f"{name}: egress=local is only valid for a loopback base_url")
    auth = entry.get("auth")
    if auth is not None:
        if not isinstance(auth, dict) or set(auth) - _AUTH:
            raise RegistryError(f"{name}: auth has unknown or missing fields")
        if not auth.get("env") or not auth.get("header"):
            raise RegistryError(f"{name}: auth.env and auth.header are required")
    usage = entry.get("usage")
    if usage is not None and (not isinstance(usage, dict) or set(usage) - _USAGE):
        raise RegistryError(f"{name}: usage has unknown keys")
    price = entry.get("price")
    if price is not None:
        if not isinstance(price, dict) or set(price) - _PRICE:
            raise RegistryError(f"{name}: price has unknown keys")
        if "in_per_mtok" not in price or "out_per_mtok" not in price:
            raise RegistryError(f"{name}: price needs in_per_mtok and out_per_mtok")
    out = dict(entry)
    out["path"] = path
    out["egress"] = egress
    return out


def load_registry(path: str | Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    raw = Path(path).read_text(encoding="utf-8")
    data = tomllib.loads(raw)
    return {name: _check(name, entry) for name, entry in data.items()}
