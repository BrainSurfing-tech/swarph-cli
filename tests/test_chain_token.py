import pytest
from swarph_cli.chain_token import sign_chain_token, verify_chain_token

SECRET = "producer-secret-xyz"

def test_sign_verify_roundtrip():
    t = sign_chain_token(SECRET, "chain-1", 3, "jti-abc")
    assert verify_chain_token(SECRET, t) == {"chain_id": "chain-1", "depth": 3, "jti": "jti-abc"}

def test_tampered_payload_rejected():
    t = sign_chain_token(SECRET, "chain-1", 0, "jti-abc")
    body, sig = t.split(".")
    import base64, json
    forged = json.dumps({"chain_id": "chain-1", "depth": 99, "jti": "jti-abc"}).encode()
    bad = base64.urlsafe_b64encode(forged).decode().rstrip("=") + "." + sig
    assert verify_chain_token(SECRET, bad) is None   # forged well-formed payload, sig mismatch

def test_wrong_secret_rejected():
    t = sign_chain_token(SECRET, "c", 1, "j")
    assert verify_chain_token("other-secret", t) is None

def test_malformed_never_raises():
    for bad in ["", "nodot", "a.b.c", "!!.??", "x."]:
        assert verify_chain_token(SECRET, bad) is None
