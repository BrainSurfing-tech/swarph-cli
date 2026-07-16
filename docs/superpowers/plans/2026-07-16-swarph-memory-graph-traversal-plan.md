# swarph memory graph traversal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `swarph memory links` file-native backlinks and multi-hop directional graph traversal over the OKF knowledge corpus, deriving edges from page bodies via the shared `okf_links` parser.

**Architecture:** gbrain stays the page store (`get_page`/`list_pages` over the existing `_mcp_call` MCP transport); the graph is computed client-side from page bodies. Forward traversal BFS-walks only reachable pages; reverse (`backlinks`/`in`) builds a reverse index with one full-corpus scan. No gbrain change, no brain-data mutation.

**Tech Stack:** Python 3, stdlib only, `argparse`; tests via `venv/bin/python -m pytest`; `unittest`/pytest with `_mcp_call` stubbed (no network).

## Global Constraints

- stdlib-only; public PyPI package; **no new dependencies**.
- Additive verb-flag change; **no hook touch**; inert-safe.
- Default `swarph memory links <slug>` (no new flags) output is **byte-for-byte unchanged** (depth=1, direction=out).
- Fail-safe: CLI paths print to stderr + return non-zero, **never traceback**; MCP ops swallow everything to `[]`, **never raise**.
- `DEPTH_CAP = 10` (mirrors gbrain's `TRAVERSE_DEPTH_CAP`); `--depth` clamped to `[1, 10]`.
- `--backlinks` is sugar for `--direction in`; combining it with an explicit `--direction` → **exit 2**.
- `--json` emits OKF **edge** records (shape change from flat slug-array — approved).
- Version bump `0.31.0 → 0.32.0` (pyproject.toml + `__init__.py` + both version-pin tests).
- Publish to PyPI is **commander-gated**; plan ends at **green + PR** (new public surface → PR left for review, NOT auto-merged under standing green-auth).
- Stage only the named files; **never `git add -A`**; never stage `.codegraph/` or `docs/superpowers/plans/2026-06-12-*`.

---

## File Structure

- `src/swarph_cli/commands/memory.py` — MODIFY. Retire the local `_WIKILINK`/`parse_links`; add the file-native traversal engine (`_all_page_slugs`, `_forward_targets`, `_reverse_index`, `traverse`, `backlinks`, `_as_okf_edge`, `DEPTH_CAP`, `_clamp_depth`); extend the `links` subcommand with flags + OKF `--json`.
- `src/swarph_cli/commands/mcp_server.py` — MODIFY. Extend `_memory_navigate` + the `swarph_memory_navigate` `@mcp.tool` wrapper with `backlinks`/`traverse` ops.
- `tests/test_memory_command.py` — MODIFY. New engine + CLI tests; update any existing `links --json` shape assertion.
- `tests/test_okf_links.py` — reference only (parser is already tested).
- `README.md` — MODIFY. §memory flags + O(N) corpus-scan note.
- `pyproject.toml`, `src/swarph_cli/__init__.py`, `tests/test_brain_ask_command.py`, `tests/test_watchdog.py` — MODIFY. Version bump.

---

### Task 1: Migrate memory link parsing to the shared okf_links parser

Retire memory.py's weaker wiki-only regex; route all link parsing through `okf_links.parse_okf_links`. Keep `parse_links` as a back-compat alias.

**Files:**
- Modify: `src/swarph_cli/commands/memory.py` (imports; delete `_WIKILINK` + `parse_links` body; add alias; `links()` uses shared parser)
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes: `okf_links.parse_okf_links(text: str) -> list[str]` (already shipped in 0.31.0).
- Produces: `memory.parse_links` remains a callable `(str) -> list[str]` (now `= parse_okf_links`); `memory.links(url, token, slug) -> list[str]` unchanged signature, now parses via the shared grammar (resolves `[[slug|alias]]`→`slug`, `[[slug#h]]`→`slug`, `![[embed]]`, `[text](path.md)`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_memory_command.py`:

```python
def test_parse_links_uses_shared_okf_grammar():
    from swarph_cli.commands import memory, okf_links
    # memory.parse_links must now BE the shared parser (single grammar, no drift)
    assert memory.parse_links is okf_links.parse_okf_links
    body = "see [[project_x|the X project]] and [[ref_y#section]] and ![[embed_z]] and [docs](guide.md)"
    assert memory.parse_links(body) == ["project_x", "ref_y", "embed_z", "guide.md"]


def test_links_resolves_alias_and_heading_via_shared_parser(monkeypatch):
    from swarph_cli.commands import memory
    page = {"slug": "hub", "content": "[[a|alias]] [[b#h]] ![[c]]"}
    inner = __import__("json").dumps({"result": {"content": [{"type": "text", "text": __import__("json").dumps(page)}]}})
    monkeypatch.setattr(memory.brain_ask, "_http_post", lambda *a, **k: inner)
    assert memory.links("http://x/mcp", "tok", "hub") == ["a", "b", "c"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_memory_command.py::test_parse_links_uses_shared_okf_grammar tests/test_memory_command.py::test_links_resolves_alias_and_heading_via_shared_parser -v`
Expected: FAIL — `test_parse_links_uses_shared_okf_grammar` fails on `is` identity (current `parse_links` is a distinct function); the alias/heading case fails because the old `_WIKILINK` regex does not strip `#heading` and does not match `![[embed]]` or markdown `.md` links.

- [ ] **Step 3: Write minimal implementation**

In `src/swarph_cli/commands/memory.py`:

Update the import block (near line 26) to add `okf_links`:

```python
from swarph_cli.commands import brain_ask, okf_links
```

Delete the `_WIKILINK` compiled pattern (line ~80) and the whole `parse_links` function (lines ~83-89). Replace with a back-compat alias placed after the imports:

```python
# The one true OKF link grammar lives in okf_links (pinned, tested). memory
# used to carry a weaker wiki-only copy; that duplication is retired — this
# alias keeps `memory.parse_links` importable for any existing caller.
parse_links = okf_links.parse_okf_links
```

Update `links()` to use the shared parser (line ~92-95):

```python
def links(url: str, token: str, slug: str) -> list:
    """Forward [[links]] out of a page (deterministic single-hop navigation),
    via the shared OKF grammar (okf_links)."""
    page = get_page(url, token, slug)
    content = page.get("content", "") if isinstance(page, dict) else ""
    return okf_links.parse_okf_links(content)
```

Remove the now-unused `import re` at the top **only if** no other code in the file uses `re` (grep first: `grep -n "re\." src/swarph_cli/commands/memory.py`). If `re` is still used elsewhere, leave the import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -v`
Expected: PASS (new tests green; existing memory tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/memory.py tests/test_memory_command.py
git commit -m "refactor: swarph memory parses links via shared okf_links grammar (#42)"
```

---

### Task 2: File-native traversal engine

Add the graph engine to `memory.py`: corpus enumeration, forward targets, reverse index, cycle-safe depth-bounded BFS, backlinks sugar, and the OKF edge record.

**Files:**
- Modify: `src/swarph_cli/commands/memory.py` (add engine functions after `links()`)
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes: `get_page`, `list_pages`, `okf_links.parse_okf_links` (from Task 1).
- Produces:
  - `DEPTH_CAP = 10`
  - `_clamp_depth(depth) -> int` (into `[1, 10]`; non-int → `1`)
  - `_all_page_slugs(url, token) -> list[str]`
  - `_forward_targets(url, token, slug) -> list[str]`
  - `_reverse_index(url, token) -> dict[str, list[str]]`
  - `traverse(url, token, slug, depth=1, direction="out") -> list[tuple[str, str, int, str]]` — ordered `(from, to, hop, edge_direction)`; `edge_direction ∈ {"out","in"}`; `"both"` = out-pass then in-pass; cycle-safe; depth-clamped.
  - `backlinks(url, token, slug) -> list[str]` — single-hop incoming, order-preserving de-dupe.
  - `_as_okf_edge(frm, to, direction, hop) -> dict`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory_command.py`. This helper stubs the corpus so no network is touched — `_mcp_call` is intercepted by faking `get_page`/`list_pages` directly:

```python
def _fake_corpus(monkeypatch, pages):
    """pages: {slug: body_str}. Stub memory.get_page/list_pages so the engine
    reads this in-memory graph instead of gbrain. No network."""
    from swarph_cli.commands import memory

    def _get_page(url, token, slug):
        if slug not in pages:
            raise RuntimeError(f"no page {slug}")   # simulate a 404/unreadable page
        return {"slug": slug, "content": pages[slug]}

    def _list_pages(url, token, type_=None, tag=None, limit=50):
        return [{"slug": s, "type": "note"} for s in pages]

    monkeypatch.setattr(memory, "get_page", _get_page)
    monkeypatch.setattr(memory, "list_pages", _list_pages)
    return memory


def test_traverse_out_depth1_equals_forward_links(monkeypatch):
    m = _fake_corpus(monkeypatch, {"a": "[[b]] [[c]]", "b": "", "c": ""})
    edges = m.traverse("u", "t", "a", depth=1, direction="out")
    assert edges == [("a", "b", 1, "out"), ("a", "c", 1, "out")]


def test_traverse_out_depth2_follows_frontier(monkeypatch):
    m = _fake_corpus(monkeypatch, {"a": "[[b]]", "b": "[[c]]", "c": ""})
    edges = m.traverse("u", "t", "a", depth=2, direction="out")
    assert ("a", "b", 1, "out") in edges
    assert ("b", "c", 2, "out") in edges


def test_traverse_is_cycle_safe(monkeypatch):
    m = _fake_corpus(monkeypatch, {"a": "[[b]]", "b": "[[a]]"})
    # a<->b cycle must terminate; each node visited once as a frontier
    edges = m.traverse("u", "t", "a", depth=10, direction="out")
    assert ("a", "b", 1, "out") in edges
    assert ("b", "a", 2, "out") in edges
    # 'a' is not re-expanded after being visited, so no hop-3 edge out of 'a'
    assert all(hop <= 2 for (_, _, hop, _) in edges)


def test_traverse_depth_clamped_to_cap(monkeypatch):
    m = _fake_corpus(monkeypatch, {"a": ""})
    assert m.DEPTH_CAP == 10
    # depth far over the cap must not raise; empty graph → no edges
    assert m.traverse("u", "t", "a", depth=9999, direction="out") == []
    assert m._clamp_depth(9999) == 10
    assert m._clamp_depth(0) == 1
    assert m._clamp_depth("x") == 1


def test_reverse_index_and_backlinks_skip_unreadable(monkeypatch):
    # 'x' and 'y' both link to 't'; 'z' is listed but unreadable (get_page raises)
    m = _fake_corpus(monkeypatch, {"t": "", "x": "[[t]]", "y": "[[t]]"})
    # inject a listed-but-missing page to prove the scan skips, not aborts
    orig = m.list_pages
    monkeypatch.setattr(m, "list_pages",
                        lambda *a, **k: orig(*a, **k) + [{"slug": "z", "type": "note"}])
    assert sorted(m.backlinks("u", "t", "t")) == ["x", "y"]


def test_traverse_both_is_out_then_in(monkeypatch):
    m = _fake_corpus(monkeypatch, {"hub": "[[out1]]", "out1": "", "src": "[[hub]]"})
    edges = m.traverse("u", "t", "hub", depth=1, direction="both")
    assert ("hub", "out1", 1, "out") in edges
    assert ("hub", "src", 1, "in") in edges
    # ordering: all out edges precede all in edges
    dirs = [d for (_, _, _, d) in edges]
    assert dirs == sorted(dirs, key=lambda d: 0 if d == "out" else 1)


def test_as_okf_edge_schema():
    from swarph_cli.commands import memory
    assert memory._as_okf_edge("a", "b", "out", 2) == {
        "type": "edge", "hemisphere": "knowledge", "from": "a", "to": "b",
        "rel": "links", "direction": "out", "hop": 2,
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -k "traverse or reverse_index or backlinks or as_okf_edge or clamp" -v`
Expected: FAIL with `AttributeError: module 'swarph_cli.commands.memory' has no attribute 'traverse'` (and siblings).

- [ ] **Step 3: Write minimal implementation**

Append to `src/swarph_cli/commands/memory.py` (after `links()`):

```python
DEPTH_CAP = 10  # mirrors gbrain's TRAVERSE_DEPTH_CAP — bounds a pathological walk


def _clamp_depth(depth) -> int:
    """Coerce a requested depth into [1, DEPTH_CAP]; non-ints resolve to 1."""
    try:
        d = int(depth)
    except (TypeError, ValueError):
        return 1
    return max(1, min(d, DEPTH_CAP))


def _all_page_slugs(url: str, token: str) -> list:
    """Every page slug in the brain (one list_pages call, high limit)."""
    pages = list_pages(url, token, limit=10000)
    return [p.get("slug") for p in pages
            if isinstance(p, dict) and p.get("slug")]


def _forward_targets(url: str, token: str, slug: str) -> list:
    """The [[links]] out of one page's body (shared OKF grammar)."""
    page = get_page(url, token, slug)
    content = page.get("content", "") if isinstance(page, dict) else ""
    return okf_links.parse_okf_links(content)


def _reverse_index(url: str, token: str) -> dict:
    """Map each target slug -> pages whose body links to it. One full-corpus
    scan; a page that fails to fetch/parse is SKIPPED (never aborts the scan)."""
    rev: dict = {}
    for src in _all_page_slugs(url, token):
        try:
            targets = _forward_targets(url, token, src)
        except Exception:
            continue  # unreadable/missing page — skip, keep scanning
        for tgt in targets:
            bucket = rev.setdefault(tgt, [])
            if src not in bucket:
                bucket.append(src)
    return rev


def traverse(url: str, token: str, slug: str, depth: int = 1,
             direction: str = "out") -> list:
    """File-native BFS over the OKF link graph. Returns ordered
    (from, to, hop, edge_direction) tuples. direction: 'out' follows forward
    [[links]]; 'in' follows the reverse index; 'both' = out-pass then in-pass.
    Cycle-safe (per-pass visited set); depth clamped to [1, DEPTH_CAP]."""
    depth = _clamp_depth(depth)
    if direction not in ("out", "in", "both"):
        direction = "out"
    passes = ["out", "in"] if direction == "both" else [direction]

    edges: list = []
    rev = None
    for edge_dir in passes:
        if edge_dir == "in" and rev is None:
            rev = _reverse_index(url, token)
        visited = {slug}
        frontier = [slug]
        for hop in range(1, depth + 1):
            nxt: list = []
            for node in frontier:
                if edge_dir == "out":
                    if hop == 1:
                        # ROOT node (frontier is just [slug] at hop 1): let a
                        # transport error PROPAGATE so the CLI/MCP boundary can
                        # report "gbrain unreachable" (rc 1 / []). Swallowing it
                        # here would make a down brain look like an empty graph
                        # — silent failure. Deeper nodes are skipped instead.
                        neighbours = _forward_targets(url, token, node)
                    else:
                        try:
                            neighbours = _forward_targets(url, token, node)
                        except Exception:
                            neighbours = []  # skip unreadable deep node, keep walking
                else:
                    neighbours = rev.get(node, []) if rev else []
                for nbr in neighbours:
                    edges.append((node, nbr, hop, edge_dir))
                    if nbr not in visited:
                        visited.add(nbr)
                        nxt.append(nbr)
            frontier = nxt
            if not frontier:
                break
    return edges


def backlinks(url: str, token: str, slug: str) -> list:
    """Pages that link TO `slug` (single-hop incoming), order-preserving."""
    rev = _reverse_index(url, token)
    return list(dict.fromkeys(rev.get(slug, [])))


def _as_okf_edge(frm: str, to: str, direction: str, hop: int) -> dict:
    """OKF edge record — the knowledge-hemisphere edge shape the walker reads."""
    return {"type": "edge", "hemisphere": "knowledge", "from": frm, "to": to,
            "rel": "links", "direction": direction, "hop": hop}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -v`
Expected: PASS (all engine tests green, existing tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/memory.py tests/test_memory_command.py
git commit -m "feat: file-native OKF graph traversal engine in swarph memory (#42)"
```

---

### Task 3: `swarph memory links` flags + OKF `--json`

Wire `--backlinks`/`--depth`/`--direction` into the `links` subcommand with the mutual-exclusion guard and OKF edge output. Preserve default output exactly.

**Files:**
- Modify: `src/swarph_cli/commands/memory.py` (the `p_links` parser + the `if args.subcommand == "links"` handler, ~lines 119-121 and 169-180)
- Test: `tests/test_memory_command.py`

**Interfaces:**
- Consumes: `traverse`, `_as_okf_edge` (Task 2); `links` (Task 1).
- Produces: CLI behaviour for `swarph memory links <slug> [--backlinks] [--depth N] [--direction {out,in,both}] [--json]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_memory_command.py` (reuses `_fake_corpus` from Task 2):

```python
def test_links_default_output_unchanged(monkeypatch, capsys):
    m = _fake_corpus(monkeypatch, {"a": "[[b]] [[c]]", "b": "", "c": ""})
    monkeypatch.setattr(m.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(m.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    rc = m.run_memory(["links", "a"])
    assert rc == 0
    assert capsys.readouterr().out == "b\nc\n"


def test_links_json_emits_okf_edges(monkeypatch, capsys):
    m = _fake_corpus(monkeypatch, {"a": "[[b]]", "b": ""})
    monkeypatch.setattr(m.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(m.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    rc = m.run_memory(["links", "a", "--json"])
    assert rc == 0
    out = __import__("json").loads(capsys.readouterr().out)
    assert out == [{"type": "edge", "hemisphere": "knowledge", "from": "a",
                    "to": "b", "rel": "links", "direction": "out", "hop": 1}]


def test_links_backlinks_flag(monkeypatch, capsys):
    m = _fake_corpus(monkeypatch, {"t": "", "x": "[[t]]"})
    monkeypatch.setattr(m.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(m.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    rc = m.run_memory(["links", "t", "--backlinks"])
    assert rc == 0
    assert capsys.readouterr().out == "x\n"


def test_links_backlinks_with_direction_is_exit_2(monkeypatch, capsys):
    m = _fake_corpus(monkeypatch, {"t": ""})
    monkeypatch.setattr(m.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(m.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    rc = m.run_memory(["links", "t", "--backlinks", "--direction", "in"])
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_links_depth_direction_both(monkeypatch, capsys):
    m = _fake_corpus(monkeypatch, {"hub": "[[o]]", "o": "", "s": "[[hub]]"})
    monkeypatch.setattr(m.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(m.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    rc = m.run_memory(["links", "hub", "--direction", "both"])
    assert rc == 0
    assert capsys.readouterr().out == "o\ns\n"


def test_links_root_unreachable_is_rc1_not_silent(monkeypatch, capsys):
    """A transport error fetching the ROOT page must surface as rc 1 (like the
    old `links`), NOT be swallowed into an empty rc-0 graph (silent failure)."""
    from swarph_cli.commands import memory

    def _boom(url, token, slug):
        raise ConnectionError("gbrain down")

    monkeypatch.setattr(memory, "get_page", _boom)
    monkeypatch.setattr(memory.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(memory.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    rc = memory.run_memory(["links", "a"])
    assert rc == 1
    assert "unreachable" in capsys.readouterr().err
```

Also update any existing test that asserts the OLD `links --json` slug-array shape. Search first: `grep -n "links" tests/test_memory_command.py | grep -i json`. If one exists (e.g. asserts `["b","c"]`), replace its assertion with the OKF-edge shape above; the shape change is approved.

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -k "links_default or links_json or backlinks_flag or backlinks_with_direction or depth_direction_both" -v`
Expected: FAIL — `--backlinks`/`--depth`/`--direction` are unrecognised args (SystemExit 2 from argparse for the flags that don't exist yet, or wrong output for the json/default cases).

- [ ] **Step 3: Write minimal implementation**

In `src/swarph_cli/commands/memory.py`, replace the `p_links` block (~line 119-121):

```python
    p_links = sub.add_parser("links", help="graph links out of / into a page (file-native OKF traversal)")
    p_links.add_argument("slug")
    p_links.add_argument("--backlinks", action="store_true",
                         help="incoming links (who links to this page) — sugar for --direction in")
    p_links.add_argument("--depth", type=int, default=1,
                         help="traversal hops (clamped to 1-10; default 1)")
    p_links.add_argument("--direction", choices=["out", "in", "both"], default=None,
                         help="traversal direction (default out)")
    p_links.add_argument("--json", action="store_true",
                         help="emit OKF edge records instead of plain slugs")
```

Replace the `if args.subcommand == "links":` handler (~line 169-180):

```python
    if args.subcommand == "links":
        if args.backlinks and args.direction is not None:
            print("swarph memory links: --backlinks and --direction are "
                  "mutually exclusive", file=sys.stderr)
            return 2
        direction = "in" if args.backlinks else (args.direction or "out")
        try:
            edges = traverse(url, token, args.slug, args.depth, direction)
        except Exception as e:  # fail-safe: never traceback at the CLI
            print(f"swarph memory links: gbrain unreachable ({e})", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps([_as_okf_edge(f, t, d, h)
                              for (f, t, h, d) in edges], indent=2))
        else:
            seen: list = []
            for (f, t, h, d) in edges:
                if t not in seen:
                    seen.append(t)
                    print(t)
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/memory.py tests/test_memory_command.py
git commit -m "feat: swarph memory links --backlinks/--depth/--direction + OKF --json (#42)"
```

---

### Task 4: Extend the `swarph_memory_navigate` MCP tool

Add `backlinks` and `traverse` ops to the memory-navigate MCP surface, fail-safe and whitelisted.

**Files:**
- Modify: `src/swarph_cli/commands/mcp_server.py` (`_memory_navigate` ~lines 188-211; the `@mcp.tool swarph_memory_navigate` wrapper ~lines 269-277)
- Test: `tests/test_memory_command.py` (or the existing mcp_server test module if one is used for `_memory_navigate` — put these beside the other `_memory_navigate` tests; grep `grep -rln "_memory_navigate" tests/`)

**Interfaces:**
- Consumes: `memory.backlinks`, `memory.traverse`, `memory._as_okf_edge` (Tasks 2).
- Produces: `_memory_navigate(op, slug=None, type=None, tag=None, limit=20, depth=1, direction="out")` handling ops `get`/`list`/`links`/`backlinks`/`traverse`; unknown op or backend error → `[]`.

- [ ] **Step 1: Write the failing tests**

Add beside the other `_memory_navigate` tests (grep to find the file; assume `tests/test_memory_command.py` if none dedicated):

```python
def test_memory_navigate_backlinks_op(monkeypatch):
    from swarph_cli.commands import mcp_server, memory
    monkeypatch.setattr(memory, "get_page",
                        lambda u, t, s: {"slug": s, "content": {"x": "[[t]]", "t": ""}.get(s, "")})
    monkeypatch.setattr(memory, "list_pages",
                        lambda *a, **k: [{"slug": "x"}, {"slug": "t"}])
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    assert mcp_server._memory_navigate("backlinks", slug="t") == ["x"]


def test_memory_navigate_traverse_op_returns_okf_edges(monkeypatch):
    from swarph_cli.commands import mcp_server, memory
    monkeypatch.setattr(memory, "get_page",
                        lambda u, t, s: {"slug": s, "content": {"a": "[[b]]"}.get(s, "")})
    monkeypatch.setattr(memory, "list_pages", lambda *a, **k: [{"slug": "a"}, {"slug": "b"}])
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    out = mcp_server._memory_navigate("traverse", slug="a", depth=1, direction="out")
    assert out == [{"type": "edge", "hemisphere": "knowledge", "from": "a",
                    "to": "b", "rel": "links", "direction": "out", "hop": 1}]


def test_memory_navigate_unknown_op_is_empty(monkeypatch):
    from swarph_cli.commands import mcp_server
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    assert mcp_server._memory_navigate("bogus", slug="a") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -k "memory_navigate_backlinks or memory_navigate_traverse or memory_navigate_unknown" -v`
Expected: FAIL — `_memory_navigate` returns `[]` for `backlinks`/`traverse` (ops not handled yet) so the backlinks/traverse assertions fail.

- [ ] **Step 3: Write minimal implementation**

In `src/swarph_cli/commands/mcp_server.py`, update `_memory_navigate` signature + body (~line 188):

```python
def _memory_navigate(op: str, slug: str | None = None, type: str | None = None,
                     tag: str | None = None, limit: int = 20,
                     depth: int = 1, direction: str = "out"):
    """Deterministic OKF memory navigation — the knowledge-hemisphere twin of
    swarph_codegraph_query. Ops: 'get', 'list', 'links', 'backlinks' (incoming),
    'traverse' (multi-hop OKF edges via depth/direction). File-native graph.

    Fail-safe: any backend/parse error, or an unrecognised op, resolves to [] or
    {} — never raises."""
    try:
        url = brain_ask._resolve_endpoint()
        token = brain_ask._resolve_token(None, brain_ask._self_name())
        if op == "get" and slug:
            return memory.get_page(url, token, slug)
        if op == "list":
            return memory.list_pages(url, token, type, tag, limit)
        if op == "links" and slug:
            return memory.links(url, token, slug)
        if op == "backlinks" and slug:
            return memory.backlinks(url, token, slug)
        if op == "traverse" and slug:
            return [memory._as_okf_edge(f, t, d, h)
                    for (f, t, h, d) in memory.traverse(url, token, slug, depth, direction)]
        return []
    except Exception:
        return []
```

Update the `@mcp.tool swarph_memory_navigate` wrapper (~line 269):

```python
    @mcp.tool()
    def swarph_memory_navigate(op: str, slug: str = "", tag: str = "",
                               type: str = "", limit: int = 20,
                               depth: int = 1, direction: str = "out"):
        """Deterministic OKF memory navigation over gbrain: op='get'|'list'|'links'|'backlinks'|'traverse'.
        Exact canonical recall + file-native graph traversal (backlinks, multi-hop
        --depth/--direction) — the knowledge-hemisphere counterpart to
        swarph_codegraph_query. For fuzzy recall use semantic search instead."""
        return _memory_navigate(op, slug=slug or None, type=type or None,
                                tag=tag or None, limit=limit,
                                depth=depth, direction=direction)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/python -m pytest tests/test_memory_command.py -k "memory_navigate" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/mcp_server.py tests/test_memory_command.py
git commit -m "feat: swarph_memory_navigate MCP gains backlinks/traverse ops (#42)"
```

---

### Task 5: README + version bump to 0.32.0

Document the new flags (with the honest O(N) scan note) and bump the version across all pinned locations.

**Files:**
- Modify: `README.md` (§memory / `swarph memory` section)
- Modify: `pyproject.toml` (`version = "0.31.0"` → `"0.32.0"`)
- Modify: `src/swarph_cli/__init__.py` (`__version__ = "0.31.0"` → `"0.32.0"`)
- Modify: `tests/test_brain_ask_command.py` (rename `test_version_is_0_31_0` → `test_version_is_0_32_0`, assert `"0.32.0"`)
- Modify: `tests/test_watchdog.py` (same rename/assert at line ~1044)

**Interfaces:** none (docs + metadata).

- [ ] **Step 1: Update the version-pin tests (write the failing assertion first)**

In `tests/test_brain_ask_command.py` (line ~18) and `tests/test_watchdog.py` (line ~1044), rename both functions and bump the asserted string:

```python
def test_version_is_0_32_0():
    import swarph_cli
    assert swarph_cli.__version__ == "0.32.0"
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/python -m pytest tests/test_brain_ask_command.py::test_version_is_0_32_0 tests/test_watchdog.py::test_version_is_0_32_0 -v`
Expected: FAIL — `__version__` is still `"0.31.0"`.

- [ ] **Step 3: Bump the version**

In `pyproject.toml`:

```toml
version = "0.32.0"
```

In `src/swarph_cli/__init__.py`:

```python
__version__ = "0.32.0"
```

- [ ] **Step 4: Run to verify they pass**

Run: `venv/bin/python -m pytest tests/test_brain_ask_command.py::test_version_is_0_32_0 tests/test_watchdog.py::test_version_is_0_32_0 -v`
Expected: PASS.

- [ ] **Step 5: Update the README**

In `README.md`, find the `swarph memory` section (search `swarph memory links`). Update the `links` line and add the flags + note. Insert:

```markdown
- `swarph memory links <slug>` — links **out of** a page (forward `[[links]]`).
  - `--backlinks` — links **into** the page (who links to it).
  - `--depth N` — multi-hop traversal (1–10; default 1).
  - `--direction out|in|both` — traversal direction (default `out`; `--backlinks` is sugar for `in`).
  - `--json` — OKF edge records (`{type, hemisphere, from, to, rel, direction, hop}`).

  The graph is **file-native**: edges are read from page bodies via the shared
  OKF link grammar, so it works against any brain regardless of whether its edge
  index is populated. Note: `--backlinks` / `--direction in|both` scan the whole
  corpus (one `get_page` per page) — O(N), fine for today's brain; a future
  gbrain edge-index will provide an O(1) indexed fast-path.
```

- [ ] **Step 6: Run the full suite**

Run: `venv/bin/python -m pytest -q`
Expected: PASS (all green; no regressions).

- [ ] **Step 7: Commit**

```bash
git add README.md pyproject.toml src/swarph_cli/__init__.py tests/test_brain_ask_command.py tests/test_watchdog.py
git commit -m "docs: swarph memory graph flags + bump to 0.32.0 (#42)"
```

---

## Definition of Done

- `venv/bin/python -m pytest -q` fully green.
- `swarph memory links <slug>` default output byte-for-byte unchanged; `--backlinks`/`--depth`/`--direction`/`--json` behave per spec; `--backlinks --direction` → exit 2.
- `memory.parse_links is okf_links.parse_okf_links` (duplication retired).
- `swarph_memory_navigate` MCP tool exposes `backlinks`/`traverse`, fail-safe to `[]`.
- Version is `0.32.0` in all four pinned locations.
- Branch `feat/memory-graph-traversal` pushed; **PR opened, left for review** (new public surface — not auto-merged). PyPI publish deferred (commander-gated).
- Edge-index backfill remains a **separate** card (untouched here).
```
