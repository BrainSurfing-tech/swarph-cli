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


def _host_of(text: str):
    import re as _re
    m = _re.search(r"://([^/\"'\s]+)", text or "")
    return m.group(1) if m else None


def scan(root: Path):
    """Return [(relpath, line, client, host)] for every network call under
    root. host is the literal destination host when the call (or a module
    constant it references) names one, else None (dynamic/configured)."""
    hits = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root.parent.parent).as_posix() \
            if root == SRC else path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        bindings = {}     # local name -> module kind
        fn_bindings = {}  # local name -> client kind (from-imported callables)
        consts = {}       # module-level string constants
        opener_vars = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = node.value.value
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
                        elif a.name == "build_opener":
                            fn_bindings[a.asname or a.name] = "urllib.request.build_opener"
                elif node.module == "urllib" :
                    for a in node.names:
                        if a.name == "request":
                            bindings[a.asname or a.name] = "urllib.request"
                elif node.module == "socket":
                    for a in node.names:
                        if a.name in ("socket", "create_connection"):
                            fn_bindings[a.asname or a.name] = f"socket.{a.name}"
                elif node.module in _MODULE_KINDS:
                    for a in node.names:
                        fn_bindings[a.asname or a.name] = _MODULE_KINDS[node.module]
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if _dotted(node.value.func).endswith("build_opener"):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            opener_vars.add(t.id)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            dotted = _dotted(node.func)
            root_name = dotted.split(".")[0] if dotted else ""
            attr = dotted.rsplit(".", 1)[-1] if dotted else ""
            kind = None
            if isinstance(node.func, ast.Name) and node.func.id in fn_bindings:
                kind = fn_bindings[node.func.id]
            elif isinstance(node.func, ast.Name) and node.func.id in opener_vars:
                kind = "urllib.request.urlopen"  # opener(...) call
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call) \
                    and _dotted(node.func.value.func).endswith("build_opener") \
                    and attr == "open":
                kind = "urllib.request.urlopen"  # build_opener(...).open(...)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id in opener_vars and attr == "open":
                kind = "urllib.request.urlopen"  # opener.open(...)
            elif dotted.endswith(".urlopen") and bindings.get(root_name) == "urllib.request":
                kind = "urllib.request.urlopen"
            elif dotted.endswith(".HTTPConnection") or dotted.endswith(".HTTPSConnection"):
                if bindings.get(root_name) == "http.client" or root_name == "http":
                    kind = "http.client"
            elif dotted in ("socket.socket", "socket.create_connection") \
                    and bindings.get(root_name) == "socket":
                kind = dotted
            elif isinstance(node.func, ast.Name) and node.func.id in (
                    "Session", "Client", "AsyncClient", "ClientSession"):
                base = {"Session": "requests", "Client": "httpx",
                        "AsyncClient": "httpx", "ClientSession": "aiohttp"}[node.func.id]
                kind = f"{base}.{node.func.id}"
            elif dotted == "websockets.connect" and bindings.get(root_name) == "websockets":
                kind = "websockets.connect"
            elif bindings.get(root_name) in ("requests", "httpx", "aiohttp", "websockets") \
                    and attr in _HTTP_ATTRS:
                kind = f"{bindings[root_name]}.{attr}"
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
                host = None
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        host = _host_of(sub.value)
                    elif isinstance(sub, ast.Name) and sub.id in consts:
                        host = _host_of(consts[sub.id])
                    if host:
                        break
                hits.append((rel, node.lineno, kind, host))
    return hits


def _egress_rows(text: str):
    """Rows keyed (file, client) -> destination text. A hit with a literal
    host is mapped only when its row's destination names that host (M2: a
    new destination in a listed file fails)."""
    rows = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[0].endswith(".py"):
            rows.setdefault((cells[0], cells[1]), []).append(cells[2])
    return rows


def unmapped(hits, rows):
    out = []
    for rel, ln, kind, host in hits:
        dests = rows.get((rel, kind))
        if dests is None:
            out.append((rel, ln, kind, host))
        elif host is not None and not any(host in d for d in dests):
            out.append((rel, ln, kind, host))
    return out


