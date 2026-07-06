# Gateway Brain-Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a mesh-gateway endpoint `POST /brain/query` that authenticates a cell by its **mesh token** (via the gateway's existing `_authorize`) and proxies a read-only query to gbrain using one gateway-held token — so no per-cell `gbrain_` token exists.

**Architecture:** The gateway endpoint constructs the gbrain MCP `query` call itself (read-only by construction — no write/admin path ever built), proxies it to gbrain over stdlib `urllib.request` wrapped in `run_in_threadpool` (no new runtime dep), and returns a clean JSON chunk array. The `brain-ask` client gains a `SWARPH_BRAIN_GATEWAY` branch that hits that endpoint with the cell's peer token; the direct-`:8792` path is untouched.

**Tech Stack:** Python 3, FastAPI (gateway), `urllib.request` + `starlette.concurrency.run_in_threadpool`, pytest + `fastapi.testclient.TestClient`.

## Global Constraints

- **Read-only by construction** — the gateway ONLY ever builds an MCP call with `params.name == "query"`. No code path emits write/admin. A test asserts the upstream body is always `name=query`.
- **Reuse `_authorize(authorization)` verbatim** (`gateway/server.py:764`) — no new auth code. Bad/missing token → `HTTPException(401)` unchanged.
- **`GATEWAY_GBRAIN_URL`** default `http://100.107.222.72:8792/mcp`; **`GATEWAY_GBRAIN_TOKEN`** unset → endpoint returns `503`. Read via `os.environ.get(...)` at module top (matches `AUTH_TOKEN = os.environ.get("MESH_GATEWAY_TOKEN", "")` at `server.py:62`).
- **Client env `SWARPH_BRAIN_GATEWAY`** (distinct from the pre-existing `--gateway`/`GBRAIN_MCP_URL` which names the *gbrain* endpoint). When set → gateway path with the **peer token**; when unset → today's direct `:8792` path, byte-unchanged.
- **No new runtime dependency** — the gateway's outbound HTTP uses stdlib `urllib.request` in a threadpool.
- **Fail loud** — gbrain unreachable / non-2xx / unparseable → `502` (never a swallowed empty result).
- **Public PyPI** — synthetic test fixtures only; **NO real tokens** in code or tests.
- **Version bump `0.25.0 → 0.26.0`** in both `pyproject.toml` and `src/swarph_cli/__init__.py`.
- **TDD**; plan ends at merged + green. Publish, gateway env config, re-pointing workstation-lc, and revoking the stopgap token are **commander-gated and OUT of scope**.
- Branch `feat/gateway-brain-proxy` is checked out; stage only the named files per commit (never `git add -A`; untracked local-only `.codegraph/` must not be committed).
- Test invocation: `cd /home/ubuntu/swarph-cli && PYTHONPATH=src python3 -m pytest <path> -v` (namespace pkg, no install; `python` is not on PATH).

## File Structure

- `src/swarph_cli/gateway/server.py` — module-top config vars; a sync helper `_brain_query_upstream(question, limit)`; a Pydantic `BrainQueryRequest`; the `@app.post("/brain/query")` endpoint.
- `src/swarph_cli/commands/brain_ask.py` — a `_gateway_query(...)` helper; a `SWARPH_BRAIN_GATEWAY` branch in `run_brain_ask`.
- `tests/test_gateway_brain.py` — NEW; TestClient tests for the endpoint.
- `tests/test_brain_ask_command.py` — add client-path tests.
- `pyproject.toml`, `src/swarph_cli/__init__.py` — version bump.

---

### Task 1: Gateway `/brain/query` endpoint + config

**Files:**
- Modify: `src/swarph_cli/gateway/server.py` (config near `:62`; helper + model + endpoint near the other `@app.post` routes)
- Test: `tests/test_gateway_brain.py` (create)

**Interfaces:**
- Consumes: `_authorize(authorization) -> AuthContext` (`server.py:764`); the FastAPI `app`.
- Produces: `POST /brain/query` accepting `{"query": str, "limit": int=8}` with `Bearer <mesh token>` → `{"chunks": [ {slug,title,chunk_text,score,...}, ... ]}`. Sync helper `_brain_query_upstream(question: str, limit: int) -> list` (builds `name=query` MCP body, POSTs to gbrain, returns the chunk array; raises on any upstream failure).

- [ ] **Step 1: Write the failing tests** — create `tests/test_gateway_brain.py`:

