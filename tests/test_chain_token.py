import pytest
from swarph_cli.chain_token import sign_chain_token, verify_chain_token

SECRET = "producer-secret-xyz"

def test_sign_verify_roundtrip():
    t = sign_chain_token(SECRET, "chain-1", 3, "jti-abc")
    assert verify_chain_token(SECRET, t) == {"chain_id": "chain-1", "depth": 3, "jti": "jti-abc"}

def test_tampered_payload_rejected():
    t = sign_chain_token(SECRET, "chain-1", 0, "jti-abc")
    _, sig = t.split(".")
    import base64, json
    # CANONICAL re-encode (same separators/sort_keys the signer uses) with ONLY depth
    # changed → isolates that the HMAC catches the VALUE change, not a formatting diff.
    forged = json.dumps({"chain_id": "chain-1", "depth": 99, "jti": "jti-abc"},
                        separators=(",", ":"), sort_keys=True).encode()
    bad = base64.urlsafe_b64encode(forged).decode().rstrip("=") + "." + sig
    assert verify_chain_token(SECRET, bad) is None   # depth 0→99, canonical form, sig mismatch

def test_wrong_secret_rejected():
    t = sign_chain_token(SECRET, "c", 1, "j")
    assert verify_chain_token("other-secret", t) is None

def test_malformed_never_raises():
    for bad in ["", "nodot", "a.b.c", "!!.??", "x."]:
        assert verify_chain_token(SECRET, bad) is None

def test_non_str_token_fails_safe_not_raises():
    # opaque tokens arrive from untrusted input (network / mesh DM); a non-str
    # value (None, bytes, int) must fail safe to None, never AttributeError out
    # of the caller. Regression for the gateway-copy fix that lagged the CLI.
    for bad in [None, 123, b"a.b", ["a", "b"], {"chain_id": "x"}]:
        assert verify_chain_token(SECRET, bad) is None
