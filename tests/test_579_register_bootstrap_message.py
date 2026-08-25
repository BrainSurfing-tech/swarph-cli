"""#579: `swarph mesh register` must not send a bootstrapping operator in a circle.

droplet hit this onboarding `friendly-coder` on 2026-08-25. The old message was:

    cannot resolve mesh token; set MESH_GATEWAY_TOKEN or create
    /root/.config/swarph/friendly-coder.peer_token

That names the very file `register` exists to MINT. For a NEW peer the file cannot
exist yet, so the advice is unfollowable exactly when it is needed. `swarph onboard`
already explained the operator path properly; this asserts `mesh register` does too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarph_cli.commands import mesh


def _message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(RuntimeError) as exc:
        mesh._resolve_token("friendly-coder", None)
    return str(exc.value)


def test_message_names_what_it_looked_for(monkeypatch, tmp_path) -> None:
    msg = _message(monkeypatch, tmp_path)
    assert "MESH_GATEWAY_TOKEN" in msg
    assert "friendly-coder.peer_token" in msg


def test_message_explains_the_bootstrap_case(monkeypatch, tmp_path) -> None:
    """The half that was missing: for a NEW peer the token does not exist yet."""
    msg = _message(monkeypatch, tmp_path)
    assert "BOOTSTRAPPING" in msg
    assert "does not exist yet" in msg


def test_message_points_at_the_operator_path(monkeypatch, tmp_path) -> None:
    """And says why another cell's own token will NOT work — it 403s."""
    msg = _message(monkeypatch, tmp_path)
    assert "OPERATOR" in msg
    assert "--token-file" in msg
    assert "403" in msg


def test_message_does_not_only_say_create_the_file(monkeypatch, tmp_path) -> None:
    """CAN-FAIL guard on the regression itself.

    If someone shortens this back to the old one-liner, the advice becomes
    circular again and this fails.
    """
    msg = _message(monkeypatch, tmp_path)
    assert msg.strip() != (
        "cannot resolve mesh token; set MESH_GATEWAY_TOKEN or create "
        f"{tmp_path}/.config/swarph/friendly-coder.peer_token"
    )
    assert len(msg.splitlines()) > 3, "a one-line message cannot carry the bootstrap case"
