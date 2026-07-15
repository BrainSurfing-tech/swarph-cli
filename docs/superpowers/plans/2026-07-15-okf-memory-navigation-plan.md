# OKF Memory-Navigation Tool (`swarph memory`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic OKF memory-navigation verb — `swarph memory get/list/links` — plus a `swarph_memory_navigate` MCP tool, the knowledge-hemisphere counterpart to `swarph codegraph`, so an agent can fetch an EXACT canonical memory fact (not fuzzy semantic recall) when it knows it needs one.

**Architecture:** A new `memory.py` command talks to the running gbrain over HTTP MCP (`:8792/mcp`, Bearer token) — the exact transport `brain_ask.py` already uses — but calls the deterministic `get_page` / `list_pages` MCP tools instead of semantic `query`. Link traversal (`memory links`) parses `[[wiki-links]]` out of a page body (gbrain's `graph`/`backlinks` are CLI-only, not exposed over MCP). A single `swarph_memory_navigate` MCP tool wraps the three ops for host discoverability, mirroring `swarph_codegraph_query`. No hook is touched — this is a deliberate, agent-invoked verb, not an ambient retrieval lane.

**Tech Stack:** Python 3, stdlib-only (`urllib`, `argparse`, `re`, `json`) — same public-package constraint as `brain_ask`/`codegraph`. Tests via `venv/bin/python -m pytest`.

## Global Constraints

- **Stdlib-only.** No new third-party dependency (swarph-cli is a public PyPI package). Reuse `urllib.request`, mirroring `brain_ask.py`.
- **DRY the transport.** Import and reuse `brain_ask`'s `_resolve_endpoint`, `_resolve_token`, `_self_name`, `_peer_token_path`, `_http_post` — do NOT duplicate endpoint/token resolution. Only the tools/call payload differs.
- **Token model identical to `brain-ask`/`mesh`:** endpoint resolved `GBRAIN_MCP_URL` > `SWARPH_BRAIN_MCP` > `http://127.0.0.1:8792/mcp`; token resolved `--token-file` > `GBRAIN_TOKEN` > `SWARPH_BRAIN_TOKEN` > mesh per-peer token (`~/.config/swarph/<self>.peer_token`).
- **Fail-safe, read-only.** Every path is read-only. Network/parse errors resolve to a clean empty result + a stderr note and a non-zero exit for the CLI; the MCP tool wrapper NEVER raises (returns `[]`/`{}`), mirroring `codegraph._codegraph_query`.
- **Inert-safe rollout.** Additive verb only. NO change to any hook, retrieval lane, or existing command behavior. Publishing to PyPI is commander-gated (out of scope for this plan — plan ends at green + PR).
- **`--tag` is more reliable than `--type`.** Per `/home/ubuntu/tools/gbrain-api-notes.md`, gbrain reclassifies its own page `type` (a `type: project` page returned as `type: "concept"`). Document this in help/docstrings; do not promise exact `--type` fidelity.
- **gbrain MCP tools available** (`/home/ubuntu/tools/gbrain-api-notes.md`): `get_page(slug, fuzzy, include_deleted)`, `list_pages(type, tag, limit, updated_after)`. There is NO `graph`/`backlinks` MCP tool — `links` must parse the page body.
- **Version bump discipline:** bumping `swarph_cli.__version__` requires updating BOTH `pyproject.toml` and `src/swarph_cli/__init__.py` AND the two pin tests (`tests/test_brain_ask_command.py`, `tests/test_watchdog.py`).
- **Routing guidance is evidence-based (#33 LOCOMO):** deterministic get/list/links is for CANONICAL/exact recall (known slug, all pages of a tag, a concept's neighbors); ambient semantic `brain-ask`/hook is for fuzzy recall. #33 finding to encode in docstrings: single-hop canonical lookup is where deterministic nav is strongest; multi-hop/relational recall is the weak spot that `links` graph-traversal directly targets.

---

## File Structure

- **Create** `src/swarph_cli/commands/memory.py` — the `swarph memory` verb (get/list/links), transport reused from `brain_ask`.
- **Create** `tests/test_memory_command.py` — unit tests (transport mocked via `monkeypatch` on the `_http_post` seam).
- **Modify** `src/swarph_cli/main.py` — register `"memory"` → `swarph_cli.commands.memory.run_memory` in the dispatch dict.
- **Modify** `src/swarph_cli/commands/mcp_server.py` — add `swarph_memory_navigate` tool + `_memory_navigate` wrapper.
- **Modify** `README.md` — add `### swarph memory` section + the AI-Router table (the deterministic-vs-ambient split).
- **Modify** `pyproject.toml`, `src/swarph_cli/__init__.py`, `tests/test_brain_ask_command.py`, `tests/test_watchdog.py` — version bump to `0.30.0`.

---

### Task 1: `swarph memory get <slug>` — deterministic page fetch

**Files:**
- Create: `src/swarph_cli/commands/memory.py`
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes (imported from `swarph_cli.commands.brain_ask`): `_resolve_endpoint() -> str`, `_resolve_token(token_file, self_name) -> Optional[str]`, `_self_name() -> str`, `_http_post(url, body, token, accept="application/json", timeout=...) -> str`.
- Produces: `run_memory(argv: list) -> int`; `_mcp_call(url, token, tool, arguments: dict) -> object` (generic tools/call → parsed content); `get_page(url, token, slug) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_command.py
import json
import pytest
from swarph_cli.commands import memory


def _fake_post(monkeypatch, captured, response_obj):
    """Stub brain_ask._http_post (the shared transport seam) to capture the
    outbound MCP request and return a canned MCP tools/call envelope."""
    def _post(url, body, token, accept="application/json", timeout=20):
        captured["url"] = url
        captured["body"] = body
        captured["token"] = token
        # gbrain MCP wraps tool output as {"result": {"content": [{"type":"text","text": <json-str>}]}}
        return json.dumps({"result": {"content": [{"type": "text", "text": json.dumps(response_obj)}]}})
    monkeypatch.setattr(memory.brain_ask, "_http_post", _post)


def test_get_page_calls_get_page_tool_and_returns_dict(monkeypatch):
    captured = {}
    page = {"slug": "project_gbrain_shipped", "type": "project", "content": "# gbrain\n$0 memory"}
    _fake_post(monkeypatch, captured, page)
    monkeypatch.setattr(memory.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(memory.brain_ask, "_resolve_token", lambda *a, **k: "tok")

    result = memory.get_page("http://x/mcp", "tok", "project_gbrain_shipped")

    assert result["slug"] == "project_gbrain_shipped"
    assert captured["body"]["method"] == "tools/call"
    assert captured["body"]["params"]["name"] == "get_page"
    assert captured["body"]["params"]["arguments"] == {"slug": "project_gbrain_shipped"}


def test_mcp_call_handles_sse_framing(monkeypatch):
    """gbrain replies over SSE (data:-prefixed) — _mcp_call must unwrap it."""
    page = {"slug": "s", "content": "x"}
    inner = json.dumps({"result": {"content": [{"type": "text", "text": json.dumps(page)}]}})
    sse = f"event: message\ndata: {inner}\n\n"
    monkeypatch.setattr(memory.brain_ask, "_http_post", lambda *a, **k: sse)
    assert memory._mcp_call("http://x/mcp", "tok", "get_page", {"slug": "s"}) == page
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_get_page_calls_get_page_tool_and_returns_dict -v`
Expected: FAIL — `ModuleNotFoundError: swarph_cli.commands.memory` (module not created yet).

- [ ] **Step 3: Write minimal implementation**

```python
# src/swarph_cli/commands/memory.py
"""``swarph memory`` — DETERMINISTIC OKF memory navigation over gbrain.

The knowledge-hemisphere counterpart to ``swarph codegraph``. When an agent
wants an EXACT canonical memory fact — a known page, every page of a tag, or
a concept's neighbours — it invokes this verb. Fuzzy/semantic recall stays
with ``swarph brain-ask`` and the per-prompt retrieval hook; the intent to
consult a *specific* fact lives with the caller (see README §The AI Router).

Routing (evidence from the #33 LOCOMO benchmark): deterministic get/list/links
is strongest for single-hop CANONICAL lookup ("the precise page for X", "all
`reference`-tagged pages"); relational/multi-hop recall is the weak spot that
``links`` graph-traversal targets. Reach for semantic ``brain-ask`` when you
don't yet know which page you need.

Transport mirrors ``brain-ask``: HTTP MCP to gbrain (:8792/mcp, Bearer). Calls
the deterministic ``get_page`` / ``list_pages`` MCP tools (NOT semantic
``query``). Read-only; stdlib-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from swarph_cli.commands import brain_ask


def _strip_sse(raw: str) -> str:
    """gbrain MCP may reply as SSE (``data:``-prefixed lines) OR plain JSON —
    mirror brain_ask._parse_query_response: return the JSON payload either way."""
    if "data:" in raw:
        for line in raw.splitlines():
            s = line.strip()
            if s.startswith("data:"):
                return s[len("data:"):].strip()
    return raw


def _mcp_call(url: str, token: str, tool: str, arguments: dict):
    """Generic gbrain MCP tools/call → parsed tool output. Reuses brain_ask's
    HTTP seam so endpoint/token behaviour stays identical across verbs. Handles
    gbrain's SSE framing + the ``result.content[].text`` = JSON-string envelope."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    raw = brain_ask._http_post(url, body, token)  # keep _http_post's default Accept
    # ("application/json, text/event-stream") — streamable-HTTP MCP servers 406 a
    # JSON-only Accept; brain_ask's proven calls never narrow it.
    envelope = json.loads(_strip_sse(raw))
    content = (envelope.get("result") or {}).get("content") or []
    for part in content:
        if part.get("type") == "text":
            try:
                return json.loads(part["text"])
            except (json.JSONDecodeError, TypeError):
                return part["text"]
    return envelope.get("result")


def get_page(url: str, token: str, slug: str) -> dict:
    """Fetch one page by exact slug via the ``get_page`` MCP tool."""
    out = _mcp_call(url, token, "get_page", {"slug": slug})
    return out if isinstance(out, dict) else {}


def run_memory(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="swarph memory",
        description="Deterministic OKF memory navigation over gbrain "
                    "(get/list/links). Fuzzy recall = `swarph brain-ask`.")
    parser.add_argument("--token-file", default=None,
                        help="path to a gbrain read token (else GBRAIN_TOKEN / "
                             "SWARPH_BRAIN_TOKEN / mesh peer token)")
    sub = parser.add_subparsers(dest="subcommand")

    p_get = sub.add_parser("get", help="read one page by exact slug")
    p_get.add_argument("slug")
    p_get.add_argument("--json", action="store_true", help="raw page JSON")

    args = parser.parse_args(argv)
    url = brain_ask._resolve_endpoint()
    token = brain_ask._resolve_token(args.token_file, brain_ask._self_name())

    if args.subcommand == "get":
        try:
            page = get_page(url, token, args.slug)
        except Exception as e:  # fail-safe: never traceback at the CLI
            print(f"swarph memory get: gbrain unreachable ({e})", file=sys.stderr)
            return 1
        if not page:
            print(f"swarph memory get: no page {args.slug!r}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(page, indent=2))
        else:
            print(page.get("content", "") or json.dumps(page, indent=2))
        return 0

    parser.print_help()
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_get_page_calls_get_page_tool_and_returns_dict -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/memory.py tests/test_memory_command.py
git commit -m "feat(memory): swarph memory get — deterministic page fetch over gbrain MCP"
```

---

### Task 2: `swarph memory list [--type T] [--tag T] [-n N]` — deterministic filter

**Files:**
- Modify: `src/swarph_cli/commands/memory.py`
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes: `_mcp_call` (Task 1).
- Produces: `list_pages(url, token, type_=None, tag=None, limit=50) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
def test_list_pages_calls_list_pages_tool_with_filters(monkeypatch):
    captured = {}
    pages = [{"slug": "reference_swarph_mesh", "type": "reference"},
             {"slug": "reference_okf_google", "type": "reference"}]
    _fake_post(monkeypatch, captured, pages)

    result = memory.list_pages("http://x/mcp", "tok", type_="reference", tag="auth", limit=25)

    assert [p["slug"] for p in result] == ["reference_swarph_mesh", "reference_okf_google"]
    assert captured["body"]["params"]["name"] == "list_pages"
    assert captured["body"]["params"]["arguments"] == {"type": "reference", "tag": "auth", "limit": 25}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_list_pages_calls_list_pages_tool_with_filters -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'list_pages'`.

- [ ] **Step 3: Write minimal implementation**

Add to `memory.py` (after `get_page`):

```python
def list_pages(url: str, token: str, type_: str | None = None,
               tag: str | None = None, limit: int = 50) -> list:
    """List pages filtered by metadata (type/tag) via ``list_pages`` — a
    DETERMINISTIC filter, not semantic search. NOTE: gbrain reclassifies its
    own page `type`, so `tag` is the reliable scope (see gbrain-api-notes)."""
    args: dict = {"limit": limit}
    if type_:
        args["type"] = type_
    if tag:
        args["tag"] = tag
    out = _mcp_call(url, token, "list_pages", args)
    return out if isinstance(out, list) else (out.get("pages", []) if isinstance(out, dict) else [])
```

Add a `list` subparser inside `run_memory` (after the `get` subparser block):

```python
    p_list = sub.add_parser("list", help="filter pages by type/tag (deterministic, not semantic)")
    p_list.add_argument("--type", dest="type_", default=None,
                        help="page type filter (unreliable — gbrain reclassifies type; prefer --tag)")
    p_list.add_argument("--tag", default=None, help="tag filter (the reliable scope)")
    p_list.add_argument("-n", "--limit", type=int, default=50)
    p_list.add_argument("--json", action="store_true")
```

Add the `list` branch inside `run_memory` (before `parser.print_help()`):

```python
    if args.subcommand == "list":
        try:
            pages = list_pages(url, token, args.type_, args.tag, args.limit)
        except Exception as e:
            print(f"swarph memory list: gbrain unreachable ({e})", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(pages, indent=2))
        else:
            for p in pages:
                print(f"{p.get('slug','?'):40} {p.get('type','')}")
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_list_pages_calls_list_pages_tool_with_filters -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/memory.py tests/test_memory_command.py
git commit -m "feat(memory): swarph memory list — deterministic type/tag filter"
```

---

### Task 3: `swarph memory links <slug>` — `[[wiki-link]]` traversal

**Files:**
- Modify: `src/swarph_cli/commands/memory.py`
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes: `get_page` (Task 1).
- Produces: `parse_links(content: str) -> list[str]`; `links(url, token, slug) -> list[str]`.

- [ ] **Step 1: Write the failing test**

```python
def test_parse_links_extracts_wiki_links_dedup_ordered():
    body = ("See [[project_gbrain_shipped]] and [[reference_okf_google]].\n"
            "Also [[project_gbrain_shipped]] again and a [markdown](path) link.")
    assert memory.parse_links(body) == ["project_gbrain_shipped", "reference_okf_google"]


def test_links_reads_page_then_extracts(monkeypatch):
    captured = {}
    page = {"slug": "a", "content": "links to [[b]] and [[c]]"}
    _fake_post(monkeypatch, captured, page)
    assert memory.links("http://x/mcp", "tok", "a") == ["b", "c"]
    assert captured["body"]["params"]["name"] == "get_page"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -k "parse_links or links_reads" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'parse_links'`.

- [ ] **Step 3: Write minimal implementation**

Add to `memory.py`:

```python
_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")


def parse_links(content: str) -> list:
    """Extract ``[[slug]]`` wiki-links from a page body (forward links),
    order-preserving de-dupe. Ignores standard ``[text](path)`` markdown links.
    (gbrain's graph/backlinks are CLI-only, not over MCP, so v1 traverses the
    body — the OKF memory format links concepts with [[name]].)"""
    slugs = [m.group(1).strip() for m in _WIKILINK.finditer(content or "")]
    return list(dict.fromkeys(slugs))


def links(url: str, token: str, slug: str) -> list:
    """Forward [[links]] out of a page (deterministic graph navigation)."""
    page = get_page(url, token, slug)
    return parse_links(page.get("content", "") if isinstance(page, dict) else "")
```

Add a `links` subparser inside `run_memory`:

```python
    p_links = sub.add_parser("links", help="forward [[wiki-links]] out of a page")
    p_links.add_argument("slug")
    p_links.add_argument("--json", action="store_true")
```

Add the `links` branch:

```python
    if args.subcommand == "links":
        try:
            out = links(url, token, args.slug)
        except Exception as e:
            print(f"swarph memory links: gbrain unreachable ({e})", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            for s in out:
                print(s)
        return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -k "parse_links or links_reads" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/memory.py tests/test_memory_command.py
git commit -m "feat(memory): swarph memory links — [[wiki-link]] graph traversal"
```

---

### Task 4: Register `memory` in the CLI dispatch + no-subcommand help

**Files:**
- Modify: `src/swarph_cli/main.py`
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes: `run_memory` (Task 1).
- Produces: `swarph memory ...` reachable through the top-level CLI.

- [ ] **Step 1: Write the failing test**

```python
def test_memory_registered_in_dispatch():
    from swarph_cli import main as m
    # the dispatch table maps the verb to the dotted run_ path
    table = getattr(m, "COMMANDS", None) or getattr(m, "_COMMANDS", None) or getattr(m, "DISPATCH", None)
    assert table is not None, "locate the dispatch dict in main.py and update this test's accessor"
    assert table["memory"] == "swarph_cli.commands.memory.run_memory"
```

Note to implementer: open `src/swarph_cli/main.py`, find the dispatch dict (the one containing `"board": "swarph_cli.commands.board.run_board"`), and set the test's accessor to that dict's actual name.

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_memory_registered_in_dispatch -v`
Expected: FAIL — `KeyError: 'memory'`.

- [ ] **Step 3: Write minimal implementation**

In `src/swarph_cli/main.py`, add to the dispatch dict (next to the `"board"` entry):

```python
    "memory": "swarph_cli.commands.memory.run_memory",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_memory_registered_in_dispatch -v`
Expected: PASS.

Then smoke the wiring end-to-end (no gbrain needed — just that the verb resolves and prints help):

Run: `venv/bin/python -m swarph_cli memory --help`
Expected: prints the `swarph memory` help with get/list/links subcommands (exit 0).

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/main.py tests/test_memory_command.py
git commit -m "feat(memory): register `swarph memory` verb in CLI dispatch"
```

---

### Task 5: `swarph_memory_navigate` MCP tool

**Files:**
- Modify: `src/swarph_cli/commands/mcp_server.py`
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes: `memory.get_page`, `memory.list_pages`, `memory.links` (Tasks 1-3); `brain_ask._resolve_endpoint`, `brain_ask._resolve_token`, `brain_ask._self_name`.
- Produces: `_memory_navigate(op, slug=None, type=None, tag=None, limit=20) -> object`; a registered `swarph_memory_navigate` FastMCP tool.

- [ ] **Step 1: Write the failing test**

```python
def test_memory_navigate_dispatches_and_is_failsafe(monkeypatch):
    from swarph_cli.commands import mcp_server
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr(mcp_server.brain_ask, "_self_name", lambda: "lab-ovh")
    monkeypatch.setattr(mcp_server.memory, "get_page", lambda u, t, s: {"slug": s, "content": "hi"})

    assert mcp_server._memory_navigate("get", slug="foo")["slug"] == "foo"

    # fail-safe: an unexpected op or an exploding backend NEVER raises
    def boom(*a, **k):
        raise RuntimeError("gbrain down")
    monkeypatch.setattr(mcp_server.memory, "list_pages", boom)
    assert mcp_server._memory_navigate("list", tag="auth") == []
    assert mcp_server._memory_navigate("bogus") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_memory_navigate_dispatches_and_is_failsafe -v`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_memory_navigate'`.

- [ ] **Step 3: Write minimal implementation**

In `src/swarph_cli/commands/mcp_server.py`, add the import near the existing `from swarph_cli.commands import codegraph`:

```python
from swarph_cli.commands import brain_ask, memory
```

Add the wrapper function (near `_codegraph_query`):

```python
def _memory_navigate(op: str, slug: str | None = None, type: str | None = None,
                     tag: str | None = None, limit: int = 20):
    """Deterministic OKF memory navigation — the knowledge-hemisphere twin of
    swarph_codegraph_query. Ops: 'get' (exact page by slug), 'list' (type/tag
    filter — deterministic, NOT semantic), 'links' (a page's [[wiki-links]]).

    Use this when you need an EXACT/canonical memory fact you can name; use the
    ambient semantic recall (the retrieval hook / brain-ask) when you don't yet
    know which page you need. (#33 LOCOMO: deterministic nav is strongest for
    single-hop canonical lookup; 'links' targets the relational/multi-hop case.)

    Fail-safe: any backend/parse error resolves to [] or {} — never raises."""
    try:
        url = brain_ask._resolve_endpoint()
        token = brain_ask._resolve_token(None, brain_ask._self_name())
        if op == "get" and slug:
            return memory.get_page(url, token, slug)
        if op == "list":
            return memory.list_pages(url, token, type, tag, limit)
        if op == "links" and slug:
            return memory.links(url, token, slug)
        return []
    except Exception:
        return []
```

Register the tool alongside the existing `swarph_codegraph_query` registration (inside the same builder that does `def swarph_codegraph_query(...)`):

```python
    @mcp.tool()
    def swarph_memory_navigate(op: str, slug: str = "", tag: str = "",
                               type: str = "", limit: int = 20):
        """Deterministic OKF memory navigation over gbrain: op='get'|'list'|'links'.
        Exact canonical recall (a named page, a tag's pages, a concept's links) —
        the counterpart to swarph_codegraph_query for the knowledge hemisphere.
        For fuzzy recall use semantic search instead."""
        return _memory_navigate(op, slug=slug or None, type=type or None,
                                tag=tag or None, limit=limit)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_memory_navigate_dispatches_and_is_failsafe -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/mcp_server.py tests/test_memory_command.py
git commit -m "feat(memory): swarph_memory_navigate MCP tool (deterministic OKF nav)"
```

---

### Task 6: README §memory + The AI Router doc + version bump to 0.30.0

**Files:**
- Modify: `README.md`
- Modify: `pyproject.toml`, `src/swarph_cli/__init__.py`
- Modify: `tests/test_brain_ask_command.py`, `tests/test_watchdog.py` (version pins)

**Interfaces:** none (docs + version constants).

- [ ] **Step 1: Update the version pin tests first (write the failing assertion)**

Find the pin assertions and bump them to `0.30.0`. In `tests/test_brain_ask_command.py` and `tests/test_watchdog.py`, locate the test that asserts the version (grep: `grep -rn "0.29.2" tests/`) and change `"0.29.2"` → `"0.30.0"`.

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/python -m pytest tests/test_brain_ask_command.py tests/test_watchdog.py -k version -v`
Expected: FAIL — the code still reports `0.29.2`.

- [ ] **Step 3: Bump the version constants**

In `pyproject.toml`: `version = "0.30.0"`.
In `src/swarph_cli/__init__.py`: `__version__ = "0.30.0"`.

- [ ] **Step 4: Run to verify they pass**

Run: `venv/bin/python -m pytest tests/test_brain_ask_command.py tests/test_watchdog.py -k version -v`
Expected: PASS.

- [ ] **Step 5: Add the README section**

In `README.md`, after the `### swarph brain-ask` section, add:

````markdown
### `swarph memory` (v0.30.0)

**Deterministic** OKF memory navigation over gbrain — the knowledge-hemisphere twin of `swarph codegraph`. When you want an EXACT canonical fact you can name, you invoke it; fuzzy recall stays with `swarph brain-ask`.

```
swarph memory get <slug>                 # read one page by exact slug
swarph memory list [--tag T] [--type T]  # filter pages (deterministic — --tag is the reliable scope)
swarph memory links <slug>               # a concept's forward [[wiki-links]]
```

Same token model as `swarph brain-ask` (`GBRAIN_MCP_URL`/`SWARPH_BRAIN_MCP` endpoint; `--token-file` > `GBRAIN_TOKEN` > `SWARPH_BRAIN_TOKEN` > mesh peer token). Add `--json` for the raw payload. Read-only. Also exposed to any MCP host as the `swarph_memory_navigate` tool.

> Note: gbrain reclassifies its own page `type`, so `--tag` is more reliable than `--type` for scoping.

#### The AI Router (why a tool, not a hook)

The modern memory stack routes a request between **ambient semantic recall** (wide/fuzzy) and **deterministic canonical lookup** (exact). swarph's router is not a classifier box — it's the agent choosing, because intent lives with the caller:

| path | when | swarph surface |
|---|---|---|
| **ambient / semantic** | "surface anything relevant" (you don't know the page) | the per-prompt retrieval hook + `swarph brain-ask` |
| **deterministic / canonical** — code | "where is X defined / what calls it" | `swarph codegraph` / `swarph_codegraph_query` |
| **deterministic / canonical** — knowledge | "the exact page for X / all `auth`-tagged / X's neighbours" | **`swarph memory` / `swarph_memory_navigate`** |

Evidence (#33 LOCOMO benchmark): deterministic navigation is strongest for single-hop canonical lookup; relational/multi-hop recall is the weak spot `memory links` graph-traversal targets. Reach for semantic recall when you can't name the page yet.
````

- [ ] **Step 6: Run the FULL suite (no regressions)**

Run: `venv/bin/python -m pytest -q`
Expected: all pass, including the new `tests/test_memory_command.py` and the bumped pins.

- [ ] **Step 7: Commit**

```bash
git add README.md pyproject.toml src/swarph_cli/__init__.py tests/test_brain_ask_command.py tests/test_watchdog.py
git commit -m "docs(memory): README §memory + AI-Router doc; bump 0.30.0"
```

---

## Self-Review

**1. Spec coverage** (`2026-07-14-okf-rag-router-design.md`):
- `swarph memory get <slug>` → Task 1 ✓
- `swarph memory list --type/--tag` → Task 2 ✓ (with the type-reliability caveat surfaced)
- `swarph memory links <slug>` (`[[links]]` traversal) → Task 3 ✓ (body-parse, since gbrain graph is CLI-only — a faithful realization of the spec's intent within the available MCP surface)
- `swarph_memory_navigate` MCP tool → Task 5 ✓
- README §memory + name-the-router table → Task 6 ✓
- "Ship inert-safe (additive verb; no hook touch)" → no hook file is in any task's file list ✓
- "build ranking/scope AFTER #33's number" → #33 routing guidance encoded in docstrings + README (Tasks 1, 5, 6) ✓
- Deferred in spec (temporal hemisphere / reserved-name index synthesis) → correctly OUT of scope, not planned ✓

**2. Placeholder scan:** no TBD/TODO; every code step has complete code. Task 4's dispatch-dict accessor is the one spot requiring the implementer to read `main.py` for the dict's actual name — called out explicitly with the grep to run, not left vague.

**3. Type consistency:** `get_page → dict`, `list_pages → list`, `links → list[str]`, `parse_links → list[str]`, `_mcp_call → object`, `_memory_navigate → object`, `run_memory → int`. The MCP envelope shape (`result.content[].text` = JSON string) is used identically in Task 1's `_mcp_call` and the test stub `_fake_post`. `brain_ask._http_post` signature `(url, body, token, accept=, timeout=)` matches the stub and the real call.
