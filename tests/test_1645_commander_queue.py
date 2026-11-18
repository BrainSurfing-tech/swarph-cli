"""card #1645: the commander queue and the #1630 cockpit on the public gateway.

HTTP contract (#1630 Appendix A with A.1-A.4) with scratch SQLite, a scratch
RSA issuer key and fake identities. Test names mirror mesh-gateway's
tests/test_1630_commander_queue.py where the behaviour is the same, so a
reviewer can diff the two suites. Fork-only behaviour (spec post 100318
section 3.1, rulings post 100349, drop's folds post 100336) has its own tests
at the end.
"""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")
import rfc8785  # noqa: E402,F401  plain import: a missing pin fails loudly wherever the extra is installed

from fastapi.testclient import TestClient  # noqa: E402

from test_meta_edge_identity import SHARED_TOKEN, _base_claims, gw, rsa_keys, sign  # noqa: E402,F401

SUB = "u-commander-1645"
OTHER = "u-someone-else"
STATIC = "commander-secret-1645"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ALLOWED = {("GET", "/commander/requests"), ("GET", "/commander/requests/{request_id}"),
           ("POST", "/commander/requests/{request_id}/decide")}
SIX_DIGITS = re.compile(r"\.\d{6}\+00:00$")


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def api(gw, monkeypatch, sign):
    """(server, client, peer headers, commander SSO headers) with the queue ON."""
    monkeypatch.setenv("META_EDGE_COMMANDER_SUB", SUB)
    monkeypatch.setenv("MESH_GATEWAY_COMMANDER_TOKEN", STATIC)
    monkeypatch.setattr(gw, "COMMANDER_TOKEN", STATIC)  # _authorize reads the import-time global
    with TestClient(gw.app) as client:
        yield gw, client, peer_headers(client, "cell-1645"), bearer(sign(_base_claims(sub=SUB)))


def peer_headers(client, name):
    """A registered peer with its per-peer token: a fresh gateway (zero ratified
    peers) mints one at register."""
    response = client.post("/peers/register", headers=bearer(SHARED_TOKEN),
                           json={"name": name, "url": "http://" + name})
    assert response.status_code == 200 and response.json()["peer_token"], response.text
    return bearer(response.json()["peer_token"])


def create(client, peer, **fields):
    response = client.post("/commander/requests", headers=peer,
                           json={"type": "decision", "question": "Ship?", **fields})
    assert response.status_code == 201, response.text
    return response.json()


def mint_cockpit(client, sso, body=None):
    response = client.post("/commander/cockpit-credential", headers=sso, json=body or {})
    assert response.status_code == 201, response.text
    return response.json()


def store_as(server, row_id, **columns):
    """Rows this gateway cannot create over HTTP (an action, an approved row, a
    legacy params shape) are written directly, the way mesh's legacy test does."""
    with server._conn() as connection:
        connection.execute("UPDATE commander_requests SET " + ", ".join(f"{k}=?" for k in columns) + " WHERE id=?",
                           (*columns.values(), row_id))


