"""No baked-in gateway host.

A default that names a specific machine has the lifetime of that machine. On
2026-08-21 card #548 deliberately replaced `localhost` defaults with the tailnet
IP of the box that then hosted the mesh-gateway — correct reasoning at the time
(the gateway never bound loopback, and an IP needs only `tailscale up` where a
MagicDNS name needs more). On 2026-08-25 that box was retired and every one of
those defaults became an address that answers nothing.

The literal was not wrong. It expired. So this module ships no host at all:
resolve from the environment, and when the environment is silent, SAY SO rather
than dialling a machine chosen by whoever packaged the release.

See card #578.
"""

from __future__ import annotations

import os

ENV_GATEWAY = "MESH_GATEWAY_URL"
ENV_BRAIN_MCP = "SWARPH_BRAIN_MCP"


def env_gateway(env: str = ENV_GATEWAY) -> str:
    """The configured gateway URL, or "" when unset.

    Deliberately returns "" instead of raising: module-level constants are
    evaluated at IMPORT time, and a package that cannot be imported without a
    configured mesh cannot even print `--help`. The refusal belongs at the point
    of USE — see :func:`require_gateway`.
    """
    return (os.environ.get(env) or "").strip()


def require_gateway(
    value: str | None = None,
    *,
    env: str = ENV_GATEWAY,
    what: str = "mesh-gateway",
) -> str:
    """Return an explicit gateway URL, or exit with an actionable message.

    Call this where a request is about to be made, not at import.
    """
    resolved = (value or os.environ.get(env) or "").strip()
    if not resolved:
        raise SystemExit(
            f"{env} is not set, and swarph ships no default {what} host.\n"
            f"  A baked-in address expires the day that box is retired "
            f"(card #578; it happened on 2026-08-25).\n"
            f"  Set {env}=http://<host>:<port>, or pass --gateway explicitly."
        )
    return resolved
