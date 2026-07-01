"""Producer-signed chain-token for event-chaining (spec §5a). Compact HMAC blob the
producer signs + verifies; the cell only passes it through opaquely (can't forge it)."""
from __future__ import annotations
import base64, hashlib, hmac, json
from typing import Optional


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_chain_token(secret: str, chain_id: str, depth: int, jti: str) -> str:
    payload = json.dumps({"chain_id": chain_id, "depth": int(depth), "jti": jti},
                         separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify_chain_token(secret: str, token: str) -> Optional[dict]:
    try:
        body_s, sig_s = token.split(".", 1)
        payload = _unb64(body_s)
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(sig_s), expected):
            return None
        d = json.loads(payload)
        if not isinstance(d, dict) or {"chain_id", "depth", "jti"} - d.keys():
            return None
        return {"chain_id": d["chain_id"], "depth": int(d["depth"]), "jti": d["jti"]}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, Exception):
        return None
