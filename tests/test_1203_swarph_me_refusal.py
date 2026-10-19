"""card #1203 — swarph-me REFUSES when SWARPH_SELF is set (before any token
is read), with an explicit, loud --as-operator escape hatch."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from swarph_cli.operator import cli  # noqa: E402
from swarph_cli.operator import config as op_config  # noqa: E402


def _cfg(tmp_path):
    token = tmp_path / "commander.peer_token"
    token.write_text("test-token\n", encoding="utf-8")
    cfg = tmp_path / "operator.json"
    op_config.save_config(identity="commander", gateway="http://example.test:8788",
                          token_file=str(token), path=cfg)
    return cfg


def test_self_set_refuses_and_no_token_opened(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")
    monkeypatch.setattr(cli, "_read_token",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("token was read before refusal")))
    rc = cli.main(["cards", "--config", str(_cfg(tmp_path))])
    assert rc != 0
    err = capsys.readouterr().err
    assert "SWARPH_SELF=cursor-win" in err
    assert "this process is inside a cell" in err
    assert "run swarph-me from a human shell" in err


def test_as_operator_proceeds_and_stderr_names_both(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_SELF", "cursor-win")
    monkeypatch.setattr(cli, "run_verb", lambda identity, gateway, token, rest: 0)
    rc = cli.main(["cards", "--as-operator", "--config", str(_cfg(tmp_path))])
    assert rc == 0
    err = capsys.readouterr().err
    assert "cursor-win" in err and "commander" in err


def test_self_unset_behaviour_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.setattr(cli, "run_verb", lambda identity, gateway, token, rest: 0)
    rc = cli.main(["cards", "--config", str(_cfg(tmp_path))])
    assert rc == 0
    assert "inside a cell" not in capsys.readouterr().err
