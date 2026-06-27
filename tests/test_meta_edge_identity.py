"""B1+B2 — Meta-Edge identity trust + login-scoped cell registry (gateway side).

Security foundation for META_EDGE_IDENTITY_CONTRACT.md: the gateway TRUSTS
Meta-Edge's RS256 SSO/join JWTs (verified with Meta-Edge's PUBLIC key) but never
signs them. These tests are the load-bearing security gate — especially the
REJECT cases (§2): a forged/expired/algorithm-confused token MUST fail closed,
and a JWT-shaped bearer that fails verification MUST 401 (never fall through to
the shared/peer-token paths).

We mint test tokens with PyJWT signing with a throwaway RSA private key, and set
META_EDGE_PUBLIC_KEY to the matching public PEM. No network, no real Meta-Edge.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import sqlite3
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ISSUER = "meta-edge"
AUDIENCE = "swarph-gateway"
SHARED_TOKEN = "test-shared-token"  # NB: no dots → not JWT-shaped


# ─── keypair + token signing ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def rsa_keys():
    """A throwaway RSA keypair: (private_pem, public_pem) as PEM strings."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _base_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "u-123",
        "iat": now,
        "exp": now + 3600,
        "provider": "github",
        "login": "octocat",
    }
    claims.update(overrides)
    return claims


@pytest.fixture
def sign(rsa_keys):
    """sign(claims) -> RS256 JWT signed with the test private key."""
    private_pem, _ = rsa_keys

    def _sign(claims: dict) -> str:
        return jwt.encode(claims, private_pem, algorithm="RS256")

    return _sign


# ─── gateway module (temp DB + Meta-Edge key configured) ──────────────────

@pytest.fixture
def gw(tmp_path, monkeypatch, rsa_keys):
    """Reload the gateway server module against a temp DB with Meta-Edge enabled.

    Reload re-reads the env-driven module globals AND re-runs _init_db() against
    the temp DB (which gets the new owner column from schema.sql). No other test
    imports this module, so reloading it is safe.
    """
    _, public_pem = rsa_keys
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", SHARED_TOKEN)
    monkeypatch.setenv("MESH_DB_PATH", str(tmp_path / "mesh.db"))
    monkeypatch.setenv("META_EDGE_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("META_EDGE_ISSUER", ISSUER)
    monkeypatch.setenv("META_EDGE_AUDIENCE", AUDIENCE)
    monkeypatch.delenv("MESH_GATEWAY_COMMANDER_TOKEN", raising=False)
    monkeypatch.delenv("META_EDGE_PUBLIC_KEY_FILE", raising=False)
    import swarph_cli.gateway.server as server
    importlib.reload(server)
    return server


@pytest.fixture
def client(gw):
    return TestClient(gw.app)


def _bearer(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _owner_of(gw, name: str):
    con = sqlite3.connect(gw.DB_PATH)
    try:
        row = con.execute("SELECT owner FROM claude_peers WHERE name=?", (name,)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


# ══ 1. valid identity token → claims + user_identity context ═══════════════

def test_valid_identity_token_verifies_and_authorizes(gw, sign):
    tok = sign(_base_claims())
    claims = gw._verify_meta_edge_token(tok)
    assert claims is not None
    assert claims["sub"] == "u-123"
    assert claims["iss"] == ISSUER and claims["aud"] == AUDIENCE

    ctx = gw._authorize(f"Bearer {tok}")
    assert ctx.kind == "user_identity"
    assert ctx.user == "u-123"
    assert ctx.provider == "github"
    assert ctx.login == "octocat"


# ══ 2. REJECT — the load-bearing security tests ════════════════════════════

def test_reject_tampered_signature(gw, sign):
    tok = sign(_base_claims())
    head, payload, sig = tok.split(".")
    # Corrupt an actual signature BYTE. Flipping the last base64url char can be a
    # no-op on trailing bits (the "tamper" decodes to the same bytes) — that made
    # this flaky: it passed on CI py3.11 and failed on py3.12 by token luck.
    import base64
    raw = bytearray(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)))
    raw[0] ^= 0xFF
    bad_sig = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
    tampered = f"{head}.{payload}.{bad_sig}"
    assert gw._verify_meta_edge_token(tampered) is None
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tampered}")
    assert e.value.status_code == 401


def test_reject_expired(gw, sign):
    now = int(time.time())
    tok = sign(_base_claims(iat=now - 7200, exp=now - 3600))
    assert gw._verify_meta_edge_token(tok) is None
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tok}")
    assert e.value.status_code == 401


def test_reject_wrong_issuer(gw, sign):
    tok = sign(_base_claims(iss="evil-issuer"))
    assert gw._verify_meta_edge_token(tok) is None
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tok}")
    assert e.value.status_code == 401


def test_reject_wrong_audience(gw, sign):
    tok = sign(_base_claims(aud="some-other-service"))
    assert gw._verify_meta_edge_token(tok) is None
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tok}")
    assert e.value.status_code == 401


def test_reject_alg_none(gw):
    """An unsigned alg:none token (forged header, empty signature) must NOT verify."""
    tok = jwt.encode(_base_claims(), key="", algorithm="none")
    assert gw._verify_meta_edge_token(tok) is None
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tok}")
    assert e.value.status_code == 401


