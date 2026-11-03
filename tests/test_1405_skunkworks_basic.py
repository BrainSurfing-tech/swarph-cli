"""Card #1405: the parts of ``swarph skunkworks`` that need no swarph-mesh skunkworks library.

These run against the swarph-mesh a fresh install resolves (the pin allows 0.9.0, which has
no skunkworks package), so they stay green before 0.9.1 ships. The tests that run a pipeline
are in test_1405_skunkworks_cli.py and skip until swarph-mesh >= 0.9.1 is installed.
"""
import ast
import sys
from pathlib import Path

import pytest

from swarph_cli.commands import skunkworks as cli

_ROOT = Path(__file__).resolve().parents[1]
_DOCS = {"howto": _ROOT / "docs" / "skunkworks-howto.md",
         "guide": _ROOT / "src" / "swarph_cli" / "guide" / "GUIDE.md"}


def test_verb_is_registered():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["skunkworks"] == "swarph_cli.commands.skunkworks.run_skunkworks"
    assert callable(cli.run_skunkworks)


def test_old_swarph_mesh_gets_a_one_line_message(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "swarph_mesh.skunkworks", None)
    assert cli.run_skunkworks(["status", "sk_1_abcd1234", "--as", "x"]) == 1
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "needs swarph-mesh 0.9.x with skunkworks" in err


def test_cut_leaves_short_text_alone():
    assert cli._cut("abc", "/p") == "abc"
    cut = cli._cut("a" * 500, "/p", limit=100)
    assert len(cut) == 100 and cut.startswith("aaa") and cut.endswith("full text at /p]")


def test_decide_has_no_override_in_the_parser_or_the_source():
    """Clause 9. Mutation: add an --override flag to decide -> the parser check fails."""
    p = cli._build_parser()
    opts = {o for sp in p._subparsers._group_actions[0].choices.values()
            for a in sp._actions for o in a.option_strings}
    assert not [o for o in opts if "override" in o.lower()], opts
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["decide", "sk_1_abcd1234", "APPROVED", "--quote-file",
                                        "q", "--override", "--as", "x"])
    tree = ast.parse(Path(cli.__file__).read_text())
    names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)] + \
            [n.arg for n in ast.walk(tree) if isinstance(n, (ast.arg, ast.keyword)) and n.arg] + \
            [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    assert not [n for n in names if "override" in n.lower()]


def test_default_card_post_uses_the_board_say_path(monkeypatch):
    calls = []

    def get(url, token, **kw):
        calls.append(("GET", url))
        return 200, {"thread_uuid": "T-1", "assignee": "peer-b", "id": 7}

    def post(url, body, token, **kw):
        calls.append(("POST", url, body))
        return 200, {"id": 1}
    monkeypatch.setattr(cli, "_http_get_json", get)
    monkeypatch.setattr(cli, "_post_json", post)
    cli._default_post_card("http://gw.invalid", "tok", "cell-a", False)(7, "status", "hello")
    assert calls[0] == ("GET", "http://gw.invalid/board/cards/7")
    _, url, body = calls[1]
    assert url == "http://gw.invalid/messages"
    assert body == {"from_node": "cell-a", "to_node": "peer-b", "kind": "status",
                    "content": "hello", "thread_id": "T-1"}
    monkeypatch.setattr(cli, "_post_json", lambda *a, **k: (403, {"detail": "no grant"}))
    with pytest.raises(RuntimeError, match="403"):
        cli._default_post_card("http://gw.invalid", "tok", "cell-a", False)(7, "status", "x")


@pytest.mark.parametrize("name", ["howto", "guide"])
def test_docs_name_commands_window_billing_and_no_code(name):
    text = _DOCS[name].read_text(encoding="utf-8")
    low = text.lower()
    for verb in ("run", "status", "show", "stop", "decide"):
        assert f"swarph skunkworks {verb}" in text or f"`{verb}`" in text, (name, verb)
    assert "09:30-16:00" in text and "new york" in low
    assert "claude quota" in low and "queue" in low
    assert "produces no code" in low
