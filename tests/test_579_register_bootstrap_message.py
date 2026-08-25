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


def _message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
             *, allow_peer_token: bool = False) -> str:
    """allow_peer_token=False IS the register path — mesh.py:848 is its only caller.

    These four tests used to call with the default (True), i.e. they pinned the
    register message on the send/reply/inbox/sidecar path, where it is false advice.
    Corrected after seat-A review of PR #318.
    """
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    with pytest.raises(RuntimeError) as exc:
        mesh._resolve_token("friendly-coder", None, allow_peer_token=allow_peer_token)
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


def test_the_bootstrap_paragraph_does_NOT_fire_on_send(monkeypatch, tmp_path) -> None:
    """>>> "MINTING IT IS WHAT THIS COMMAND DOES" IS FALSE FOR `mesh send`. <<<

    The paragraph shipped unconditionally in `_resolve_token`, and send (:544),
    reply (:622), inbox (:802) and sidecar (:2441) all reach it with the default
    allow_peer_token=True. A user whose token file is merely missing was told that
    the command they ran mints tokens. Measured by drop-on-meta-edge, PR #318.
    """
    msg = _message(monkeypatch, tmp_path, allow_peer_token=True)
    assert "minting it is what this command does" not in msg
    assert "BOOTSTRAPPING" not in msg
    # still actionable: it names what to do when the credential does not exist yet
    assert "MESH_GATEWAY_TOKEN" in msg and "friendly-coder.peer_token" in msg
    assert "swarph mesh register" in msg


def test_the_403_claim_is_scoped_to_gateways_that_enforce_it(monkeypatch, tmp_path) -> None:
    """The register message asserted a refusal the SHIPPED gateway does not perform.

    `swarph gateway serve`'s peers_register never calls _check_caller_binding, and
    even where that helper is called the 403 needs MESH_CALLER_BINDING_ENFORCE=1
    (default "0", warn-only). True on this mesh's deployment, false on the package's
    own gateway — so the sentence names the condition instead of promising the 403.
    """
    msg = _message(monkeypatch, tmp_path)
    assert "MESH_CALLER_BINDING_ENFORCE" in msg
    assert "gateway serve" in msg
