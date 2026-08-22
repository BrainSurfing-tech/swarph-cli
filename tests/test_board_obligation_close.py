"""#562's CLI HALF — the explicit close act had no verb. Post-#562 the ONLY
way to close an obligation is POST /board/obligations/{id}/close with an
outcome and evidence; a holder doing that raw with curl is the journey the
board verbs exist to carry. `swarph board obligations close` is that act.

The verb's honesty rules, inherited from the endpoint:
- outcome is one of pass|fail|cannot_evaluate — argparse choices, so a typo
  refuses before the network.
- evidence must be non-empty AFTER stripping — the gateway's min_length=1
  accepts "   "; the CLI refuses it, because whitespace evidence is the
  vibe-close the endpoint exists to kill.
- the gateway's refusals (403 not-your-obligation, 409 already-closed,
  404 unknown) propagate with their detail and a non-zero exit — a refusal
  rendered as success is #319's class, one verb over.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import board


def _harness(monkeypatch, tmp_path, *, status=200, payload=None):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "shared-auth")
    captured = {}

    def fake_post(url, body, token, *, timeout=10.0):
        captured["url"] = url
        captured["body"] = body
        return status, payload if payload is not None else {
            "id": 22, "status": "closed", "closed_by": "cursor-lin",
            "close_outcome": body["outcome"],
        }

    monkeypatch.setattr(board, "_post_json", fake_post)
    return captured


def test_close_posts_the_explicit_act(monkeypatch, tmp_path, capsys):
    captured = _harness(monkeypatch, tmp_path)
    rc = board.run_board(["obligations", "close", "22", "--as", "cursor-lin",
                          "--outcome", "pass",
                          "--evidence", "ran the check: A and B hold"])
    assert rc == 0
    assert captured["url"].endswith("/board/obligations/22/close")
    assert captured["body"] == {"outcome": "pass",
                                "evidence": "ran the check: A and B hold"}
    out = capsys.readouterr().out
    assert "CLOSED" in out and "pass" in out


def test_outcome_is_a_closed_set(monkeypatch, tmp_path):
    _harness(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc:
        board.run_board(["obligations", "close", "22", "--as", "cursor-lin",
                         "--outcome", "done", "--evidence", "x"])
    assert exc.value.code != 0, "a bogus outcome refuses before the network"


def test_whitespace_only_evidence_is_refused_before_the_network(monkeypatch, tmp_path, capsys):
    captured = _harness(monkeypatch, tmp_path)
    rc = board.run_board(["obligations", "close", "22", "--as", "cursor-lin",
                          "--outcome", "pass", "--evidence", "   "])
    assert rc != 0
    assert not captured, "whitespace evidence is the vibe-close — refused locally"


def test_gateway_refusals_propagate_with_their_detail(monkeypatch, tmp_path, capsys):
    _harness(monkeypatch, tmp_path, status=409,
             payload={"detail": "obligation 22 is already closed"})
    rc = board.run_board(["obligations", "close", "22", "--as", "cursor-lin",
                          "--outcome", "pass", "--evidence", "ran it"])
    assert rc != 0
    assert "already closed" in capsys.readouterr().err, (
        "the gateway's reason reaches the operator — a refusal rendered as "
        "success is #319's class, one verb over")