def counts(server):
    with server._conn() as connection:
        return (connection.execute("SELECT COUNT(*) FROM commander_requests").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM claude_messages").fetchone()[0])


# ── mirrored from mesh tests/test_1630_commander_queue.py ──────────────────

def test_created_and_fetched_row_has_derived_fields_and_original_strings(api):
    _, client, peer, _sso = api
    question = "Café ? " + "x" * 241
    result = client.post("/commander/requests", headers=peer, json={
        "type": "decision", "question": question, "options": ["oui", "non"],
        "needs_sso": False, "why": "Pour vérifier."})
    assert result.status_code == 201, result.text
    row = result.json()
    assert row["params"] == {"question": question, "options": ["oui", "non"], "needs_sso": False}
    assert row["why"] == "Pour vérifier."
    assert row["needs_sso"] is False
    assert len(row["digest"]) == 64
    assert row["summary"][1:4] == [question[:120], question[120:240], question[240:]]
    assert 1 <= len(row["summary"]) <= 12 and all(len(line) <= 120 for line in row["summary"])
    assert all(row[field] is None for field in ("decided_by", "decided_via", "executed_at", "executed_by", "result_detail"))
    fetched = client.get(f"/commander/requests/{row['id']}", headers=peer)
    assert fetched.status_code == 200
    assert fetched.json() == row


@pytest.mark.parametrize("changes", [
    {"question": "x" * 361}, {"question": "a\n b"}, {"question": "a\u0085b"},
    {"question": "a‮b"}, {"question": "a​b"}, {"options": ["x"] * 7},
    {"options": ["x" * 41]}, {"options": [1]}, {"needs_sso": "false"},
    {"why": "x" * 1001}, {"why": "a\x7fb"},
])
def test_creation_refuses_invalid_fields_before_writing(api, changes):
    server, client, peer, _ = api
    response = client.post("/commander/requests", headers=peer,
                           json={"type": "decision", "question": "ok?", **changes})
    assert response.status_code == 400, response.text
    assert counts(server) == (0, 0)


def test_reads_are_scoped_and_existing_input_canonicalisation_stays(api):
    _, client, peer, sso = api
    one = client.post("/commander/requests", headers=peer, json={
        "type": "decision", "question": "  oui ?  ", "options": [" oui "]}).json()
    other = peer_headers(client, "other-1645")
    two = client.post("/commander/requests", headers=other, json={
        "type": "decision", "question": "Réglage ?", "needs_sso": True}).json()
    assert one["params"]["question"] == "oui ?" and one["options"] == ["oui"]
    assert two["needs_sso"] is True
    assert [r["id"] for r in client.get("/commander/requests", headers=peer).json()["requests"]] == [one["id"]]
    assert len(client.get("/commander/requests", headers=sso).json()["requests"]) == 2
    assert client.get(f"/commander/requests/{two['id']}", headers=peer).status_code == 404


def test_queue_readers_are_only_peer_owner_commander_sso_static_or_cockpit(api, sign):
    _, client, peer, sso = api
    row = create(client, peer)
    for path in ("/commander/requests", f"/commander/requests/{row['id']}"):
        for headers in (bearer(SHARED_TOKEN), bearer(sign(_base_claims(sub=OTHER)))):
            assert client.get(path, headers=headers).status_code == 403
        assert client.get(path, headers=bearer(STATIC)).status_code == 200
        assert client.get(path, headers=sso).status_code == 200


def test_pending_cap_rolls_back_sixth_creation(api):
    server, client, peer, _ = api
    for number in range(5):
        assert client.post("/commander/requests", headers=peer, json={
            "type": "decision", "question": str(number)}).status_code == 201
    assert client.post("/commander/requests", headers=peer, json={
        "type": "decision", "question": "sixth"}).status_code == 429
    assert counts(server) == (5, 5)


def test_cockpit_mint_rotation_expiry_revoke_and_hash_only_storage(api):
    server, client, peer, sso = api
    assert client.post("/commander/cockpit-credential", headers=peer, json={}).status_code == 403
    assert client.post("/commander/cockpit-credential", headers=bearer(STATIC), json={}).status_code == 403
    first = mint_cockpit(client, sso)
    assert re.fullmatch(r"swck_[A-Za-z0-9_-]{43}", first["credential"])
    assert re.fullmatch(r"ck_[0-9a-f]{8}", first["credential_id"])
    assert re.fullmatch(r".*\.\d{6}\+00:00", first["expires_at"])
    assert datetime.now(timezone.utc) < datetime.fromisoformat(first["expires_at"]) <= datetime.now(timezone.utc) + timedelta(days=30)
    second = mint_cockpit(client, sso)
    with server._conn() as connection:
        rows = connection.execute("SELECT * FROM cockpit_credentials").fetchall()
        assert len(rows) == 2
        assert first["credential"] not in str([dict(row) for row in rows])
        assert second["credential"] not in str([dict(row) for row in rows])
        assert rows[0]["token_sha256"] == hashlib.sha256(first["credential"].encode()).hexdigest()
        assert rows[0]["minted_by"] == "sso:" + SUB
        assert connection.execute("SELECT COUNT(*) FROM peer_tokens WHERE token_sha256 IN (?,?)",
                                  tuple(row["token_sha256"] for row in rows)).fetchone()[0] == 0
    assert client.get("/commander/requests", headers=bearer(first["credential"])).json() == {"error": "cockpit_credential_revoked"}
    revoked = client.post("/commander/cockpit-credential/revoke", headers=bearer(STATIC), json={})
    assert revoked.status_code == 200 and revoked.json()["credential_id"] == second["credential_id"]
    assert client.post("/commander/cockpit-credential/revoke", headers=sso, json={}).json() == {"error": "not_found"}


@pytest.mark.parametrize("expires", ["bad", "2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z", None, 7, "2026-11-18",
                                     "2026-12-01T00:00:00"])
def test_bad_cockpit_expiry_is_exact_400_without_mint(api, expires):
    server, client, _, sso = api
    response = client.post("/commander/cockpit-credential", headers=sso, json={"expires_at": expires})
    assert response.status_code == 400
    assert response.json() == {"error": "bad_expires_at"}
    with server._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cockpit_credentials").fetchone()[0] == 0


def test_cockpit_middleware_walks_every_route_before_auth_or_validity_lookup(api, monkeypatch):
    """T2, the 480-pair walk: every route template times seven methods, minus
    the three allowed pairs, each with a valid, a revoked and an unknown
    credential. No lookup, no _authorize, no validity oracle."""
    server, client, _, sso = api
    revoked = mint_cockpit(client, sso)["credential"]
    valid = mint_cockpit(client, sso)["credential"]
    states = {"valid": valid, "revoked": revoked, "unknown": "swck_" + secrets.token_urlsafe(32)}
    cockpit_sql = []
    real_conn = server._conn

    @contextlib.contextmanager
    def traced_conn():
        with real_conn() as connection:
            connection.set_trace_callback(lambda sql: cockpit_sql.append(sql) if "cockpit_credentials" in sql else None)
            yield connection

    def forbidden_authorize(*args, **kwargs):
        raise AssertionError("cockpit must never reach the peer/SSO resolver")

    monkeypatch.setattr(server, "_conn", traced_conn)
    monkeypatch.setattr(server, "_authorize", forbidden_authorize)
    templates = sorted({route.path for route in server.app.routes})
    pairs = 0
    for template in templates:
        path = re.sub(r"\{[^}]+\}", "1", template)
        for method in METHODS:
            if (method, template) in ALLOWED:
                continue
            pairs += 1
            for state, token in states.items():
                response = client.request(method, path, headers=bearer(token))
                assert response.status_code == 403, (method, path, state, response.text)
                if method != "HEAD":  # HTTP forbids response bodies on HEAD.
                    assert response.json() == {"error": "cockpit_not_allowed"}
    print(f"\ncard #1645 T2: {len(templates)} route templates, {pairs} forbidden (method, route) pairs x 3 credentials")
    assert pairs == len(templates) * len(METHODS) - len(ALLOWED) == 480, "update this number when a route is added"
    assert cockpit_sql == []
    assert client.get("/not-a-route", headers=bearer("swck_unknown")).status_code == 403
    assert client.get("/commander/requests/", headers=bearer(valid)).status_code == 403
    monkeypatch.setattr(server, "_conn", real_conn)
    assert client.get("/commander/requests", headers=bearer(valid)).status_code == 200
    assert client.get("/commander/requests/1", headers=bearer(valid)).json() == {"error": "not_found"}


def test_allowed_routes_have_exact_cockpit_unknown_and_expired_errors(api):
    server, client, _, sso = api
    minted = mint_cockpit(client, sso)
    with server._conn() as connection:
        connection.execute("UPDATE cockpit_credentials SET expires_at='2020-01-01T00:00:00.000000+00:00'")
    for method, path in [("GET", "/commander/requests"), ("GET", "/commander/requests/999"),
                         ("POST", "/commander/requests/999/decide")]:
        for token, error in [("swck_no", "unknown"), (minted["credential"], "expired")]:
            response = client.request(method, path, headers=bearer(token), json={})
            assert response.status_code == 401
            assert response.json() == {"error": "cockpit_credential_" + error}


def test_cockpit_answers_with_audit_fields_and_notifies_proposer(api):
    _, client, peer, sso = api
    row = create(client, peer)
    minted = mint_cockpit(client, sso)
    response = client.post(f"/commander/requests/{row['id']}/decide", headers=bearer(minted["credential"]),
                           json={"decision": "answer", "answer": "Une autre réponse", "digest": row["digest"]})
    assert response.status_code == 200, response.text
    answered = response.json()
    assert answered["status"] == "answered"
    assert answered["answer"] == "Une autre réponse"
    assert answered["decided_via"] == "cockpit"
    assert answered["decided_by"] == "cockpit:" + minted["credential_id"]
    assert SIX_DIGITS.search(answered["decided_at"])
    assert answered["digest"] == row["digest"]
    inbox = client.get("/messages", headers=peer, params={"to": "cell-1645"}).json()
    assert answered["decided_by"] in str(inbox) and "decided_via=cockpit" in str(inbox)
    assert "from_node': 'commander'" in str(inbox) or '"from_node": "commander"' in str(inbox) or "commander" in str(inbox)


@pytest.mark.parametrize("fields,stored,body,status,error", [
    ({}, {"type": "merge_pr"}, {}, 403, "cockpit_not_allowed"),
    ({"needs_sso": True}, {}, {}, 403, "needs_sso"),
    ({}, {}, {"decision": "approve", "answer": "yes"}, 400, "bad_decision"),
    ({}, {}, {"answer": ""}, 400, "bad_answer"),
    ({}, {}, {"answer": "x"*1001}, 400, "bad_answer"),
    ({}, {}, {"answer": "a‮b"}, 400, "bad_answer"),
    ({}, {}, {"digest": None}, 400, "digest_required"),
    ({}, {}, {"digest": "A"*64}, 400, "digest_required"),
    ({}, {}, {"digest": "0"*64}, 409, "digest_mismatch"),
])
def test_decide_exact_errors_and_type_precedes_body(api, fields, stored, body, status, error):
    server, client, peer, sso = api
    row = create(client, peer, **fields)
    if stored:  # an action row cannot be created here (decisions only); its stored shape still refuses the cockpit
        store_as(server, row["id"], **stored)
    minted = mint_cockpit(client, sso)
    response = client.post(f"/commander/requests/{row['id']}/decide", headers=bearer(minted["credential"]),
                           json={"decision": "answer", "answer": "yes", "digest": row["digest"], **body})
    assert response.status_code == status, response.text
    assert response.json() == {"error": error}
    assert client.get(f"/commander/requests/{row['id']}", headers=sso).json()["status"] == "pending"


def test_decide_precedence_unknown_row_body_status_digest_and_missing_decision(api):
    _, client, peer, sso = api
    row = create(client, peer)
    cockpit = bearer(mint_cockpit(client, sso)["credential"])
    assert client.post("/commander/requests/999/decide", headers=cockpit,
                       json={"decision": "bad"}).json() == {"error": "not_found"}
    path = f"/commander/requests/{row['id']}/decide"
    assert client.post(path, headers=cockpit, json={}).status_code == 422
    assert client.post(path, headers=cockpit, json={"decision": "bad"}).json() == {"error": "bad_decision"}
    assert client.post(path, headers=cockpit, json={"decision": "answer"}).json() == {"error": "bad_answer"}
    assert client.post(path, headers=cockpit, json={"decision": "answer", "answer": "yes"}).json() == {"error": "digest_required"}
    assert client.post(path, headers=cockpit, json={"decision": "answer", "answer": "yes", "digest": row["digest"]}).status_code == 200
    # Bad body precedes non-pending; non-pending precedes a well-shaped wrong digest.
    assert client.post(path, headers=cockpit, json={"decision": "bad"}).json() == {"error": "bad_decision"}
    response = client.post(path, headers=cockpit, json={"decision": "answer", "answer": "yes", "digest": "0"*64})
    assert response.status_code == 409 and response.json() == {"error": "not_pending", "status": "answered"}


def test_sso_decisions_require_digest_and_keep_cockpit_only_restrictions_out(api):
    _, client, peer, sso = api
    row = create(client, peer, needs_sso=True)
    path = f"/commander/requests/{row['id']}/decide"
    assert client.post(path, headers=sso, json={"decision": "answer", "answer": "yes"}).json() == {"error": "digest_required"}
    result = client.post(path, headers=sso, json={"decision": "answer", "answer": "yes", "digest": row["digest"]})
    assert result.status_code == 200 and result.json()["decided_by"] == "sso:" + SUB
    assert result.json()["decided_via"] == "sso"


@pytest.mark.parametrize("legacy_params", [
    {"question": "legacy", "options": []},
    {"question": "x"*361, "options": []},
    {"question": "a\n b", "options": ["x"]*7},
])
def test_legacy_params_are_hashed_as_stored_and_overlimit_requires_sso(api, legacy_params):
    import json
    server, client, peer, sso = api
    created = create(client, peer)
    store_as(server, created["id"], params=json.dumps(legacy_params), question=legacy_params["question"],
             options=json.dumps(legacy_params["options"]))
    row = client.get(f"/commander/requests/{created['id']}", headers=peer).json()
    assert row["params"] == legacy_params and row["needs_sso"] is False
    assert row["digest"] == server.commander_queue.digest(row["id"], "decision", legacy_params)
    cockpit = bearer(mint_cockpit(client, sso)["credential"])
    body = {"decision": "answer", "answer": "yes", "digest": row["digest"]}
    response = client.post(f"/commander/requests/{row['id']}/decide", headers=cockpit, json=body)
    if legacy_params["question"] == "legacy":
        assert response.status_code == 200
    else:
        assert row["summary"] == ["This question exceeds the cockpit limits: answer it in the Command Center."]
        assert response.status_code == 403 and response.json() == {"error": "needs_sso"}
        assert client.post(f"/commander/requests/{row['id']}/decide", headers=sso, json=body).status_code == 200


@pytest.mark.parametrize("terminal", ["executed", "failed", "stale"])
def test_result_preserves_decision_and_records_actual_executor(api, terminal):
    """No row reaches 'approved' over HTTP here (decisions only), so the
    approved row is stored directly; the result route is otherwise mesh's."""
    server, client, peer, sso = api
    row = create(client, peer)
    path = f"/commander/requests/{row['id']}"
    decided_at = "2026-11-18T09:00:00.000001+00:00"
    store_as(server, row["id"], status="approved", decided_at=decided_at, decided_by="sso:" + SUB, decided_via="sso")
    for detail in (None, {"surprise": "not yet defined"}):
        assert client.post(path + "/result", headers=sso,
                           json={"status": terminal, "result_detail": detail}).status_code == 400
    assert client.post(path + "/result", headers=sso, json={"status": terminal, "result": "x"*4001}).status_code == 400
    assert client.post(path + "/result", headers=peer, json={"status": terminal}).status_code == 403
    response = client.post(path + "/result", headers=sso, json={"status": terminal, "result": "ok"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["decided_at"] == decided_at and result["decided_by"] == "sso:" + SUB
    assert result["executed_by"] == "sso:" + SUB
    assert SIX_DIGITS.search(result["executed_at"])
    assert result["result_detail"] is None and result["status"] == terminal
    assert client.post(path + "/result", headers=bearer(STATIC), json={"status": terminal}).status_code == 409


def test_hourly_cap_includes_withdrawn_rows_but_not_old_or_other_peers(api):
    server, client, peer, _ = api
    for number in range(10):
        row = create(client, peer, question=str(number))
        assert client.post(f"/commander/requests/{row['id']}/withdraw", headers=peer).status_code == 200
    response = client.post("/commander/requests", headers=peer, json={"type": "decision", "question": "eleventh"})
    assert response.status_code == 429
    assert create(client, peer_headers(client, "other-1645"), question="other")["status"] == "pending"
    with server._conn() as connection:
        connection.execute("UPDATE commander_requests SET created_at='2020-01-01T00:00:00.000000+00:00' WHERE proposer='cell-1645'")
    assert create(client, peer, question="new hour")["status"] == "pending"


def test_digest_prefix_collision_rolls_back_allocated_row_and_notification(api, monkeypatch):
    server, client, peer, _ = api
    create(client, peer)
    # Cryptographic boundary: force a 64-bit prefix collision without requiring 2^32 trials.
    original = server.commander_queue.digest
    monkeypatch.setattr(server.commander_queue, "digest", lambda *args: "a"*16 + original(*args)[16:])
    response = client.post("/commander/requests", headers=peer, json={"type": "decision", "question": "different"})
    assert response.status_code == 409
    assert counts(server) == (1, 1)


def test_inclusive_limits_are_accepted_and_why_is_not_trimmed(api):
    _, client, peer, sso = api
    row = create(client, peer, question="x"*360, options=["x"*40]*6, why=" " + "x"*998 + " ")
    assert len(row["summary"]) == 11
    assert row["why"] == " " + "x"*998 + " "
    assert len(row["created_at"].split(".")[1].split("+")[0]) == 6
    assert client.post(f"/commander/requests/{row['id']}/decide", headers=sso, json={
        "decision": "answer", "answer": "x"*1000, "digest": row["digest"]}).status_code == 200


def test_explicit_cockpit_expiry_and_sso_revoke_do_not_return_secret_again(api):
    _, client, _, sso = api
    expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    minted = mint_cockpit(client, sso, {"expires_at": expiry})
    assert datetime.fromisoformat(minted["expires_at"]) == datetime.fromisoformat(expiry)
    response = client.post("/commander/cockpit-credential/revoke", headers=sso,
                           json={"credential_id": minted["credential_id"]})
    assert response.status_code == 200
    assert set(response.json()) == {"credential_id", "revoked_at"}
    assert client.post("/commander/cockpit-credential", headers=sso, json={"credential": "x"}).status_code == 422


@pytest.mark.parametrize("header", ["Bearer\tswck_bad", "bearer swck_bad", "BEARER swck_bad", "Bearer   swck_bad",
                                    "Bearer swck_bad "])
def test_all_bearer_spellings_are_blocked_before_public_routes(api, header):
    _, client, _, _ = api
    response = client.get("/health", headers={"Authorization": header})
    assert response.status_code == 403 and response.json() == {"error": "cockpit_not_allowed"}


# ── fork-only behaviour (card #1645) ───────────────────────────────────────

def test_header_variants_not_caught_fall_to_the_ordinary_401_and_the_first_header_wins(api, sign):
    _, client, peer, sso = api
    valid = mint_cockpit(client, sso)["credential"]
    peer_token = peer["Authorization"].split(" ", 1)[1]
    for header in (valid, "Token " + valid, "Bearer " + valid.upper(),
                   "Bearer " + peer_token + ", Bearer " + valid):
        assert client.get("/peers", headers={"Authorization": header}).status_code == 401, header
    cockpit_first = [("authorization", "Bearer " + valid), ("authorization", "Bearer " + peer_token)]
    assert client.send(client.build_request("GET", "/peers", headers=cockpit_first)).status_code == 403
    assert client.send(client.build_request("GET", "/peers", headers=cockpit_first[::-1])).status_code == 200


def test_queue_is_off_without_a_commander_sub(gw, monkeypatch, sign):
    """T6 / D1: no META_EDGE_COMMANDER_SUB = 503 commander_not_configured on
    all 8 routes, probed with valid bodies and every non-cockpit credential,
    before any credential is read. A swck_ bearer stops at the middleware,
    except a locally minted one on the three allowed routes, which reaches
    the handler and gets the same 503."""
    monkeypatch.delenv("META_EDGE_COMMANDER_SUB", raising=False)
    monkeypatch.setenv("MESH_GATEWAY_COMMANDER_TOKEN", STATIC)
    monkeypatch.setattr(gw, "COMMANDER_TOKEN", STATIC)
    with TestClient(gw.app) as client:
        peer = peer_headers(client, "cell-off")
        sso = bearer(sign(_base_claims(sub=SUB)))
        routes = [
            ("POST", "/commander/requests", {"type": "decision", "question": "Ship?"}),
            ("GET", "/commander/requests", None),
            ("GET", "/commander/requests/1", None),
            ("POST", "/commander/requests/1/decide", {"decision": "answer", "answer": "yes", "digest": "0" * 64}),
            ("POST", "/commander/requests/1/result", {"status": "executed"}),
            ("POST", "/commander/requests/1/withdraw", None),
            ("POST", "/commander/cockpit-credential", {}),
            ("POST", "/commander/cockpit-credential/revoke", {}),
        ]
        assert len(routes) == 8
        for method, path, body in routes:
            for headers in (peer, sso, bearer(SHARED_TOKEN), bearer(STATIC), {}):
                response = client.request(method, path, headers=headers, json=body)
                assert response.status_code == 503, (method, path, headers, response.text)
                assert response.json() == {"error": "commander_not_configured"}
        local = "swck_" + secrets.token_urlsafe(32)
        with gw._conn() as connection:
            connection.execute("INSERT INTO cockpit_credentials VALUES('ck_00000001',?,?,?,NULL,'local:test')",
                               (hashlib.sha256(local.encode()).hexdigest(), "2026-01-01T00:00:00.000000+00:00",
                                "2099-01-01T00:00:00.000000+00:00"))
        for method, path, body in routes:
            response = client.request(method, path, headers=bearer(local), json=body)
            expected = 503 if (method, re.sub(r"/1(/|$)", r"/{request_id}\1", path)) in ALLOWED else 403
            assert response.status_code == expected, (method, path, response.text)
        assert client.get("/commander/requests", headers=bearer("swck_unknown")).json() == {"error": "cockpit_credential_unknown"}
        assert client.get("/commander/requests", headers=bearer("swck_unknown")).status_code == 401


def test_commander_identity_matrix(api, sign, monkeypatch):
    """T6 / D2: only a verified identity JWT whose sub is the configured one is
    the commander (F-M14); a peer token, the shared token, a cell-join JWT and
    another sub are not; with no public key a JWT is an ordinary bad token."""
    server, client, peer, sso = api
    row = create(client, peer)
    wrong = bearer(sign(_base_claims(sub=OTHER)))
    join = bearer(sign(_base_claims(sub=SUB, purpose="cell-join", owner=SUB)))
    decide = {"decision": "answer", "answer": "yes", "digest": row["digest"]}
    for headers in (wrong, join, peer, bearer(SHARED_TOKEN), bearer(STATIC)):
        assert client.post("/commander/cockpit-credential", headers=headers, json={}).status_code == 403
        assert client.post(f"/commander/requests/{row['id']}/decide", headers=headers, json=decide).status_code == 403
    for headers in (wrong, join, bearer(SHARED_TOKEN)):
        assert client.get("/commander/requests", headers=headers).status_code == 403
    assert client.get(f"/commander/requests/{row['id']}", headers=peer_headers(client, "other-1645")).status_code == 404
    pem = os.environ["META_EDGE_PUBLIC_KEY"]
    monkeypatch.delenv("META_EDGE_PUBLIC_KEY")  # sub set, no key: a JWT is an ordinary bad token
    assert client.post("/commander/cockpit-credential", headers=sso, json={}).status_code == 401
    monkeypatch.setenv("META_EDGE_PUBLIC_KEY", pem)
    assert client.post("/commander/cockpit-credential", headers=sso, json={}).status_code == 201
    assert client.post(f"/commander/requests/{row['id']}/decide", headers=sso, json=decide).status_code == 200
    assert len(client.get("/commander/requests", headers=sso).json()["requests"]) == 1
    assert len(client.get("/commander/requests", headers=bearer(STATIC)).json()["requests"]) == 1
    store_as(server, row["id"], status="approved")
    assert client.post(f"/commander/requests/{row['id']}/result", headers=sso, json={"status": "executed"}).status_code == 200


def test_commander_token_equal_to_shared_token_is_inert(api, monkeypatch):
    """D6 / F-M15: `swarph gateway serve` hands the shared token to every
    cell, so a static commander token equal to it counts as unset."""
    server, client, peer, sso = api
    row = create(client, peer)
    store_as(server, row["id"], status="approved")
    mint_cockpit(client, sso)
    monkeypatch.setenv("MESH_GATEWAY_COMMANDER_TOKEN", SHARED_TOKEN)
    assert client.get("/commander/requests", headers=bearer(SHARED_TOKEN)).status_code == 403
    assert client.post("/commander/cockpit-credential/revoke", headers=bearer(SHARED_TOKEN), json={}).status_code == 403
    assert client.post(f"/commander/requests/{row['id']}/result", headers=bearer(SHARED_TOKEN),
                       json={"status": "executed"}).status_code == 403
    monkeypatch.setenv("MESH_GATEWAY_COMMANDER_TOKEN", STATIC)
    assert client.get("/commander/requests", headers=bearer(STATIC)).status_code == 200


def test_twelve_parallel_mints_leave_one_active(api):
    """F-M12: the one-active partial index, not only the revoke-on-mint."""
    server, client, _, sso = api
    results = []

    def one():
        results.append(client.post("/commander/cockpit-credential", headers=sso, json={}).status_code)

    threads = [threading.Thread(target=one) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(201) == 12
    with server._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cockpit_credentials WHERE revoked_at IS NULL").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM cockpit_credentials").fetchone()[0] == 12


def test_one_active_credential_is_enforced_by_the_partial_index(api):
    """F-M12: with the one-active index removed, revoke-on-mint alone still
    passes a serialised client; a second active row written directly must fail."""
    import sqlite3
    server, client, _, sso = api
    mint_cockpit(client, sso)
    with server._conn() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO cockpit_credentials VALUES('ck_00000002','0'*64,'x','x',NULL,'local:test')")


def test_no_credential_in_logs_db_or_replies(api, caplog, capfd):
    """T5: the minted value appears only in the 201 body. Captured: every
    logger at DEBUG, stdout and stderr, during mint, a forbidden route and the
    three allowed routes; then the database file, its WAL and journal, and the
    list and GET bodies (kills L-M1 and L-M2)."""
    server, client, peer, sso = api
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()
    row = create(client, peer)
    minted = mint_cockpit(client, sso)
    raw = minted["credential"]
    forbidden = client.get("/health", headers=bearer(raw))
    listed = client.get("/commander/requests", headers=bearer(raw))
    one = client.get(f"/commander/requests/{row['id']}", headers=bearer(raw))
    decided = client.post(f"/commander/requests/{row['id']}/decide", headers=bearer(raw),
                          json={"decision": "answer", "answer": "oui", "digest": row["digest"]})
    assert (forbidden.status_code, listed.status_code, one.status_code, decided.status_code) == (403, 200, 200, 200)
    captured = capfd.readouterr()
    assert raw not in caplog.text and raw not in captured.out and raw not in captured.err
    assert hashlib.sha256(raw.encode()).hexdigest() not in caplog.text
    blob = b""
    for suffix in ("", "-wal", "-journal"):
        if os.path.exists(server.DB_PATH + suffix):
            blob += open(server.DB_PATH + suffix, "rb").read()
    assert raw.encode() not in blob and len(blob) > 0
    for text in (listed.text, one.text, decided.text):
        assert raw not in text
    with server._conn() as connection:
        assert raw not in "\n".join(r[0] for r in connection.execute("SELECT content FROM claude_messages"))


def test_timestamps_keep_six_fraction_digits_at_microsecond_zero(api, monkeypatch):
    """T9 / D5 / F-M17: create, decide, withdraw, result, mint and revoke at a
    clock reading microsecond 0."""
    server, client, peer, sso = api
    real = datetime

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            t = real.now(tz)
            return cls(t.year, t.month, t.day, t.hour, t.minute, t.second, 0, tzinfo=t.tzinfo)

    monkeypatch.setattr(server, "datetime", Frozen)
    first = create(client, peer, question="first?")
    second = create(client, peer, question="second?")
    third = create(client, peer, question="third?")
    store_as(server, third["id"], status="approved")
    seen = {
        "create created_at": first["created_at"],
        "decide decided_at": client.post(f"/commander/requests/{first['id']}/decide", headers=sso, json={
            "decision": "answer", "answer": "yes", "digest": first["digest"]}).json()["decided_at"],
        "withdraw decided_at": client.post(f"/commander/requests/{second['id']}/withdraw", headers=peer).json()["decided_at"],
        "result executed_at": client.post(f"/commander/requests/{third['id']}/result", headers=bearer(STATIC),
                                          json={"status": "executed"}).json()["executed_at"],
    }
    minted = mint_cockpit(client, sso)
    seen["mint expires_at"] = minted["expires_at"]
    seen["revoke revoked_at"] = client.post("/commander/cockpit-credential/revoke", headers=sso, json={}).json()["revoked_at"]
    with server._conn() as connection:
        seen["mint stored created_at"] = connection.execute("SELECT created_at FROM cockpit_credentials").fetchone()[0]
    listed = client.get("/commander/requests", headers=sso).json()["requests"]
    seen["list withdrawn decided_at"] = next(r["decided_at"] for r in listed if r["status"] == "withdrawn")
    assert all(seen.values())
    assert {name: bool(SIX_DIGITS.search(value)) for name, value in seen.items()} == {name: True for name in seen}
    assert ".000000+00:00" in seen["withdraw decided_at"]


def test_cockpit_allowlist_is_the_outermost_middleware(api):
    """F-M18: Starlette runs the last-added middleware first; user_middleware
    lists it first."""
    server, _, _, _ = api
    assert server.app.user_middleware[0].cls is server._CockpitAllowlist
    assert [m.cls.__name__ for m in server.app.user_middleware][-1] == "CORSMiddleware"


def test_digest_wrong_only_in_its_last_character_is_a_mismatch(api):
    """D-M3 (fold F1): a prefix-only compare survives every other test."""
    _, client, peer, sso = api
    row = create(client, peer)
    cockpit = bearer(mint_cockpit(client, sso)["credential"])
    last = "0" if row["digest"][-1] != "0" else "1"
    response = client.post(f"/commander/requests/{row['id']}/decide", headers=cockpit,
                           json={"decision": "answer", "answer": "yes", "digest": row["digest"][:-1] + last})
    assert response.status_code == 409 and response.json() == {"error": "digest_mismatch"}
    assert client.get(f"/commander/requests/{row['id']}", headers=peer).json()["status"] == "pending"


@pytest.mark.parametrize("body", [
    {"type": "merge_pr", "repo": "example-org/example-repo", "pr": 1, "head_sha": "a" * 40},
    {"type": "release_finish", "pr": 1, "head_sha": "a" * 40, "version": "1.2.3"},
])
def test_action_types_are_refused_with_a_typed_error(api, body):
    """Q3 ruling: decisions only on a third-party gateway."""
    server, client, peer, _ = api
    response = client.post("/commander/requests", headers=peer, json=body)
    assert response.status_code == 400, response.text
    assert response.json() == {"error": "action_not_supported", "type": body["type"]}
    assert client.post("/commander/requests", headers=peer, json={"type": "other", "question": "?"}).status_code == 400
    assert counts(server) == (0, 0)


def test_card_id_is_refused_without_a_board(api):
    """D3: no board here, so every card is unknown; the same 404 as mesh."""
    server, client, peer, _ = api
    response = client.post("/commander/requests", headers=peer, json={"type": "decision", "question": "?", "card_id": 1645})
    assert response.status_code == 404 and response.json() == {"detail": "card 1645 not found"}
    assert counts(server) == (0, 0)


def test_the_name_commander_cannot_register(api):
    """Fold F2: the notify sender is never a peer; the request row is the record.
    Register and message_post share the PEER_NAME_RE allow-list (security
    review), so every spelling refused on post is refused here, and ordinary
    kebab-case names stay allowed on both."""
    server, client, _, _ = api
    for name, register_status, _ in RESERVED_SPELLINGS:
        response = client.post("/peers/register", headers=bearer(SHARED_TOKEN), json={"name": name, "url": "http://x"})
        assert response.status_code == register_status, (name, response.text)
        assert ("reserved" if register_status == 400 else "must match") in response.text
    with server._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claude_peers").fetchone()[0] == 1  # the fixture's peer only
        assert connection.execute("SELECT COUNT(*) FROM peer_tokens").fetchone()[0] == 1
    for name in ALLOWED_NAMES:
        assert client.post("/peers/register", headers=bearer(SHARED_TOKEN), json={"name": name, "url": "http://x"}).status_code == 200, name
        assert client.post("/messages", headers=bearer(SHARED_TOKEN), json={
            "from_node": name, "to_node": "cell-1645", "kind": "fyi", "content": "hi"}).status_code == 200, name


def test_shared_token_cannot_create_or_withdraw_and_duplicates_are_refused(api):
    _, client, peer, _ = api
    assert client.post("/commander/requests", headers=bearer(SHARED_TOKEN),
                       json={"type": "decision", "question": "?"}).status_code == 400
    row = create(client, peer)
    assert client.post("/commander/requests", headers=peer, json={"type": "decision", "question": "Ship?"}).status_code == 409
    assert client.post(f"/commander/requests/{row['id']}/withdraw", headers=bearer(SHARED_TOKEN)).status_code == 403
    assert client.post(f"/commander/requests/{row['id']}/withdraw",
                       headers=peer_headers(client, "other-1645")).status_code == 403
    assert client.post(f"/commander/requests/{row['id']}/withdraw", headers=peer).json()["status"] == "withdrawn"
    assert client.post(f"/commander/requests/{row['id']}/withdraw", headers=peer).status_code == 409


# (spelling, expected status at register, expected status at message_post): the exact
# name is reserved (400 / 403); everything else fails the PEER_NAME_RE allow-list (422).
RESERVED_SPELLINGS = [("commander", 400, 403), ("Commander", 422, 422), (" commander", 422, 422),
                      ("COMMANDER\t", 422, 422), ("comm\u200dander", 422, 422), ("comma\u0301nder", 422, 422),
                      ("\u0441ommander", 422, 422),
                      ("\uff43\uff4f\uff4d\uff4d\uff41\uff4e\uff44\uff45\uff52", 422, 422), ("com mander", 422, 422)]
ALLOWED_NAMES = ["lab-ovh", "commander-tools", "board-publisher", "mycommander", "commander2"]


@pytest.mark.parametrize("sender,_register_status,post_status", RESERVED_SPELLINGS, ids=lambda v: repr(v) if isinstance(v, str) else str(v))
def test_no_caller_can_post_a_message_as_commander(api, monkeypatch, sender, _register_status, post_status):
    """Security review on #5840 (1, 3): only the gateway's own notify path writes
    from_node 'commander'. The exact name is refused (403) for a peer token, the
    shared token and an SSO caller under both caller-binding modes; every
    lookalike fails the PEER_NAME_RE allow-list (422)."""
    server, client, peer, sso = api
    for enforce in (False, True):
        monkeypatch.setattr(server, "MESH_CALLER_BINDING_ENFORCE", enforce)
        for headers in (peer, bearer(SHARED_TOKEN), sso):
            for target in ({"to_node": "cell-1645"}, {"channel": "general"}):
                response = client.post("/messages", headers=headers, json={
                    "from_node": sender, "kind": "answer", "content": "approved", **target})
                assert response.status_code == post_status, (enforce, headers, target, response.text)
            # the other route that writes a caller-supplied from_node (kind 'unblock')
            response = client.post("/tasks/1/unblock", headers=headers, json={"posted_by": sender, "evidence": "x"})
            assert response.status_code == post_status, (enforce, headers, "unblock", response.text)
    with server._conn() as connection:
        assert connection.execute("SELECT COUNT(*) FROM claude_messages WHERE from_node LIKE '%ommander%'").fetchone()[0] == 0
    row = create(client, peer)  # the notify path still writes it
    with server._conn() as connection:
        assert connection.execute("SELECT from_node FROM claude_messages").fetchone()[0] == "commander"
    assert client.post("/messages", headers=peer, json={
        "from_node": "cell-1645", "to_node": "other", "kind": "fyi", "content": str(row["id"])}).status_code == 200


def test_dedupe_is_scoped_to_the_proposer(api):
    """Security review on #5840 (2) / D8: identical params from another peer
    are a new request, and a 409 names only the caller's own pending id."""
    server, client, peer_a, _ = api
    peer_b = peer_headers(client, "cell-b-1645")
    a = create(client, peer_a, question="Same?", options=["yes", "no"])
    b = create(client, peer_b, question="Same?", options=["yes", "no"])
    assert a["id"] != b["id"] and a["proposer"] != b["proposer"]
    again = client.post("/commander/requests", headers=peer_a, json={"type": "decision", "question": "Same?", "options": ["yes", "no"]})
    assert again.status_code == 409 and str(a["id"]) in again.text and str(b["id"]) not in again.text
    again = client.post("/commander/requests", headers=peer_b, json={"type": "decision", "question": "Same?", "options": ["yes", "no"]})
    assert again.status_code == 409 and str(b["id"]) in again.text and str(a["id"]) not in again.text
    with server._conn() as connection:
        keys = [r[0] for r in connection.execute("SELECT dedupe_key FROM commander_requests ORDER BY id")]
    assert keys[0].startswith("cell-1645\n") and keys[1].startswith("cell-b-1645\n")
