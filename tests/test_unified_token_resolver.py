"""One resolver, one order — and the two regressions that motivated it.

The bugs under test were reported from workstation-lc against 0.41.6 and
confirmed by lab-ovh on main:

  * a RETIRED shared MESH_GATEWAY_TOKEN in the environment silently outranked a
    VALID per-cell peer token sitting on disk;
  * `--as <cell>` selected an identity without carrying that cell's credential,
    which on a host running three cells is structurally unsatisfiable.

Every test here pins behaviour that, if it regresses, reopens one of those.
"""
from __future__ import annotations

import pytest

from swarph_cli import tokens


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate HOME so peer-token lookups cannot see the developer's real ones."""
    monkeypatch.setattr(tokens.Path, "home", staticmethod(lambda: tmp_path))
    for var in ("MESH_GATEWAY_TOKEN", "GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _write_peer(home, name: str, token: str, *, legacy: bool = False):
    d = home / (".swarph" if legacy else ".config/swarph")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.peer_token"
    p.write_text(token, encoding="utf-8")
    return p


# ── THE HEADLINE REGRESSION ──────────────────────────────────────────────────

def test_explicit_identity_beats_ambient_env(home, monkeypatch):
    """The whole point: a named cell uses ITS OWN credential, not the env's.

    This is the 401 that cost workstation-lc an afternoon — env held the token
    retired at the R1 enforce-flip while the valid one sat unread on disk.
    """
    _write_peer(home, "workstation-lc", "PEER-TOKEN-VALID")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "SHARED-TOKEN-RETIRED")

    res = tokens.resolve_token("workstation-lc", identity_is_explicit=True)

    assert res is not None
    assert res.token == "PEER-TOKEN-VALID"
    assert res.source == "peer-token"


def test_each_cell_gets_its_own_token_on_a_multi_cell_host(home, monkeypatch):
    """`--as` must carry the credential. One env var cannot serve three cells."""
    _write_peer(home, "workstation-lc", "TOKEN-LC")
    _write_peer(home, "gpt-lc", "TOKEN-GPT")
    _write_peer(home, "gpu-wsl", "TOKEN-GPU")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "ONE-GLOBAL-TOKEN")

    got = {
        name: tokens.resolve_token(name, identity_is_explicit=True).token
        for name in ("workstation-lc", "gpt-lc", "gpu-wsl")
    }

    assert got == {"workstation-lc": "TOKEN-LC",
                   "gpt-lc": "TOKEN-GPT",
                   "gpu-wsl": "TOKEN-GPU"}


# ── THE DELIBERATELY NARROW SCOPE ────────────────────────────────────────────

def test_env_still_wins_when_no_identity_was_named(home, monkeypatch):
    """Scope guard. An operator relying on env, naming no cell, is UNAFFECTED.

    If this flips, the change stops being narrow and starts being a migration.
    """
    _write_peer(home, "workstation-lc", "PEER-TOKEN")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "OPERATOR-TOKEN")

    res = tokens.resolve_token("workstation-lc", identity_is_explicit=False)

    assert res.token == "OPERATOR-TOKEN"
    assert res.source == "env"


def test_explicit_token_file_outranks_everything(home, monkeypatch, tmp_path):
    _write_peer(home, "cell", "PEER")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "ENV")
    f = tmp_path / "explicit.token"
    f.write_text("FROM-FLAG", encoding="utf-8")

    res = tokens.resolve_token("cell", str(f), identity_is_explicit=True)

    assert res.token == "FROM-FLAG"
    assert res.source == "token-file"


def test_token_file_accepts_env_style_and_bare(home, tmp_path):
    """#317's root: one flag meant a TOKEN file to mesh and a SECRETS file to
    onboard. Accepting both shapes is what lets the flag mean one thing."""
    env_style = tmp_path / "secrets.toml"
    env_style.write_text('# comment\nMESH_GATEWAY_TOKEN = "FROM-KV"\n', encoding="utf-8")
    bare = tmp_path / "bare.token"
    bare.write_text("FROM-BARE\n", encoding="utf-8")

    assert tokens.resolve_token("c", str(env_style)).token == "FROM-KV"
    assert tokens.resolve_token("c", str(bare)).token == "FROM-BARE"


# ── DIVERGENCE IS REPORTED, NEVER SILENT ─────────────────────────────────────

def test_conflicting_tokens_warn_naming_both_sources(home, monkeypatch):
    """With three resolvers in play, this warning is the only thing that would
    have shown either investigating cell the real shape."""
    _write_peer(home, "cell", "PEER")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "DIFFERENT")
    seen: list[str] = []

    tokens.resolve_token("cell", identity_is_explicit=True, warn=seen.append)

    assert len(seen) == 1
    assert "MESH_GATEWAY_TOKEN" in seen[0]
    assert "peer_token" in seen[0]


def test_identical_tokens_do_not_warn(home, monkeypatch):
    """Byte-identical copies are the common case; warning on them trains people
    to ignore the warning that matters."""
    _write_peer(home, "cell", "SAME")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "SAME")
    seen: list[str] = []

    tokens.resolve_token("cell", identity_is_explicit=True, warn=seen.append)

    assert seen == []


# ── LEGACY STORE ─────────────────────────────────────────────────────────────

def test_legacy_store_is_used_but_announced(home):
    """~/.swarph/<cell>.peer_token still exists on deployed boxes. Keep working,
    say it is deprecated — silently preferring one of two copies is how they
    drift apart unnoticed on the next rotation."""
    _write_peer(home, "cell", "LEGACY-TOKEN", legacy=True)
    seen: list[str] = []

    res = tokens.resolve_token("cell", identity_is_explicit=True, warn=seen.append)

    assert res.token == "LEGACY-TOKEN"
    assert res.source == "legacy-peer-token"
    assert any("DEPRECATED" in m for m in seen)


def test_canonical_wins_over_legacy(home):
    _write_peer(home, "cell", "CANONICAL")
    _write_peer(home, "cell", "LEGACY", legacy=True)

    res = tokens.resolve_token("cell", identity_is_explicit=True)

    assert res.token == "CANONICAL"
    assert res.source == "peer-token"


# ── ABSENCE ──────────────────────────────────────────────────────────────────

def test_returns_none_when_nothing_configured(home):
    """None, not a raise: each caller keeps its own error text. onboard's
    enumerated 'tried, in order' message is better than anything generic."""
    assert tokens.resolve_token("cell", identity_is_explicit=True) is None


def test_empty_peer_token_file_is_not_a_credential(home, monkeypatch):
    """An empty file is 'nothing here', not 'the empty token' — otherwise a
    truncated write silently sends `Authorization: Bearer `."""
    _write_peer(home, "cell", "   \n")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "ENV-TOKEN")

    res = tokens.resolve_token("cell", identity_is_explicit=True)

    assert res.token == "ENV-TOKEN"


def test_brain_ask_env_keys_are_honoured(home, monkeypatch):
    """Unifying the ORDER must not unify the VARIABLES."""
    monkeypatch.setenv("GBRAIN_TOKEN", "BRAIN")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "MESH")

    res = tokens.resolve_token(
        None, env_keys=("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN"))

    assert res.token == "BRAIN"