def test_every_network_call_is_listed_in_egress_md():
    hits = scan(SRC)
    assert hits, "scanner found no network calls — the walk is broken"
    rows = _egress_rows(EGRESS.read_text(encoding="utf-8"))
    bad = unmapped(hits, rows)
    assert not bad, "unlisted network calls (add an EGRESS.md row): " + \
        ", ".join(f"{f}:{ln} ({k} -> {h})" for f, ln, k, h in bad)


def test_mutant_unlisted_requests_get_fails(tmp_path):
    """Mutant check: an unlisted requests.get in a scratch copy must fail."""
    pkg = tmp_path / "swarph_cli"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        "import requests\n\ndef go():\n    return requests.get('https://example.invalid')\n")
    hits = scan(pkg)
    assert hits and hits[0][2] == "requests.get"
    assert unmapped(hits, {}) == hits
    assert unmapped(hits, {("mod.py", "requests.get"): ["example.invalid"]}) == []


_MUTANTS = {
    # M2: a NEW destination in a file that is already listed
    "M2_new_destination_in_listed_file": (
        "src/swarph_cli/commands/version.py",
        "import urllib.request\n\ndef go():\n"
        "    return urllib.request.urlopen('https://evil.example/x')\n"),
    "M3_http_client": (
        "src/swarph_cli/mod.py",
        "import http.client\n\ndef go():\n"
        "    return http.client.HTTPSConnection('example.invalid')\n"),
    "M4_requests_Session": (
        "src/swarph_cli/mod.py",
        "from requests import Session\n\ndef go():\n    return Session()\n"),
    "M5_from_urllib_import_request": (
        "src/swarph_cli/mod.py",
        "from urllib import request\n\ndef go():\n"
        "    return request.urlopen('https://example.invalid')\n"),
    "M6_build_opener_open": (
        "src/swarph_cli/mod.py",
        "import urllib.request\n\ndef go():\n"
        "    return urllib.request.build_opener().open('https://example.invalid')\n"),
    "M7_httpx_Client": (
        "src/swarph_cli/mod.py",
        "from httpx import Client\n\ndef go():\n    return Client()\n"),
    "M8_aiohttp_ClientSession": (
        "src/swarph_cli/mod.py",
        "from aiohttp import ClientSession\n\ndef go():\n    return ClientSession()\n"),
    "M13_websockets_connect": (
        "src/swarph_cli/mod.py",
        "import websockets\n\ndef go():\n    return websockets.connect('wss://example.invalid')\n"),
    "M9_socket_create_connection": (
        "src/swarph_cli/mod.py",
        "import socket\n\ndef go():\n"
        "    return socket.create_connection(('example.invalid', 443))\n"),
    "M10_subprocess_curl": (
        "src/swarph_cli/mod.py",
        "import subprocess\n\ndef go():\n"
        "    return subprocess.run(['curl', 'https://example.invalid/x'])\n"),
    "M11_subprocess_ssh": (
        "src/swarph_cli/mod.py",
        "import subprocess\n\ndef go():\n"
        "    return subprocess.run(['ssh', 'example.invalid', 'true'])\n"),
}


import pytest as _pytest


@_pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_mutants_are_detected_and_unmapped(name, tmp_path):
    rel, src = _MUTANTS[name]
    pkg = tmp_path / "swarph_cli"
    (pkg / "commands").mkdir(parents=True)
    target = pkg / Path(rel).name if False else tmp_path / Path(rel).name
    target.write_text(src)
    hits = scan(tmp_path)
    assert hits, f"{name}: scanner missed the mutant"
    rows = _egress_rows(EGRESS.read_text(encoding="utf-8"))
    if name.startswith("M2"):
        # place the mutant IN the listed file: only its new destination
        # (evil.example, not in that file's row) may unmap it
        hits = [(rel, ln, kind, host) for _, ln, kind, host in hits]
    assert unmapped(hits, rows), f"{name}: mutant was silently mapped"
