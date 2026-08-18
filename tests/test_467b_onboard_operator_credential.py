"""#467b — `swarph onboard <other>` must not present THIS cell's per-peer token.

FOUND BY drop-on-meta-edge (#24305) against the merged mesh-gateway fix, before
that gateway deployed — i.e. the client break was caught while it was still
theoretical, which is the only comfortable time to catch one.

THE FLOW, and it is a real command, not a constructed one:
  onboard.py :105  p.add_argument("peer", ...)   <- POSITIONAL. Arbitrary. Never
                   derived from self, and the help text's own example names a
                   DIFFERENT BOX ("e.g. razorpeter").
  onboard.py :630  canonical = KNOWN_ALIASES.get(args.peer, args.peer)
  onboard.py :667  POST /peers/register {"name": canonical, ...}
  rung 4           ~/.config/swarph/$SWARPH_SELF.peer_token  <- THIS CELL's token

So `swarph onboard razorpeter`, run on lab-ovh with no --token-file and no
MESH_GATEWAY_TOKEN, presented lab-ovh's credential to register razorpeter. It
worked because the gateway never asked. mesh-gateway e4a1f6a now binds
register's `name` to the authenticated caller, so it 403s.

>>> THE CAPABILITY IS NOT WORTH SAVING. ONBOARDING ANOTHER CELL IS AN OPERATOR
ACTION, and a per-peer token is the credential whose entire purpose is to say
"I am this ONE cell." <<< The fix is not to restore the flow; it is to refuse it
where the operator can see it.

THE CONTRAST PROVES IT WAS NEVER DECIDED, only defaulted:
  mesh.py:463   _resolve_token(self_name, args.token_file, allow_peer_token=False)
                and the registered name IS self_name
  onboard.py    no such parameter existed (and daemon + ratify IMPORT this
                resolver, which is why the new one is keyword-only with a default)
Two verbs, one endpoint, opposite credential policies.

WHY CLIENT-SIDE AND NOT JUST LETTING THE 403 LAND: the server can only name a
binding rule — "peer 'lab-ovh' may not act as register_mint_target='razorpeter'"
— which names neither the verb, nor which credential to use, nor that the CLIENT
is the thing to change. And this rung has ALREADY caused that exact confusion
once: onboard.py's own REFUSE-DO-NOT-PROMPT comment records a missing credential
being reported as a BROKEN VERB. A server-side 403 on the onboarding path
reproduces it through a new door — on the path #465 and #467 exist to keep open.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from swarph_cli.commands import onboard


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """The other rungs must be ABSENT or they short-circuit rung 4 and every test
    here passes for the wrong reason."""
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config" / "swarph").mkdir(parents=True)
    yield


def _write_peer_token(tmp_path, name, value="tok-per-peer"):
    p = tmp_path / ".config" / "swarph" / f"{name}.peer_token"
    p.write_text(value + "\n", encoding="utf-8")
    return p


def test_onboarding_ANOTHER_peer_with_our_own_token_is_REFUSED(monkeypatch, tmp_path):
    """>>> THE REGRESSION. <<< This is the razorpeter case, verbatim."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _write_peer_token(tmp_path, "lab-ovh")

    with pytest.raises(RuntimeError) as e:
        onboard._resolve_token(None, target_peer="razorpeter")

    msg = str(e.value)
    # A refusal must name the remedy, not just the rule — #468's house rule, and
    # the whole reason this lives client-side instead of as a gateway 403.
    assert "razorpeter" in msg and "lab-ovh" in msg
    assert "--token-file" in msg, "must name the operator-credential escape"
    assert "MESH_GATEWAY_TOKEN" in msg, "must name the env alternative too"
    assert "swarph onboard lab-ovh" in msg, (
        "must name the SUPPORTED use of the credential the operator already has"
    )


def test_onboarding_OURSELF_with_our_own_token_still_works(monkeypatch, tmp_path):
    """>>> NON-VACUITY, AND IT IS THE POINT OF MAKING THE CHECK TARGET-AWARE. <<<
    A blanket allow_peer_token=False would refuse this too — and this is the case
    #243 added rung 4 for. The gateway accepts it (auth.peer == req.name), so the
    client must not be stricter than the server it is protecting."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _write_peer_token(tmp_path, "lab-ovh")
    assert onboard._resolve_token(None, target_peer="lab-ovh") == "tok-per-peer"


def test_no_target_peer_means_no_check(monkeypatch, tmp_path):
    """REGRESSION GUARD for `ratify` and `daemon`, which IMPORT this resolver
    (ratify.py:82, daemon.py:154) and never call POST /peers/register. The
    parameter is keyword-only with a default precisely so their call sites keep
    working untouched — if this fails, two other verbs just broke."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _write_peer_token(tmp_path, "lab-ovh")
    assert onboard._resolve_token(None) == "tok-per-peer"


def test_an_OPERATOR_credential_onboards_anyone(monkeypatch, tmp_path):
    """The capability itself is preserved — it just requires the right credential.
    If this fails, the fix removed onboarding-others rather than re-crediting it."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok-operator")
    _write_peer_token(tmp_path, "lab-ovh")
    assert onboard._resolve_token(None, target_peer="razorpeter") == "tok-operator"


def test_explicit_token_file_outranks_the_refusal(monkeypatch, tmp_path):
    """--token-file is the escape the refusal ADVERTISES, so it must actually work
    — a refusal naming a remedy that does not function is worse than silence.
    Also guards #332's ordering: an explicit argument is a decision, not a hint."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    _write_peer_token(tmp_path, "lab-ovh")
    opf = tmp_path / "operator.token"
    opf.write_text("tok-operator-file\n", encoding="utf-8")
    assert onboard._resolve_token(str(opf), target_peer="razorpeter") == "tok-operator-file"


def test_the_refusal_does_not_fire_when_there_is_no_per_peer_token(monkeypatch, tmp_path):
    """SCOPING: with no rung-4 file the operator's problem is a MISSING credential,
    not a WRONG one. Firing the cross-name refusal here would misdiagnose it — and
    misdiagnosis on this path is the exact failure the rung was added to end."""
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    with pytest.raises(RuntimeError) as e:
        onboard._resolve_token(None, target_peer="razorpeter")
    msg = str(e.value)
    assert "no gateway credential found" in msg
    assert "refusing to onboard" not in msg
