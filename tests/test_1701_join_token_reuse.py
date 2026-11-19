"""Card #1701: onboard reuses join's token and skips the second register."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from swarph_cli.commands.onboard import run_onboard

_REPO = Path(__file__).resolve().parents[1]
_ONBOARD = _REPO / "src" / "swarph_cli" / "commands" / "onboard.py"
_TOKEN = "operator-token"
_JOIN = "join-saved-token"


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.delenv("SWARPH_WITNESS", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_URL", raising=False)
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)


def _stub():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            seen.append((self.command, self.path, self.headers.get("Authorization"), raw))
            if self.path.split("?", 1)[0].endswith("/peers/register"):
                payload = b'{"registered_unratified":false,"token_status":"existing"}'
                code = 200
            elif self.path.split("?", 1)[0].endswith("/peers"):
                payload = b'{"peers":[{"name":"lab-ovh","ratified":true}]}'
                code = 200
            else:
                payload = b"{}"
                code = 404
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._body()

        def do_POST(self):
            self._body()

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def _place_join(home: Path, name: str, gateway: str, token: str = _JOIN, token_name: str | None = None):
    folder = home / ".config" / "swarph"
    folder.mkdir(parents=True, exist_ok=True)
    token_path = folder / f"{token_name or name}.peer_token"
    token_path.write_text(token + "\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    sidecar = folder / f"{name}.join.json"
    sidecar.write_text(json.dumps({
        "name": name,
        "gateway": gateway,
        "token_file": str(token_path),
    }), encoding="utf-8")
    return token_path


def _run(argv, home: Path):
    server, seen = _stub()
    host, port = server.server_address
    env_url = f"http://{host}:{port}"
    os.environ["MESH_GATEWAY_TOKEN"] = _TOKEN
    os.environ["MESH_GATEWAY_URL"] = env_url
    state = home / "state"
    try:
        rc = run_onboard(argv + ["--state-dir", str(state)])
    finally:
        server.shutdown()
    return rc, seen, state, env_url


def test_skip_only_for_a_sidecar_naming_this_peer_and_token_file(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    gateway = "http://sidecar.example:8788"
    _place_join(tmp_path, "new-cell", gateway)
    rc, seen, state, _ = _run(["new-cell"], tmp_path)
    captured = capsys.readouterr()
    posts = [item for item in seen if item[0] == "POST"]
    assert rc == 0
    assert posts == []
    assert "not registering a second time" in captured.out
    text = (state / "new-cell" / ".env.example").read_text(encoding="utf-8")
    assert f"MESH_GATEWAY_URL={gateway}\n" in text
    assert _JOIN not in captured.out
    assert _JOIN not in captured.err


def test_wrong_sidecar_name_still_registers(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    token_path = _place_join(tmp_path, "new-cell", "http://sidecar.example:8788")
    sidecar = token_path.with_name("new-cell.join.json")
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["name"] = "other-cell"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    rc, seen, _, env_url = _run(["new-cell"], tmp_path)
    assert rc == 0
    posts = [item for item in seen if item[0] == "POST" and item[1] == "/peers/register"]
    assert len(posts) == 1
    body = json.loads(posts[0][3])
    assert body["name"] == "new-cell"
    assert env_url in capsys.readouterr().out


def test_other_token_file_still_registers(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    _place_join(tmp_path, "new-cell", "http://sidecar.example:8788")
    other = tmp_path / "other.token"
    other.write_text(_TOKEN + "\n", encoding="utf-8")
    os.chmod(other, 0o600)
    rc, seen, _, _ = _run(["new-cell", "--token-file", str(other)], tmp_path)
    assert rc == 0
    assert any(item[0] == "POST" and item[1] == "/peers/register" for item in seen)


def test_parsed_gateway_beats_the_environment_and_the_sidecar(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    _place_join(tmp_path, "new-cell", "http://sidecar.example:8788")
    flag = "http://flag.example:8788"
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://env.example:8788")
    rc = run_onboard(["new-cell", "--g", flag, "--state-dir", str(tmp_path / "state")])
    captured = capsys.readouterr()
    assert rc == 0
    text = (tmp_path / "state" / "new-cell" / ".env.example").read_text(encoding="utf-8")
    assert f"MESH_GATEWAY_URL={flag}\n" in text
    assert "env.example" not in text
    assert "sidecar.example" not in text
    assert "not registering a second time" in captured.out


def _load_main_onboard(tmp_path: Path):
    show = subprocess.run(
        ["git", "show", "origin/main:src/swarph_cli/commands/onboard.py"],
        cwd=_REPO, capture_output=True,
    )
    if show.returncode != 0:
        subprocess.check_call(["git", "fetch", "--depth", "1", "origin", "main"], cwd=_REPO)
        show = subprocess.run(
            ["git", "show", "origin/main:src/swarph_cli/commands/onboard.py"],
            cwd=_REPO, capture_output=True, check=True,
        )
    path = tmp_path / "onboard_main.py"
    path.write_bytes(show.stdout)
    spec = importlib.util.spec_from_file_location("onboard_main_tree", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _once(fn, argv, home: Path, url: str):
    state = home / "state"
    if state.exists():
        shutil.rmtree(state)
    server, seen = _stub()
    host, port = server.server_address
    live = f"http://{host}:{port}"
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    os.environ["MESH_GATEWAY_TOKEN"] = _TOKEN
    os.environ["MESH_GATEWAY_URL"] = url or live
    expanded = [arg.replace("URL", live) for arg in argv]
    try:
        rc = fn(expanded + ["--state-dir", str(state)])
    finally:
        server.shutdown()
    env_file = state / "new-cell" / ".env.example"
    body = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    posts = [item for item in seen if item[0] == "POST"]
    return rc, posts, body.replace(live, "URL")


@pytest.mark.parametrize("argv", [
    ["new-cell", "--gateway", "URL"],
    ["new-cell", "--g", "URL"],
    ["new-cell", "--gateway", "URL/extra"],
    ["new-cell", "--gateway", "URL/?q=1"],
    ["new-cell", "--gateway", "  URL  "],
    ["new-cell"],
])
def test_register_path_matches_main(argv, monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    # A sidecar that does not name this peer must not move the register path.
    _place_join(tmp_path, "other-cell", "http://sidecar.example:8788", token_name="other-cell")
    main = _load_main_onboard(tmp_path)
    capsys.readouterr()
    head = _once(run_onboard, argv, tmp_path / "head", "")
    capsys.readouterr()
    base = _once(main.run_onboard, argv, tmp_path / "base", "")
    assert head[0] == base[0]
    assert head[1] == base[1]
    assert head[2] == base[2]


def _mentions_gateway(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and "MESH_GATEWAY_URL" in child.value:
            return True
        if isinstance(child, ast.Name) and child.id == "ENV_GATEWAY":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "ENV_GATEWAY":
            return True
    return False


def _is_write(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr in {"write_text", "write"}


class _Writes(ast.NodeVisitor):
    def __init__(self):
        self.func = "<module>"
        self.found = []

    def visit_FunctionDef(self, node):
        previous = self.func
        self.func = node.name
        self.generic_visit(node)
        self.func = previous

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        if _is_write(node) and any(_mentions_gateway(arg) for arg in node.args):
            self.found.append((self.func, node))
        self.generic_visit(node)


def _checked_gateway_shape(tree: ast.AST) -> bool:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_checked_gateway":
            body = [stmt for stmt in node.body if not isinstance(stmt, ast.Expr)]
            if len(body) != 1 or not isinstance(body[0], ast.Return):
                return False
            call = body[0].value
            return (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "require_gateway"
                and len(call.args) == 1
                and isinstance(call.args[0], ast.Name)
                and not call.keywords
            )
    return False


def _writer_uses_checked_name(tree: ast.AST) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != "_write_peer_env":
            continue
        assigned = False
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "checked"
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "_checked_gateway"
                and len(stmt.value.args) == 1
                and isinstance(stmt.value.args[0], ast.Name)
                and stmt.value.args[0].id == "gateway"
            ):
                assigned = True
        return assigned
    return False


def _url_value_is_only_checked(call: ast.Call) -> bool:
    for arg in call.args:
        if not isinstance(arg, ast.JoinedStr):
            continue
        values = arg.values
        for index, part in enumerate(values):
            if not (isinstance(part, ast.Constant) and isinstance(part.value, str) and part.value.endswith("MESH_GATEWAY_URL=")):
                continue
            nxt = values[index + 1] if index + 1 < len(values) else None
            after = values[index + 2] if index + 2 < len(values) else None
            return (
                isinstance(nxt, ast.FormattedValue)
                and isinstance(nxt.value, ast.Name)
                and nxt.value.id == "checked"
                and nxt.format_spec is None
                and isinstance(after, ast.Constant)
                and isinstance(after.value, str)
                and after.value.startswith("\n")
            )
    return False


def gateway_write_problems(root: Path) -> list[str]:
    problems = []
    onboard = None
    writes = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        if path.name == "onboard.py":
            onboard = tree
        finder = _Writes()
        finder.visit(tree)
        writes.extend((path, func, node) for func, node in finder.found)
    if onboard is None or not _checked_gateway_shape(onboard):
        problems.append("checked gateway")
    if onboard is None or not _writer_uses_checked_name(onboard):
        problems.append("writer call")
    if len(writes) != 1 or writes and writes[0][1] != "_write_peer_env":
        problems.append(f"writes:{len(writes)}")
    elif not _url_value_is_only_checked(writes[0][2]):
        problems.append("written value")
    return problems


def test_structural_writer_is_clean():
    assert gateway_write_problems(_REPO / "src") == []


def _plant_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "src"
    shutil.copytree(_REPO / "src" / "swarph_cli", dest / "swarph_cli", ignore=shutil.ignore_patterns("__pycache__"))
    return dest


def _apply(root: Path, edits):
    for rel, old, new in edits:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        assert text.count(old) == 1, old[:40]
        path.write_text(text.replace(old, new, 1), encoding="utf-8")


_O = "swarph_cli/commands/onboard.py"
_OK = '    print_safe(f"      ok (inbox.log, cursor.json, .env.example, run-daemon.sh)")\n'
_V = "os.environ.get('SWARPH_PLANT', '')"


def _second(expr, pre=""):
    return [(_O, _OK, f"{pre}    (peer_dir / '.env').write_text({expr}, encoding='utf-8')\n" + _OK)]


_PLANTS = {
    "W1 f-string": _second(f'f"MESH_GATEWAY_URL={{{_V}}}\\n"'),
    "W2 format": _second(f'"MESH_GATEWAY_URL={{}}\\n".format({_V})'),
    "W3 append": [(_O, _OK, f"    with open(env_example, 'a', encoding='utf-8') as _f:\n        _f.write('MESH_GATEWAY_URL=' + {_V} + '\\n')\n" + _OK)],
    "W4 concat": _second(f'"MESH_GATEWAY_URL" + "=" + {_V} + "\\n"'),
    "W5 constant": _second(f'f"{{ENV_GATEWAY}}={{{_V}}}\\n"', pre="    from swarph_cli.gateway_default import ENV_GATEWAY\n"),
    "W6 percent": _second(f'"%s=%s\\n" % ("MESH_GATEWAY_URL", {_V})'),
    "W7 format args": _second(f'"{{}}={{}}\\n".format("MESH_GATEWAY_URL", {_V})'),
    "W8 join": _second(f'"=".join(("MESH_GATEWAY_URL", {_V})) + "\\n"'),
    "W9 split literal": _second(f'"MESH_GATEWAY_" "URL=" + {_V} + "\\n"'),
    "W10 dict": _second(f'"".join(f"{{k}}={{v}}\\n" for k, v in {{"MESH_GATEWAY_URL": {_V}}}.items())'),
    "W11 other module": [(
        "swarph_cli/tokens.py",
        "\ndef read_token_file(",
        "\ndef write_env(peer_dir, value):\n    (peer_dir / '.env').write_text(f\"MESH_GATEWAY_URL={value}\\n\", encoding='utf-8')\n\n\ndef read_token_file(",
    ), (_O, _OK, f"    from swarph_cli.tokens import write_env\n    write_env(peer_dir, {_V})\n" + _OK)],
    "W12 join module": [(
        "swarph_cli/commands/join.py",
        "        config_saved = True\n",
        "        config_saved = True\n        (folder / f\"{args.name}.env\").write_text(f\"MESH_GATEWAY_URL={args.gateway}\\n\")\n",
    )],
    "B1 raw assignment": [(_O, "    checked = _checked_gateway(gateway)\n", "    checked = gateway\n")],
    "B2 check returns raw": [(_O, "    return require_gateway(value)\n", "    return value\n")],
    "B3 write uses the parameter": [(_O, "MESH_GATEWAY_URL={checked}", "MESH_GATEWAY_URL={gateway}")],
    "B4 text after the check": [(_O, "MESH_GATEWAY_URL={checked}\\n", "MESH_GATEWAY_URL={checked}{gateway}\\n")],
    "B5 call dropped": [(_O, "    checked = _checked_gateway(gateway)\n", "    checked = gateway + ''\n")],
    "B6 second writer": _second(f'f"MESH_GATEWAY_URL={{checked}}\\n"'),
}


@pytest.mark.parametrize("name", list(_PLANTS))
def test_plant_is_caught(name, tmp_path):
    root = _plant_copy(tmp_path)
    _apply(root, _PLANTS[name])
    assert gateway_write_problems(root), name
