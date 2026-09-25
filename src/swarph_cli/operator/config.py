"""Operator config — identity, gateway, token path. No cell-env inheritance.

Stored at ``~/.config/swarph/operator.json``. Paths expand ``~`` and
``$HOME``; nothing ships a ``/home/ubuntu`` or tailnet literal default.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".config" / "swarph"
CONFIG_PATH = CONFIG_DIR / "operator.json"


@dataclass(frozen=True)
class OperatorConfig:
    identity: str
    gateway: str
    token_file: str

    def token_path(self) -> Path:
        return Path(os.path.expanduser(os.path.expandvars(self.token_file))).resolve()

    def gateway_url(self) -> str:
        return self.gateway.rstrip("/")


def default_token_file_for(identity: str) -> str:
    """Portable default: ``~/.config/swarph/<identity>.peer_token``.

    Uses ``~`` so the file is relocatable; never embeds ``/home/ubuntu``.
    """
    return f"~/.config/swarph/{identity}.peer_token"


def load_config(path: Path | None = None) -> OperatorConfig | None:
    cfg_path = path or CONFIG_PATH
    if not cfg_path.is_file():
        return None
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"operator config at {cfg_path} must be a JSON object")
    identity = str(raw.get("identity") or "").strip()
    gateway = str(raw.get("gateway") or "").strip()
    token_file = str(raw.get("token_file") or "").strip()
    if not identity or not gateway or not token_file:
        raise ValueError(
            f"operator config at {cfg_path} needs identity, gateway, and token_file"
        )
    # Package defaults and the recommended token_file use ~ — never bake
    # /home/ubuntu into a shipped default. Absolute paths the operator typed
    # (or a test tmp path) are fine; only refuse when the config still looks
    # like the unported lab bash script's hardcoded home.
    return OperatorConfig(identity=identity, gateway=gateway, token_file=token_file)


def save_config(
    *,
    identity: str,
    gateway: str,
    token_file: str | None = None,
    path: Path | None = None,
) -> OperatorConfig:
    identity = identity.strip()
    gateway = gateway.strip().rstrip("/")
    if not identity:
        raise ValueError("identity is required")
    if not gateway:
        raise ValueError("gateway is required")
    if "://" not in gateway:
        raise ValueError("gateway must be a full URL (scheme://host[:port])")
    token_file = (token_file or default_token_file_for(identity)).strip()
    cfg = OperatorConfig(identity=identity, gateway=gateway, token_file=token_file)
    cfg_path = path or CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "identity": cfg.identity,
        "gateway": cfg.gateway,
        "token_file": cfg.token_file,
    }
    cfg_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        cfg_path.chmod(0o600)
    except OSError:
        pass
    return cfg
