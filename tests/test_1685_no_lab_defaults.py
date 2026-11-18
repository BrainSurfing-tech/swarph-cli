"""card #1685 (v1.0 V3): the swarph-cli wheel names no lab-only peer.

Five modules routed to a hardcoded peer (stall_alert, the triage router,
duplicate_session, rowclear, wake_watchdog), the onboard handshake carried a
private-repo reference and a default witness, and main.py linked a private repo.
A fresh install has none of those peers. So:

  (a) a scan of the BUILT wheel fails on a string literal equal to a lab-only
      name, and planting one turns it red;
  (b) with no config each of the five names no target, routes nowhere, and prints
      ONE line saying so; with config set, routing equals what it was;
  (c) the handshake `onboard` writes has no private-repo reference and no default
      witness;
  (d) main.py links no private repository.

The scan reads the wheel, not src/: a literal that exists in the tree but not in the
shipped wheel (or the reverse) is a different question, and the wheel is what a
stranger installs.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarph_cli import escalation

ROOT = Path(__file__).resolve().parents[1]
BANNED = ("lab-ovh", "drop-on-meta-edge", "science-claude", "hedge-fund-mcp", "darw007d")
OWNERS_SHIPPED = ROOT / "src" / "swarph_cli" / "triage" / "owners.json"

# What lab ran on before #1685, as config. The PR body tells lab-ovh to set these.
LAB_OPERATOR_JSON = {
    "escalation_target": "lab-ovh",
    "escalation_alternate": "drop-on-meta-edge",
    "rowclear_opt_in": ["drop-on-meta-edge"],
}
LAB_TRIAGE_OWNERS = {"owners": {
    "security": {"owner": "drop-on-meta-edge"},
    "memory": {"owner": "lab-ovh"},
    "calibration": {"owner": "science-claude"},
}}


# ── (a) the scan ────────────────────────────────────────────────────────────


def banned_literals(root: Path) -> list[str]:
    """`file:line: literal` for every string literal in every .py under root that
    equals a banned name. Comments never reach the AST; a docstring (the first
    statement of a module, class or function) is excluded; any other string,
    including one inside an f-string, a dict or a tuple, counts."""
    found = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_bytes(), filename=str(path))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and node.value.strip() in BANNED):
                found.append(f"{path.relative_to(root).as_posix()}:{node.lineno}: "
                             f"{node.value.strip()}")
    return found


@pytest.fixture(scope="module")
def wheel_tree(tmp_path_factory):
    """The wheel `pip wheel .` builds from this tree, unzipped. The build must not
    see the source tree through PYTHONPATH (test_1073_wheel_install, same reason)."""
    tmp = tmp_path_factory.mktemp("wheel-1685")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(tmp / "dist")],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stderr[-2000:]
    wheels = sorted((tmp / "dist").glob("swarph_cli-*.whl"))
    assert len(wheels) == 1, [w.name for w in wheels]
    tree = tmp / "tree"
    with zipfile.ZipFile(wheels[0]) as zf:
        zf.extractall(tree)
    return tree


def test_the_built_wheel_has_no_lab_only_string_literal(wheel_tree):
    scanned = sorted(p.relative_to(wheel_tree).as_posix() for p in wheel_tree.rglob("*.py"))
    # the scan must have looked at the five modules, or a green is a scan of nothing
    for needed in ("swarph_cli/stall_alert.py", "swarph_cli/duplicate_session.py",
                   "swarph_cli/rowclear.py", "swarph_cli/triage/router.py",
                   "swarph_cli/scripts/wake_watchdog.py", "swarph_cli/commands/onboard.py",
                   "swarph_cli/main.py"):
        assert needed in scanned, needed
    assert len(scanned) > 100, len(scanned)
    assert banned_literals(wheel_tree) == []


@pytest.mark.parametrize("literal", BANNED)
def test_planting_one_literal_in_the_built_wheel_turns_the_scan_red(wheel_tree, tmp_path, literal):
    planted = tmp_path / "tree"
    shutil.copytree(wheel_tree, planted)
    (planted / "swarph_cli" / "planted.py").write_text(f'TARGET = "{literal}"\n', encoding="utf-8")
    assert banned_literals(planted) == [f"swarph_cli/planted.py:1: {literal}"]


def test_the_scan_sees_a_literal_wherever_it_hides_and_ignores_prose(tmp_path):
    (tmp_path / "prose.py").write_text(textwrap.dedent('''\
        """lab-ovh in a module docstring"""
        # lab-ovh in a comment
        class C:
            """drop-on-meta-edge in a class docstring"""
            def f(self):
                """science-claude in a function docstring"""
                return "lab-ovh is a peer"
        '''), encoding="utf-8")
    assert banned_literals(tmp_path) == []
    (tmp_path / "hiding.py").write_text(textwrap.dedent('''\
        A = {"k": "lab-ovh"}
        B = ("x", "drop-on-meta-edge")
        C = f"science-claude"
        D = [1, "darw007d"]
        def f():
            x = 1
            "hedge-fund-mcp"
        '''), encoding="utf-8")
    hits = banned_literals(tmp_path)
    assert [h.split(": ")[1] for h in hits] == [
        "lab-ovh", "drop-on-meta-edge", "science-claude", "darw007d", "hedge-fund-mcp"]


def test_shipped_owner_map_names_no_peer():
    shipped = json.loads(OWNERS_SHIPPED.read_text(encoding="utf-8"))
    assert all(v.get("owner") is None for v in shipped["owners"].values())
    assert not any(name in OWNERS_SHIPPED.read_text(encoding="utf-8") for name in BANNED)


# ── (b) no config: no target, one line; config set: routing as before ──────


def _lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]


def _operator_json(payload: dict) -> Path:
    path = Path.home() / ".config" / "swarph" / "operator.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _recorder(monkeypatch, module):
    calls = []

    def fake(method, url, token, payload=None):
        calls.append((method, url, payload))
        return {"obligations": []}

    monkeypatch.setattr(module, "_request", fake)
    return calls


def _posts(calls):
    return [c[2] for c in calls if c[0] == "POST"]


def test_stall_alert_with_no_config_names_no_target(monkeypatch, capsys):
    from swarph_cli import stall_alert as st
    calls = _recorder(monkeypatch, st)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    (ask,) = _posts(calls)
    assert ask["holder"] == "cell-x"
    assert "fallback_target" not in ask and "timeout_hours" not in ask
    (line,) = _lines(capsys)
    assert "no stall escalation target configured" in line and "routes nowhere" in line
    # a name that used to be special is not special now
    calls.clear()
    assert st.send_stall_alert("http://gw", "tok", "lab-ovh", 60, 1) is True
    (ask,) = _posts(calls)
    assert ask["holder"] == "lab-ovh" and "fallback_target" not in ask
    assert _lines(capsys) == []  # once per process, not once per tick


def test_duplicate_session_with_no_config_names_no_target(monkeypatch, capsys):
    from swarph_cli import duplicate_session as ds
    calls = _recorder(monkeypatch, ds)
    assert ds.send_duplicate_alert("http://gw", "tok", "cell-x", [11, 12]) is False
    assert _posts(calls) == []
    (line,) = _lines(capsys)
    assert "no duplicate-session alert target configured" in line


def test_rowclear_with_no_config_opts_no_cell_in_and_says_so_once(monkeypatch, capsys):
    from swarph_cli import rowclear
    typed = []

    def boom(*a, **k):
        raise AssertionError("read the board for a cell nobody opted in")

    def run():
        return rowclear.maybe_rowclear(
            SimpleNamespace(self_name="any-cell"), agent_running=lambda t: False,
            http_get=boom, type_clear=lambda t: typed.append(t) or True)

    assert run() == "off"  # the default mode: nothing runs, nothing is printed
    assert _lines(capsys) == []
    monkeypatch.setenv("SWARPH_ROWCLEAR", "live")
    assert run() == "not-opted-in"
    (line,) = _lines(capsys)
    assert "no opt-in cell configured" in line
    assert run() == "not-opted-in"
    assert _lines(capsys) == [] and typed == []


def test_triage_with_no_config_routes_to_nobody_and_counts(capsys):
    from swarph_cli.triage import backend as _backend
    from swarph_cli.triage import job as _job
    from swarph_cli.triage import router as _router
    from swarph_cli.triage.rows import TriageStore
    owners = _router.load_owners(str(OWNERS_SHIPPED))
    assert _router.organ_owner(owners, "security") == (None, True)
    store = TriageStore(_backend.MemoryBackend())
    record = {"source_organ": "stuck-work", "type": "real-stall", "confidence": 1.0,
              "owner_hint": None, "holder_is_peer": False,
              "evidence": {"waiter_row": 1, "need_row": 2}, "item_id": "stuck:1:2:v",
              "sharp": True, "security": False}
    summary = _job.route_and_store(store, owners, [record])
    assert summary["appended"] == 0 and summary["unrouted"] == 1
    assert store.footer()["unmapped"] == {"stuck-work": 1}
    (line,) = _lines(capsys)
    assert "no default owner configured" in line


def _watchdog(tmp_path, *, extra_env=None, args=()):
    """Two runs of the real script against a stub `swarph`: the first marks the
    missing cell, the second (after backdating it) raises the outage."""
    root = tmp_path / "state"
    inbox = root / "nobody" / "mesh-sidecar"
    inbox.mkdir(parents=True)
    (inbox / "inbox.log").write_text("")
    stub = tmp_path / "swarph"
    stub.write_text('#!/bin/sh\necho "$@" >> "$STUB_LOG"\nexit 0\n')
    stub.chmod(0o755)
    log, state_path = tmp_path / "stub.log", tmp_path / "wake.json"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "SWARPH_STATE_ROOT": str(root),
           "WAKE_WATCHDOG_STATE": str(state_path), "SWARPH_BIN": str(stub),
           "STUB_LOG": str(log), **(extra_env or {})}
    for name in ("SWARPH_SELF", "MESH_GATEWAY_URL", "WAKE_WATCHDOG_ESCALATE"):
        if name not in (extra_env or {}):
            env.pop(name, None)
    cmd = [sys.executable, "-m", "swarph_cli.scripts.wake_watchdog", "--as", "watcher", *args]
    subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    saved = json.loads(state_path.read_text())
    saved["nobody"]["missing_since"] = 0
    state_path.write_text(json.dumps(saved))
    second = subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    sends = [ln for ln in log.read_text().splitlines() if ln.startswith("mesh send")]
    return sends, [ln for ln in second.stderr.splitlines() if ln.strip()]


_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the POSIX swarph stub cannot be argv0 on Windows")


@_posix_only
def test_wake_watchdog_with_no_config_names_no_escalation_peer(tmp_path):
    sends, err = _watchdog(tmp_path)
    assert len(sends) == 1 and sends[0].startswith("mesh send nobody ")
    assert len(err) == 1 and "no escalation peer configured" in err[0]


@_posix_only
@pytest.mark.parametrize("how", ["env", "operator.json", "flag"])
def test_wake_watchdog_with_config_escalates_as_before(tmp_path, how):
    extra, args = {}, ()
    if how == "env":
        extra = {"SWARPH_ESCALATION_ALTERNATE": "drop-on-meta-edge"}
    elif how == "operator.json":
        _operator_json(LAB_OPERATOR_JSON)
    else:
        args = ("--escalate", "drop-on-meta-edge")
    sends, err = _watchdog(tmp_path, extra_env=extra, args=args)
    assert [s.split()[2] for s in sends] == ["nobody", "drop-on-meta-edge"]
    assert err == []


def test_stall_alert_with_lab_config_routes_as_before(monkeypatch, capsys):
    _operator_json(LAB_OPERATOR_JSON)
    from swarph_cli import stall_alert as st
    calls = _recorder(monkeypatch, st)
    assert st.send_stall_alert("http://gw", "tok", "cell-x", 60, 1) is True
    (ask,) = _posts(calls)
    assert (ask["holder"], ask["fallback_target"], ask["timeout_hours"]) == ("cell-x", "lab-ovh", 2)
    calls.clear()
    assert st.send_stall_alert("http://gw", "tok", "lab-ovh", 60, 1) is True
    (ask,) = _posts(calls)
    assert ask["holder"] == "drop-on-meta-edge" and "fallback_target" not in ask
    assert _lines(capsys) == []


def test_duplicate_session_with_lab_config_routes_as_before(monkeypatch, capsys):
    _operator_json(LAB_OPERATOR_JSON)
    from swarph_cli import duplicate_session as ds
    assert ds._holder_for("cell-x") == "lab-ovh"
    assert ds._holder_for("lab-ovh") == "drop-on-meta-edge"
    calls = _recorder(monkeypatch, ds)
    assert ds.send_duplicate_alert("http://gw", "tok", "cell-x", [1, 2]) is True
    assert _posts(calls)[0]["holder"] == "lab-ovh"
    assert _lines(capsys) == []


def test_rowclear_with_lab_config_opts_in_the_same_cell(monkeypatch):
    _operator_json(LAB_OPERATOR_JSON)
    from swarph_cli import rowclear
    monkeypatch.setenv("SWARPH_ROWCLEAR", "live")

    def run(name):
        state = SimpleNamespace(self_name=name, gateway="http://gw", token="t")
        return rowclear.maybe_rowclear(
            state, agent_running=lambda t: False,
            http_get=lambda url, token: (500, {}), type_clear=lambda t: True)

    assert run("some-cell") == "not-opted-in"
    assert run("drop-on-meta-edge") == "blocked-rows-unknown"  # past the opt-in gate


def test_triage_with_lab_config_routes_every_organ_as_before(tmp_path, monkeypatch):
    local = tmp_path / "owners.local.json"
    local.write_text(json.dumps(LAB_TRIAGE_OWNERS), encoding="utf-8")
    _operator_json({**LAB_OPERATOR_JSON, "triage_owners_file": str(local)})
    from swarph_cli.commands import triage as triage_cmd
    from swarph_cli.triage import router as _router
    args = triage_cmd._build_parser().parse_args([])
    assert args.owners_file == str(local)
    owners = _router.load_owners(str(OWNERS_SHIPPED), args.owners_file)
    assert _router.organ_owner(owners, "security") == ("drop-on-meta-edge", False)
    assert _router.organ_owner(owners, "memory") == ("lab-ovh", False)
    assert _router.organ_owner(owners, "calibration") == ("science-claude", False)
    assert _router.organ_owner(owners, "brand-new-organ") == ("lab-ovh", True)
    assert _router.default_owner() == "lab-ovh"


def test_env_beats_operator_json_and_a_bad_file_reads_as_unset(monkeypatch):
    path = _operator_json(LAB_OPERATOR_JSON)
    assert escalation.target() == "lab-ovh"
    monkeypatch.setenv("SWARPH_ESCALATION_TARGET", "other-peer")
    assert escalation.target() == "other-peer"
    monkeypatch.delenv("SWARPH_ESCALATION_TARGET")
    assert escalation.rowclear_opt_in() == frozenset({"drop-on-meta-edge"})
    for broken in ("{not json", "[]", '{"escalation_target": 7}', '{"escalation_target": " "}'):
        path.write_text(broken, encoding="utf-8")
        assert escalation.target() is None, broken
    path.unlink()
    assert escalation.target() is None and escalation.rowclear_opt_in() == frozenset()


def test_resaving_operator_identity_keeps_the_escalation_keys(tmp_path):
    from swarph_cli.operator import config
    path = tmp_path / "operator.json"
    path.write_text(json.dumps({**LAB_OPERATOR_JSON, "identity": "old", "gateway": "http://old",
                                "token_file": "~/old"}), encoding="utf-8")
    config.save_config(identity="new", gateway="http://new", path=path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["identity"] == "new" and saved["gateway"] == "http://new"
    assert saved["escalation_target"] == "lab-ovh" and saved["rowclear_opt_in"] == ["drop-on-meta-edge"]


# ── (c) the handshake `onboard` writes ──────────────────────────────────────


def _written_handshake(monkeypatch, tmp_path) -> str:
    from swarph_cli.commands import onboard
    import swarph_shared

    def fake_post(url, body, token, *, method="POST"):
        return 200, {"status": "registered", "name": "test-peer", "ratified": False,
                     "registered_unratified": True}

    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")
    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr(swarph_shared, "verify_subscription_setup", lambda: True, raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    rc = onboard.run_onboard(["test-peer", "--gateway", "http://localhost:8788",
                              "--state-dir", str(tmp_path / "state")])
    assert rc == 0
    return (tmp_path / "test-peer-handshake.md").read_text(encoding="utf-8")


def test_the_handshake_has_no_private_reference_and_no_default_witness(monkeypatch, tmp_path):
    body = _written_handshake(monkeypatch, tmp_path)
    assert "DM SEMANTICS" in body and "Mesh-secrets out-of-band" in body and "test-peer" in body
    for private in (*BANNED, "Science Claude", "PLAN.md", "CLAUDE.md", "auto-memory",
                    "paper main draft"):
        assert private not in body, private
    assert "--caller test-peer.handshake\n" in body
    assert "witness peer. The witness" in body


def test_the_handshake_names_the_witness_only_when_one_is_configured(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARPH_WITNESS", "my-witness")  # the variable `swarph ratify` reads
    body = _written_handshake(monkeypatch, tmp_path)
    assert "--caller test-peer.handshake.witness-my-witness\n" in body
    assert "witness peer (``my-witness``)" in body


# ── (d) main.py ─────────────────────────────────────────────────────────────


def test_main_links_no_private_repository():
    from swarph_cli import main
    source = (ROOT / "src" / "swarph_cli" / "main.py").read_text(encoding="utf-8")
    repos = set(re.findall(r"github\.com/([\w.-]+/[\w.-]+)", source))
    assert repos == {"BrainSurfing-tech/swarph-cli"}, repos
    for private in ("darw007d", "hedge-fund-mcp"):
        assert private not in source and private not in main._BANNER
    link = re.search(r"https://github\.com/BrainSurfing-tech/swarph-cli/blob/main/(\S+)",
                     main._BANNER)
    assert link and (ROOT / link.group(1)).is_file(), "the spec link must name a file this repo ships"
