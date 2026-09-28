"""A defaulted name is not an explicit identity — tested THROUGH THE CALLER.

gpt-ops, reviewing #190: `brain_ask._self_name()` never returns empty (it falls
back to _DEFAULT_SELF, removed by card #402), so deriving `identity_is_explicit` from `bool(self_name)`
meant TWO things at once:

  1. a local `lab-ovh.peer_token` would outrank GBRAIN_TOKEN on any invocation
     where nothing named the cell — promoting ANOTHER CELL'S credential from
     last-resort to first choice;
  2. the resolver's own "no explicit identity" unit test became UNREACHABLE
     through this caller. It passed. The branch it covered could not occur in
     production.

>>> THAT SECOND ONE IS WHY THESE TESTS LIVE AT THE CALLER AND NOT AT THE
RESOLVER. A negative test whose subject cannot exhibit the positive is not a
test, and no amount of resolver-level coverage can see what the caller makes
reachable. <<<
"""
from __future__ import annotations

import pytest

from swarph_cli import tokens
from swarph_cli.commands import brain_ask


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(tokens.Path, "home", staticmethod(lambda: tmp_path))
    for var in ("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN", "SWARPH_SELF", "SWARPH_CELL", "SWARPH_NODE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write_peer(home, name: str, token: str):
    d = home / ".config" / "swarph"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.peer_token").write_text(token, encoding="utf-8")


def test_defaulted_identity_does_NOT_promote_another_cells_token(home, monkeypatch):
    """>>> THE BLOCKER. <<< No identity declared. (Before card #402 _self_name()
    returned the legacy default 'lab-ovh'; now it returns None.) A lab-ovh.peer_token lying on this disk must
    NOT be preferred over the brain's own env credential — nothing named a cell,
    so nothing earned precedence.
    """
    _write_peer(home, "lab-ovh", "SOMEONE-ELSES-TOKEN")
    monkeypatch.setenv("GBRAIN_TOKEN", "BRAIN-TOKEN")

    got = brain_ask._resolve_token(None, brain_ask._self_name())

    assert got == "BRAIN-TOKEN"


def test_named_identity_DOES_take_precedence(home, monkeypatch):
    """The other half — and it must be reachable through this caller too, or the
    fix is only theoretical here."""
    monkeypatch.setenv("SWARPH_SELF", "workstation-lc")
    _write_peer(home, "workstation-lc", "MY-OWN-TOKEN")
    monkeypatch.setenv("GBRAIN_TOKEN", "BRAIN-TOKEN")

    got = brain_ask._resolve_token(None, brain_ask._self_name())

    assert got == "MY-OWN-TOKEN"


def test_swarph_node_also_counts_as_naming_the_cell(home, monkeypatch):
    """_self_name() accepts either variable; explicitness must follow the same
    rule, or the two ways of naming a cell would behave differently."""
    monkeypatch.setenv("SWARPH_NODE", "workstation-lc")
    _write_peer(home, "workstation-lc", "MY-OWN-TOKEN")
    monkeypatch.setenv("GBRAIN_TOKEN", "BRAIN-TOKEN")

    got = brain_ask._resolve_token(None, brain_ask._self_name())

    assert got == "MY-OWN-TOKEN"


def test_an_undeclared_cell_never_presents_another_cells_token(home):
    """Card #402 REVERSES what this test used to pin. Before, with no brain env vars
    at all, the defaulted name 'lab-ovh' supplied lab-ovh's credential as a last
    resort: an undeclared cell authenticated AS lab-ovh. There is no default name
    now, so there is no peer-token lookup and no credential.
    """
    _write_peer(home, "lab-ovh", "SOMEONE-ELSES-TOKEN")

    assert brain_ask._self_name() is None
    assert brain_ask._resolve_token(None, brain_ask._self_name()) is None


def test_explicit_token_file_still_wins_for_both(home, monkeypatch, tmp_path):
    monkeypatch.setenv("SWARPH_SELF", "workstation-lc")
    _write_peer(home, "workstation-lc", "PEER")
    monkeypatch.setenv("GBRAIN_TOKEN", "BRAIN")
    f = tmp_path / "explicit.token"
    f.write_text("FROM-FLAG", encoding="utf-8")

    assert brain_ask._resolve_token(str(f), brain_ask._self_name()) == "FROM-FLAG"
