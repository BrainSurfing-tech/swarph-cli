"""card #1214 — egress inventory: the build fails when code gains a
network call that EGRESS.md does not list.

Egress-inventory test pattern after XERJ (github.com/xerj-org/xerj, Apache-2.0),
engine/crates/xerj-rerank/tests/egress_inventory.rs. Idea only; no code copied.

Walks src/swarph_cli with the AST for network clients (requests, httpx,
urllib.request, http.client, socket connections, aiohttp, websockets, and
subprocess argv starting curl/wget/ssh/scp). Every hit must map to an
EGRESS.md row keyed by (file, client), or this test fails naming file:line.
"""
import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "swarph_cli"
EGRESS = ROOT / "EGRESS.md"

_MODULE_KINDS = {
    "requests": "requests", "httpx": "httpx", "aiohttp": "aiohttp",
    "websockets": "websockets", "http.client": "http.client",
}
_HTTP_ATTRS = {"get", "post", "put", "delete", "head", "patch", "request"}
_SUBPROCESS_TOOLS = {"curl", "wget", "ssh", "scp"}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def scan(root: Path):
    """Return [(relpath, line, client)] for every network call under root."""
    hits = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root.parent.parent).as_posix() \
            if root == SRC else path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        bindings = {}   # local name -> module kind
        fn_bindings = {}  # local name -> client kind (from-imported callables)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    kind = _MODULE_KINDS.get(a.name) or (
                        "urllib.request" if a.name.startswith("urllib") else
                        "socket" if a.name == "socket" else None)
                    if kind:
                        bindings[a.asname or a.name.split(".")[0]] = kind
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("urllib.request"):
                    for a in node.names:
                        if a.name in ("urlopen", "Request"):
                            fn_bindings[a.asname or a.name] = "urllib.request.urlopen"
                elif node.module == "socket":
                    for a in node.names:
                        if a.name in ("socket", "create_connection"):
                            fn_bindings[a.asname or a.name] = f"socket.{a.name}"
                elif node.module in _MODULE_KINDS:
                    for a in node.names:
                        fn_bindings[a.asname or a.name] = _MODULE_KINDS[node.module]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted(node.func)
            root_name = dotted.split(".")[0] if dotted else ""
            kind = None
            if isinstance(node.func, ast.Name) and node.func.id in fn_bindings:
                kind = fn_bindings[node.func.id]
            elif dotted.endswith(".urlopen") and bindings.get(root_name) == "urllib.request":
                kind = "urllib.request.urlopen"
            elif dotted in ("socket.socket", "socket.create_connection") \
                    and bindings.get(root_name) == "socket":
                kind = dotted
            elif bindings.get(root_name) in ("requests", "httpx", "aiohttp", "websockets") \
                    and dotted.rsplit(".", 1)[-1] in _HTTP_ATTRS:
                kind = f"{bindings[root_name]}.{dotted.rsplit('.', 1)[-1]}"
            elif dotted.split(".")[0] == "subprocess" or (
                    isinstance(node.func, ast.Name)
                    and node.func.id in ("run", "Popen", "call", "check_output", "check_call")):
                if node.args:
                    first = node.args[0]
                    tool = None
                    if isinstance(first, (ast.List, ast.Tuple)) and first.elts \
                            and isinstance(first.elts[0], ast.Constant):
                        tool = str(first.elts[0].value).split()[0] if first.elts[0].value else None
                    elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                        tool = first.value.split()[0] if first.value.split() else None
                    if tool in _SUBPROCESS_TOOLS:
                        kind = f"subprocess:{tool}"
            if kind:
                hits.append((rel, node.lineno, kind))
    return hits


def _egress_rows(text: str):
    rows = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0].endswith(".py"):
            rows.add((cells[0], cells[1]))
    return rows


def unmapped(hits, rows):
    return [h for h in hits if (h[0], h[2]) not in rows]


def test_every_network_call_is_listed_in_egress_md():
    hits = scan(SRC)
    assert hits, "scanner found no network calls — the walk is broken"
    rows = _egress_rows(EGRESS.read_text(encoding="utf-8"))
    bad = unmapped(hits, rows)
    assert not bad, "unlisted network calls (add an EGRESS.md row): " + \
        ", ".join(f"{f}:{ln} ({k})" for f, ln, k in bad)


def test_mutant_unlisted_requests_get_fails(tmp_path):
    """Mutant check: an unlisted requests.get in a scratch copy must fail."""
    pkg = tmp_path / "swarph_cli"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "import requests\n\ndef go():\n    return requests.get('https://example.invalid')\n")
    hits = scan(pkg)
    assert hits and hits[0][2] == "requests.get"
    assert unmapped(hits, set()) == hits
    assert unmapped(hits, {("mod.py", "requests.get")}) == []
