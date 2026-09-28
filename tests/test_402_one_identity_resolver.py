"""#402 — one identity resolver, no default peer name, SWARPH_SELF outranks SWARPH_CELL.

The card: "a rule repeated at N sites is a rule applied at N-1". #360 fixed four
peer-name defaults, then a fifth, sixth and seventh turned up. Closure asked for
one shared resolver, identity as a required input that RAISES when undeclared,
every site routed through it, and the two variables collapsed to one order.

Re-measured on main bda694b before building: the headline hooks.py site had been
fixed, but brain_ask still defaulted to 'lab-ovh' and ignored SWARPH_CELL, and the
MCP codegraph tool passed SWARPH_CELL as the EXPLICIT identity (CELL-first, the
order #538 measured posting under another cell's name).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from swarph_cli import identity
from swarph_cli.commands import brain_ask

_VARS = ("SWARPH_SELF", "SWARPH_CELL", "SWARPH_NODE")


@pytest.fixture
def clean(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


# ---- the resolver -------------------------------------------------------------

def test_order_is_flag_then_self_then_cell(clean):
    clean.setenv("SWARPH_SELF", "self-cell")
    clean.setenv("SWARPH_CELL", "leaked-cell")
    assert identity.declared() == ("self-cell", "$SWARPH_SELF")
    assert identity.declared("flag-cell") == ("flag-cell", "flag")
    clean.delenv("SWARPH_SELF")
    assert identity.declared() == ("leaked-cell", "$SWARPH_CELL")


def test_undeclared_raises_and_never_guesses(clean):
    assert identity.declared() == (None, "undeclared")
    with pytest.raises(identity.IdentityNotDeclared) as e:
        identity.require(verb="probe")
    assert "no default" in str(e.value)
    assert identity.or_sentinel() == identity.SENTINEL == "unidentified-cell"


def test_a_self_only_site_ignores_an_ambient_SWARPH_CELL(clean):
    """The mesh verbs read SWARPH_SELF only: reading an inbox consumes it."""
    clean.setenv("SWARPH_CELL", "leaked-cell")
    assert identity.declared(env=identity.SELF_ONLY) == (None, "undeclared")


# ---- the card's three-way retest, at a live site ---------------------------------

@pytest.mark.parametrize("env,expected", [
    ({"SWARPH_CELL": "ws-lc"}, None),         # main: 'lab-ovh'. CELL is ambient here: it
                                              # would pick a peer TOKEN, so it declares nothing
    ({"SWARPH_SELF": "ws-lc"}, "ws-lc"),
    ({}, None),                               # main: 'lab-ovh' (peer-name default)
])
def test_brain_ask_three_way_retest(clean, env, expected):
    for k, v in env.items():
        clean.setenv(k, v)
    assert brain_ask._self_name() == expected


def test_brain_ask_never_selects_a_token_from_a_leaked_SWARPH_CELL(clean, tmp_path):
    """psmux leaks SWARPH_CELL (#538). If brain_ask took it as the identity, a leaked
    CELL would promote ANOTHER cell's peer token above GBRAIN_TOKEN."""
    from swarph_cli import tokens
    clean.setattr(tokens.Path, "home", staticmethod(lambda: tmp_path))
    d = tmp_path / ".config" / "swarph"
    d.mkdir(parents=True)
    (d / "other-cell.peer_token").write_text("OTHER-CELLS-TOKEN")
    clean.setenv("SWARPH_CELL", "other-cell")
    clean.setenv("GBRAIN_TOKEN", "BRAIN-TOKEN")
    assert brain_ask._resolve_token(None, brain_ask._self_name()) == "BRAIN-TOKEN"


def test_brain_ask_gateway_path_refuses_an_undeclared_cell(clean, tmp_path, capsys):
    """Main: an undeclared cell hunted lab-ovh.peer_token and presented it if found."""
    clean.setattr(brain_ask.Path, "home", staticmethod(lambda: tmp_path))
    tok = tmp_path / ".config" / "swarph" / "lab-ovh.peer_token"
    tok.parent.mkdir(parents=True)
    tok.write_text("SOMEONE-ELSES-TOKEN")
    clean.setenv("SWARPH_BRAIN_GATEWAY", "http://127.0.0.1:9")
    clean.setenv("SWARPH_BRAIN_MCP", "http://127.0.0.1:9")
    called = {}
    clean.setattr(brain_ask, "_gateway_query",
                  lambda *a, **k: called.setdefault("q", a) or [])
    rc = brain_ask.run_brain_ask(["what"])
    assert rc == 2
    assert "q" not in called, "must not query the gateway with another cell's token"
    assert "no cell identity is declared" in capsys.readouterr().err


def test_mcp_codegraph_identity_is_SELF_first(clean, monkeypatch):
    """Main passed SWARPH_CELL as the explicit identity, so a leaked CELL won."""
    from swarph_cli.commands import codegraph, mcp_server
    seen = {}

    def fake_query(q, *, index_path, caller_cell, **kw):
        seen["caller"] = caller_cell
        return []

    monkeypatch.setattr(codegraph, "structural_query", fake_query)
    clean.setenv("SWARPH_SELF", "self-cell")
    clean.setenv("SWARPH_CELL", "leaked-cell")
    mcp_server._codegraph_query("anything")
    assert seen.get("caller") == "self-cell"


# ---- the card's census: identity is read in ONE module ---------------------------

# Modules that must run as a BARE FILE (`python3 cell_selfcheck.py`) on a host where
# swarph_cli is not importable, so their module scope is stdlib-only (pinned by
# test_cell_selfcheck / test_139_tool_probe). They cannot import swarph_cli.identity.
# Both read SWARPH_SELF directly and neither falls back to a peer name.
_BARE_FILE_MODULES = {"commands/cell_selfcheck.py", "commands/cell_probe.py"}


def test_no_module_but_identity_reads_an_identity_variable():
    src = pathlib.Path(identity.__file__).parent
    hits = []
    for p in src.rglob("*.py"):
        if p.name == "identity.py" or p.relative_to(src).as_posix() in _BARE_FILE_MODULES:
            continue
        tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and n.value in _VARS:
                parent_calls = [c for c in ast.walk(tree)
                                if isinstance(c, ast.Call) and n in c.args]
                reads = [c for c in parent_calls
                         if ast.unparse(c.func).endswith(("environ.get", "getenv"))]
                if reads:
                    hits.append(f"{p.relative_to(src)}:{n.lineno}")
            if (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                    and n.slice.value in _VARS and isinstance(n.ctx, ast.Load)
                    and ast.unparse(n.value).endswith("environ")):
                hits.append(f"{p.relative_to(src)}:{n.lineno}")
            if isinstance(n, (ast.Tuple, ast.List)) and any(
                    isinstance(e, ast.Constant) and e.value in _VARS for e in n.elts):
                hits.append(f"{p.relative_to(src)}:{n.lineno}")
    assert hits == [], hits


def test_brain_ask_has_no_default_peer_name():
    assert not hasattr(brain_ask, "_DEFAULT_SELF")


# ---- review of a6f7faf (drop-on-meta-edge, 12-agent pass) --------------------------

def test_the_first_non_empty_value_decides_and_a_blank_one_does_not_fall_through(clean):
    """Main's `a or b` chains: '' falls through, anything else stops the search. A
    whitespace-only SWARPH_SELF must NOT hand the identity to a leaked SWARPH_CELL."""
    clean.setenv("SWARPH_SELF", "   ")
    clean.setenv("SWARPH_CELL", "leaked-cell")
    assert identity.declared() == (None, "undeclared")
    clean.setenv("SWARPH_SELF", "")
    assert identity.declared() == ("leaked-cell", "$SWARPH_CELL")   # '' is unset, as on main
    clean.setenv("SWARPH_SELF", "  padded  ")
    assert identity.declared() == ("padded", "$SWARPH_SELF")
    assert identity.declared("   ") == ("padded", "$SWARPH_SELF")  # a blank flag = no flag (#872)


def test_channel_gate_normalises_both_sides(clean, monkeypatch):
    from swarph_cli import channel
    monkeypatch.setenv("SWARPH_CHANNEL", "dev")
    clean.setenv("SWARPH_SELF", "  s  ")
    monkeypatch.setenv("SWARPH_CHANNEL_CELL", "  s  ")
    assert channel.channel_serves() is True
    monkeypatch.setenv("SWARPH_CHANNEL_CELL", "t")
    assert channel.channel_serves() is False


def test_monitor_calls_a_blank_as_derived_like_the_resolver_does(clean):
    import argparse
    from swarph_cli.commands import monitor
    assert monitor._self_name_was_derived(argparse.Namespace(self_name="   ")) is True
    assert monitor._self_name_was_derived(argparse.Namespace(self_name="cell-a")) is False


def test_memory_gateway_path_refuses_an_undeclared_cell_by_name(clean):
    """Review finding: it looked up ~/.config/swarph/None.peer_token."""
    from swarph_cli.commands import memory
    clean.setenv("SWARPH_BRAIN_GATEWAY", "http://127.0.0.1:9")
    with pytest.raises(OSError) as e:
        memory._via_gateway("get", {"slug": "x"})
    assert "SWARPH_SELF" in str(e.value) and "None.peer_token" not in str(e.value)


@pytest.mark.parametrize("source,explicit", [
    ("--cell", True), ("$SWARPH_SELF", True), ("$SWARPH_CELL", True),
    ("git user.name", False), ("hostname", False),
])
def test_highlight_only_a_declared_identity_selects_its_peer_token(monkeypatch, source, explicit):
    """Review finding (pre-existing on main): an undeclared highlight guessed the cell
    from git user.name, e.g. 'lab-ovh', and then presented lab-ovh's own peer token."""
    from swarph_cli.commands import highlight
    seen = {}

    def fake_token(cell, token_file, **kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    monkeypatch.setattr(highlight, "_resolve_token", fake_token)
    highlight._log_via_gateway("http://127.0.0.1:9", "some-cell", "h", "", "t", None,
                               cell_source=source)
    assert seen.get("identity_is_explicit") is explicit


def test_memory_emit_hook_passes_where_its_cell_came_from(clean, monkeypatch):
    from swarph_cli.commands import memory_emit_hook as meh
    monkeypatch.setattr(meh.socket, "gethostname", lambda: "Lab-ovh")
    assert meh._cell_with_source() == ("Lab-ovh", "hostname")
    clean.setenv("SWARPH_SELF", "cell-a")
    assert meh._cell_with_source() == ("cell-a", "$SWARPH_SELF")


def test_watchdog_install_refuses_the_sentinel(clean, capsys):
    from swarph_cli.commands.watchdog import run_watchdog
    rc = run_watchdog(argv=["--install-service", "--dry-run"])
    assert rc == 4
    assert "no cell identity declared" in capsys.readouterr().err