```python
import json
import os
import importlib
import pytest
from types import SimpleNamespace


def _load_app(monkeypatch, *, gbrain_token="gbrain_hostheld", auth="tok_shared"):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", auth)
    monkeypatch.setenv("GATEWAY_GBRAIN_TOKEN", gbrain_token)
    monkeypatch.setenv("GATEWAY_GBRAIN_URL", "http://127.0.0.1:8792/mcp")
    monkeypatch.setenv("MESH_DB_PATH", ":memory:")
    from swarph_cli.gateway import server
    importlib.reload(server)          # re-read module-top env config
    return server


def _sse(chunks):
    inner = json.dumps(chunks)
    env = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": inner}]}}
    return "event: message\ndata: " + json.dumps(env) + "\n\n"


def _client(server):
    from fastapi.testclient import TestClient
    return TestClient(server.app)


def test_brain_query_authenticates_and_returns_chunks(monkeypatch):
    server = _load_app(monkeypatch)
    chunks = [{"slug": "s1", "title": "T", "chunk_text": "hello", "score": 0.9}]
    monkeypatch.setattr(server, "_brain_query_upstream", lambda q, n: chunks)
    r = _client(server).post("/brain/query", json={"query": "hi", "limit": 5},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 200
    assert r.json() == {"chunks": chunks}


def test_brain_query_bad_token_401(monkeypatch):
    server = _load_app(monkeypatch)
    monkeypatch.setattr(server, "_brain_query_upstream", lambda q, n: [])
    r = _client(server).post("/brain/query", json={"query": "hi"},
                             headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_brain_query_unconfigured_503(monkeypatch):
    server = _load_app(monkeypatch, gbrain_token="")   # GATEWAY_GBRAIN_TOKEN unset
    r = _client(server).post("/brain/query", json={"query": "hi"},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 503


def test_brain_query_empty_query_400(monkeypatch):
    server = _load_app(monkeypatch)
    monkeypatch.setattr(server, "_brain_query_upstream", lambda q, n: [])
    r = _client(server).post("/brain/query", json={"query": "   "},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 400


def test_brain_query_upstream_error_502(monkeypatch):
    server = _load_app(monkeypatch)
    def boom(q, n): raise RuntimeError("gbrain down")
    monkeypatch.setattr(server, "_brain_query_upstream", boom)
    r = _client(server).post("/brain/query", json={"query": "hi"},
                             headers={"Authorization": "Bearer tok_shared"})
    assert r.status_code == 502


def test_upstream_helper_is_read_only_by_construction(monkeypatch):
    """The MCP body the proxy sends upstream ALWAYS has params.name == 'query'."""
    server = _load_app(monkeypatch)
    captured = {}
    class FakeResp:
        def read(self): return _sse([{"slug": "s"}]).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        captured["auth"] = req.get_header("Authorization")
        return FakeResp()
    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    out = server._brain_query_upstream("anything", 8)
    assert out == [{"slug": "s"}]
    assert captured["body"]["params"]["name"] == "query"          # read-only lock
    assert captured["auth"] == "Bearer gbrain_hostheld"           # gateway's held token
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /home/ubuntu/swarph-cli && PYTHONPATH=src python3 -m pytest tests/test_gateway_brain.py -v`
Expected: FAIL — `AttributeError` / 404 (endpoint + `_brain_query_upstream` don't exist yet).

- [ ] **Step 3: Add the config vars** — in `server.py`, near the other module-top config (`AUTH_TOKEN = os.environ.get("MESH_GATEWAY_TOKEN", "")` ~`:62`), add:

```python
GATEWAY_GBRAIN_URL = os.environ.get("GATEWAY_GBRAIN_URL", "http://100.107.222.72:8792/mcp")
GATEWAY_GBRAIN_TOKEN = os.environ.get("GATEWAY_GBRAIN_TOKEN", "")
```

Confirm `import urllib.request` and `import json` are present at the top of `server.py` (add `import urllib.request` if missing).

- [ ] **Step 4: Add the upstream helper** — in `server.py` (module level, near other helpers). This is the ONLY place the MCP body is built, so read-only lives here:

```python
def _brain_query_upstream(question: str, limit: int) -> list:
    """Proxy a READ-ONLY gbrain query. Builds the MCP `query` call itself (never
    write/admin), POSTs to GATEWAY_GBRAIN_URL with the gateway-held token, returns
    the chunk array. Raises on any upstream/parse failure (caller maps to 502)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "query",
                       "arguments": {"query": question, "limit": limit, "expand": False}}}
    req = urllib.request.Request(
        GATEWAY_GBRAIN_URL, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", f"Bearer {GATEWAY_GBRAIN_TOKEN}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed tailnet URL
        raw = resp.read().decode("utf-8")
    payload = raw
    if "data:" in raw:
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("data:"):
                payload = s[len("data:"):].strip()
                break
    doc = json.loads(payload)
    parsed = json.loads(doc["result"]["content"][0]["text"])
    return parsed if isinstance(parsed, list) else []
```

- [ ] **Step 5: Add the request model + endpoint** — in `server.py`, near the other `@app.post` routes. Add the import `from starlette.concurrency import run_in_threadpool` at the top if absent, and (with the other Pydantic models) `from pydantic import BaseModel, Field` is already imported (the file defines request models like `MessagePostRequest`):

```python
class BrainQueryRequest(BaseModel):
    query: str
    limit: int = 8


@app.post("/brain/query")
async def brain_query(req: BrainQueryRequest,
                      authorization: Optional[str] = Header(None)) -> dict:
    auth = _authorize(authorization)                      # 401 on bad mesh token
    if not GATEWAY_GBRAIN_TOKEN:
        raise HTTPException(503, "brain proxy not configured")
    if not req.query.strip():
        raise HTTPException(400, "query is required")
    try:
        chunks = await run_in_threadpool(_brain_query_upstream, req.query, req.limit)
    except Exception as e:  # fail loud — never a swallowed empty result
        log.warning("brain proxy upstream error for peer=%s: %s", auth.peer, e)
        raise HTTPException(502, "brain upstream error")
    log.info("brain query peer=%s regime=%s limit=%d chunks=%d",
             auth.peer, auth.regime, req.limit, len(chunks))
    return {"chunks": chunks}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /home/ubuntu/swarph-cli && PYTHONPATH=src python3 -m pytest tests/test_gateway_brain.py -v`
Expected: PASS — all six tests green.

- [ ] **Step 7: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/gateway/server.py tests/test_gateway_brain.py
git commit -m "feat(gateway): POST /brain/query — mesh-token auth, read-only gbrain proxy

Reuses _authorize; builds the MCP query itself (read-only by construction);
proxies with one gateway-held token; 401/400/502/503; per-peer attribution."
```

---

### Task 2: `brain-ask` gateway client path

**Files:**
- Modify: `src/swarph_cli/commands/brain_ask.py` (add `_gateway_query`; branch in `run_brain_ask` ~`:161-170`)
- Test: `tests/test_brain_ask_command.py`

**Interfaces:**
- Consumes: `_peer_token_path(self_name)` (`:87`), `_self_name()` (`:91`), `_http_post(url, body, token, accept, timeout)` (`:112`).
- Produces: `_gateway_query(gw_base: str, peer_token: str, question: str, limit: int) -> list` — POSTs `<gw_base>/brain/query` with `{"query","limit"}` + `Bearer peer_token`, returns the `chunks` list. `run_brain_ask` uses it when `SWARPH_BRAIN_GATEWAY` is set.

- [ ] **Step 1: Write the failing tests** — add to `tests/test_brain_ask_command.py`:

```python
def test_gateway_query_posts_brain_query_with_peer_token(monkeypatch, tmp_path):
    from swarph_cli.commands import brain_ask
    captured = {}
    def fake_http_post(url, body, token, accept="application/json, text/event-stream", timeout=30):
        captured.update(url=url, body=body, token=token)
        import json as _j
        return _j.dumps({"chunks": [{"slug": "s1", "chunk_text": "x"}]})
    monkeypatch.setattr(brain_ask, "_http_post", fake_http_post)
    out = brain_ask._gateway_query("http://gw:8788", "peer_tok_123", "hello", 5)
    assert out == [{"slug": "s1", "chunk_text": "x"}]
    assert captured["url"] == "http://gw:8788/brain/query"
    assert captured["body"] == {"query": "hello", "limit": 5}
    assert captured["token"] == "peer_tok_123"


def test_run_brain_ask_uses_gateway_when_env_set(monkeypatch, tmp_path, capsys):
    from swarph_cli.commands import brain_ask
    # peer token on disk for _self_name()
    cfg = tmp_path / ".config" / "swarph"
    cfg.mkdir(parents=True)
    (cfg / "wlc.peer_token").write_text("peer_tok_xyz", encoding="utf-8")
    monkeypatch.setattr(brain_ask.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SWARPH_SELF", "wlc")
    monkeypatch.setenv("SWARPH_BRAIN_GATEWAY", "http://gw:8788")
    seen = {}
    def fake_gateway_query(gw, tok, q, n):
        seen.update(gw=gw, tok=tok, q=q)
        return [{"slug": "s1", "chunk_text": "hit"}]
    monkeypatch.setattr(brain_ask, "_gateway_query", fake_gateway_query)
    rc = brain_ask.run_brain_ask(["--no-synth", "what", "is", "up"])
    assert rc == 0
    assert seen["gw"] == "http://gw:8788" and seen["tok"] == "peer_tok_xyz"
    assert "s1" in capsys.readouterr().out


def test_run_brain_ask_direct_path_unchanged_when_gateway_unset(monkeypatch, capsys):
    from swarph_cli.commands import brain_ask
    monkeypatch.delenv("SWARPH_BRAIN_GATEWAY", raising=False)
    monkeypatch.setenv("GBRAIN_TOKEN", "gbrain_direct")
    called = {}
    monkeypatch.setattr(brain_ask, "_mcp_query",
                        lambda url, tok, q, n: called.setdefault("hit", (url, tok)) or [{"slug": "d"}])
    rc = brain_ask.run_brain_ask(["--no-synth", "hello"])
    assert rc == 0
    assert called["hit"][1] == "gbrain_direct"     # direct path used the gbrain token
    assert "d" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /home/ubuntu/swarph-cli && PYTHONPATH=src python3 -m pytest tests/test_brain_ask_command.py -k "gateway or direct_path" -v`
Expected: FAIL — `_gateway_query` undefined; `run_brain_ask` has no gateway branch.

- [ ] **Step 3: Add `_gateway_query`** — in `brain_ask.py`, after `_mcp_query` (`:127`):

```python
def _gateway_query(gw_base: str, peer_token: str, question: str, limit: int) -> list:
    """Query the brain via the mesh gateway's /brain/query proxy, authenticating
    with the cell's MESH peer token. The gateway holds the gbrain token; we never do."""
    url = gw_base.rstrip("/") + "/brain/query"
    raw = _http_post(url, {"query": question, "limit": limit}, peer_token,
                     accept="application/json")
    return json.loads(raw).get("chunks", [])
```

- [ ] **Step 4: Branch in `run_brain_ask`** — replace the retrieval block. Current (`:161-170`):

```python
    token = _resolve_token(args.token_file, _self_name())
    if not token:
        sys.stderr.write(
            "swarph brain-ask: no gbrain read token "
            "(set GBRAIN_TOKEN / SWARPH_BRAIN_TOKEN, pass --token-file, or place a "
            "mesh peer token at ~/.config/swarph/<self>.peer_token)\n")
        return 2

    try:
        chunks = _mcp_query(args.gateway, token, question, args.limit)
```

New:

```python
    gw = os.environ.get("SWARPH_BRAIN_GATEWAY")
    if gw:
        self_name = _self_name()
        try:
            peer_token = _peer_token_path(self_name).read_text(encoding="utf-8").strip()
        except OSError:
            peer_token = ""
        if not peer_token:
            sys.stderr.write(
                f"swarph brain-ask: SWARPH_BRAIN_GATEWAY set but no mesh peer token at "
                f"~/.config/swarph/{self_name}.peer_token\n")
            return 2
        try:
            chunks = _gateway_query(gw, peer_token, question, args.limit)
        except Exception as e:  # noqa: BLE001 — surface, don't swallow
            sys.stderr.write(f"swarph brain-ask: gateway brain query failed: {e}\n")
            return 1
    else:
        token = _resolve_token(args.token_file, _self_name())
        if not token:
            sys.stderr.write(
                "swarph brain-ask: no gbrain read token "
                "(set GBRAIN_TOKEN / SWARPH_BRAIN_TOKEN, pass --token-file, or place a "
                "mesh peer token at ~/.config/swarph/<self>.peer_token)\n")
            return 2
        try:
            chunks = _mcp_query(args.gateway, token, question, args.limit)
```

(Keep the rest of the `try` body — the `except`/synthesis/print — exactly as it is after this line; only the retrieval source changes.)

- [ ] **Step 5: Run to verify they pass + the file's existing tests still pass**

Run: `cd /home/ubuntu/swarph-cli && PYTHONPATH=src python3 -m pytest tests/test_brain_ask_command.py -v`
Expected: PASS — new gateway + compat tests pass, all pre-existing brain-ask tests still pass.

- [ ] **Step 6: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add src/swarph_cli/commands/brain_ask.py tests/test_brain_ask_command.py
git commit -m "feat(brain-ask): SWARPH_BRAIN_GATEWAY path — query the brain with the mesh peer token

When set, POSTs the gateway /brain/query proxy with the cell's peer token; the
direct :8792 + gbrain_ token path is unchanged when unset."
```

---

### Task 3: Version bump `0.26.0` + docs + full-suite gate

**Files:**
- Modify: `pyproject.toml`, `src/swarph_cli/__init__.py`, `src/swarph_cli/commands/brain_ask.py` (docstring env note)
- Test: `tests/test_brain_ask_command.py` (version-independent) + full suite

**Interfaces:** none (release bookkeeping + docs).

- [ ] **Step 1: Write the failing test** — add to `tests/test_brain_ask_command.py`:

```python
def test_version_is_0_26_0():
    import swarph_cli
    assert swarph_cli.__version__ == "0.26.0"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /home/ubuntu/swarph-cli && PYTHONPATH=src python3 -m pytest tests/test_brain_ask_command.py::test_version_is_0_26_0 -v`
Expected: FAIL — `assert '0.25.0' == '0.26.0'`.

- [ ] **Step 3: Bump both pins + document the env var**

`src/swarph_cli/__init__.py` (line with `__version__ = "0.25.0"`) → `__version__ = "0.26.0"`.
`pyproject.toml` (line `version = "0.25.0"`) → `version = "0.26.0"`.
In `brain_ask.py`, add to the module docstring's env section (near the `GBRAIN_TOKEN` note ~`:14`):

```
  SWARPH_BRAIN_GATEWAY  when set, query the brain via the mesh gateway's
                        /brain/query proxy using the cell's mesh peer token
                        (no per-cell gbrain_ token). Unset = direct :8792.
```

- [ ] **Step 4: Run the version test + the FULL suite**

Run: `cd /home/ubuntu/swarph-cli && PYTHONPATH=src python3 -m pytest tests/test_brain_ask_command.py::test_version_is_0_26_0 -v && PYTHONPATH=src python3 -m pytest -q`
Expected: PASS — version test passes; entire repo suite green (no regression).

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/swarph-cli
git add pyproject.toml src/swarph_cli/__init__.py src/swarph_cli/commands/brain_ask.py tests/test_brain_ask_command.py
git commit -m "chore(release): bump swarph-cli 0.25.0 -> 0.26.0 (gateway brain-proxy)"
```

---

## Self-Review

**1. Spec coverage:**
- `POST /brain/query` + reuse `_authorize` + read-only-by-construction → Task 1 (`_brain_query_upstream` builds `name=query` only; `test_upstream_helper_is_read_only_by_construction`). ✓
- `GATEWAY_GBRAIN_URL`/`GATEWAY_GBRAIN_TOKEN`, unset→503 → Task 1 (config + `test_brain_query_unconfigured_503`). ✓
- Errors 401/400/502/503 → Task 1 tests. ✓
- Client `SWARPH_BRAIN_GATEWAY` peer-token path + direct-`:8792` backward-compat → Task 2 (`_gateway_query`, branch, `test_run_brain_ask_direct_path_unchanged_when_gateway_unset`). ✓
- No new runtime dep (`urllib` + `run_in_threadpool`) → Task 1 Steps 3–5. ✓
- Fail-loud 502 → Task 1 endpoint + `test_brain_query_upstream_error_502`. ✓
- Version 0.26.0 both files + docs → Task 3. ✓
- Public PyPI / synthetic fixtures / no real tokens → all tests use `gbrain_hostheld` / `peer_tok_*` literals + mocks. ✓
- Rollout (publish/env/re-point/revoke) — out of scope, absent from tasks. ✓

**2. Placeholder scan:** No TBD/TODO; every code step shows full before/after. ✓

**3. Type consistency:** `_brain_query_upstream(question, limit) -> list` and `_gateway_query(gw_base, peer_token, question, limit) -> list` are used identically in impl + tests. Endpoint returns `{"chunks": list}`; client reads `.get("chunks", [])`. `BrainQueryRequest(query, limit=8)`. Env names consistent: `GATEWAY_GBRAIN_URL/TOKEN` (server), `SWARPH_BRAIN_GATEWAY` (client). ✓

**Note for the executor:** line anchors (`:62`, `:161-170`, etc.) are pre-change references; after edits later anchors shift — locate by the quoted code, not the raw number. Confirm `Header`, `HTTPException`, `Optional`, `BaseModel`, and `log` are already imported in `server.py` (they are — used by existing routes); add only `urllib.request` and `run_in_threadpool` if missing.