def _b64url(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def test_reject_hs256_algorithm_confusion(gw, rsa_keys):
    """HS256 signed with the RSA PUBLIC key bytes as the HMAC secret — the classic
    algorithm-confusion attack. Pinning algorithms=['RS256'] must reject it.

    We hand-craft the token (PyJWT's encode now refuses an asymmetric key as an
    HMAC secret — but a real attacker doesn't use PyJWT to forge; they HMAC by
    hand with the *known public* key as the secret). The DECODE side is what must
    reject it, which is exactly what algorithms=['RS256'] guarantees."""
    _, public_pem = rsa_keys
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(_base_claims()).encode())
    signing_input = header + b"." + payload
    sig = _b64url(hmac.new(public_pem.encode(), signing_input, hashlib.sha256).digest())
    tok = (signing_input + b"." + sig).decode()
    assert gw._verify_meta_edge_token(tok) is None
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tok}")
    assert e.value.status_code == 401


def test_reject_missing_sub(gw, sign):
    claims = _base_claims()
    del claims["sub"]
    tok = sign(claims)
    assert gw._verify_meta_edge_token(tok) is None
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tok}")
    assert e.value.status_code == 401


# ══ 3. JWT-shaped bearer that fails verification → 401, NOT a fall-through ══

def test_failed_jwt_does_not_fall_through_to_shared_token(gw, sign):
    """A JWT-shaped bearer that fails verification must 401 — it is never retried
    against the shared/peer-token paths (that would be an auth downgrade)."""
    now = int(time.time())
    tok = sign(_base_claims(iat=now - 7200, exp=now - 3600))  # expired, well-formed RS256 JWT
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {tok}")
    assert e.value.status_code == 401

    # And a totally bogus 3-segment string is rejected the same way (not treated
    # as a shared/peer token).
    with pytest.raises(gw.HTTPException) as e2:
        gw._authorize("Bearer aaa.bbb.ccc")
    assert e2.value.status_code == 401


# ══ 4. join-key at /peers/register → owner stamped + peer-token minted ═════

def test_cell_join_register_stamps_owner_and_mints_token(gw, client, sign):
    join_key = sign(_base_claims(sub="join-key-id", purpose="cell-join", owner="u-123"))
    r = client.post(
        "/peers/register",
        json={"name": "cell-x", "url": "http://cell-x:8787", "capabilities": {}},
        headers=_bearer(join_key),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "registered"
    assert body["peer_token"]  # a fresh peer-token was minted + returned once
    assert body["token_status"] == "minted"
    assert _owner_of(gw, "cell-x") == "u-123"


def test_cell_join_without_owner_claim_rejected(gw, sign):
    bad = sign(_base_claims(sub="jk", purpose="cell-join"))  # no owner claim
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize(f"Bearer {bad}")
    assert e.value.status_code == 401


# ══ 5. identity token at /peers/register → owner == sub ════════════════════

def test_identity_register_stamps_owner_as_sub(gw, client, sign):
    tok = sign(_base_claims(sub="u-777"))
    r = client.post(
        "/peers/register",
        json={"name": "cell-y", "url": "http://cell-y:8787", "capabilities": {}},
        headers=_bearer(tok),
    )
    assert r.status_code == 200, r.text
    assert _owner_of(gw, "cell-y") == "u-777"


# ══ 6. GET /peers as user_identity → only that user's owned cells ══════════

def test_peers_list_login_scoped(gw, client, sign):
    # Seed two owners via their identity tokens.
    tok_123 = sign(_base_claims(sub="u-123"))
    tok_999 = sign(_base_claims(sub="u-999"))
    for tok, name in ((tok_123, "owned-by-123"), (tok_999, "owned-by-999")):
        rr = client.post(
            "/peers/register",
            json={"name": name, "url": f"http://{name}:8787", "capabilities": {}},
            headers=_bearer(tok),
        )
        assert rr.status_code == 200, rr.text

    # u-123 sees ONLY its own cell.
    r = client.get("/peers", headers=_bearer(tok_123))
    assert r.status_code == 200, r.text
    names = {p["name"] for p in r.json()["peers"]}
    assert names == {"owned-by-123"}

    # The shared (lab) token sees BOTH (unchanged — no scoping).
    r_all = client.get("/peers", headers=_bearer(SHARED_TOKEN))
    assert r_all.status_code == 200
    all_names = {p["name"] for p in r_all.json()["peers"]}
    assert {"owned-by-123", "owned-by-999"} <= all_names


# ══ 7. regression — shared-token paths unchanged ═══════════════════════════

def test_shared_token_register_list_auth_unchanged(gw, client):
    # shared token authenticates and resolves to the shared_token regime.
    ctx = gw._authorize(f"Bearer {SHARED_TOKEN}")
    assert ctx.regime == "shared_token"
    assert ctx.kind is None  # not a Meta-Edge principal

    # shared-token register → owner stays NULL (lab behavior).
    r = client.post(
        "/peers/register",
        json={"name": "lab-cell", "url": "http://lab-cell:8787", "capabilities": {}},
        headers=_bearer(SHARED_TOKEN),
    )
    assert r.status_code == 200, r.text
    assert _owner_of(gw, "lab-cell") is None

    # shared-token list returns the lab cell (sees all).
    r2 = client.get("/peers", headers=_bearer(SHARED_TOKEN))
    assert r2.status_code == 200
    assert "lab-cell" in {p["name"] for p in r2.json()["peers"]}

    # a bad opaque (non-JWT) token still 401s exactly as before.
    with pytest.raises(gw.HTTPException) as e:
        gw._authorize("Bearer totally-wrong-token")
    assert e.value.status_code == 401
