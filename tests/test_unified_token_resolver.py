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


def test_empty_peer_token_file_is_not_a_credential(home):
    """An empty file is 'nothing here', not 'the empty token' — otherwise a
    truncated write silently sends `Authorization: Bearer `.

    NOTE this test previously asserted a fallback to $MESH_GATEWAY_TOKEN. That
    assertion was WRONG and is reversed below — see the unflattering-branch
    section for why a fallback that succeeds is the defect wearing a pass.
    """
    _write_peer(home, "cell", "   \n")

    assert tokens.resolve_token("cell", identity_is_explicit=True) is None


# ── THE UNFLATTERING BRANCH ──────────────────────────────────────────────────
# lab-ovh's review lens on #190 (DM 17155, item 2), turned on this module, found
# a real defect in it: an earlier revision fell through to the AMBIENT credential
# when the NAMED identity's file existed but was unusable. That fallback
# SUCCEEDS — the call works, the operator believes it ran as the named cell, and
# the only trace is a warning nobody reads. It is the exact escalation shape this
# PR exists to close, reintroduced one branch lower down. These keep it closed.

def test_unusable_peer_file_does_NOT_fall_back_to_ambient(home, monkeypatch):
    """Naming a cell is a decision. A present-but-empty credential for that cell
    must NOT silently hand the caller the ambient one."""
    _write_peer(home, "cell", "")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "AMBIENT")

    res = tokens.resolve_token("cell", identity_is_explicit=True)

    assert res is None, "fell back to the ambient credential for a NAMED identity"


def test_the_refusal_says_no_fallback_was_attempted(home, monkeypatch):
    """A refusal that does not say what it declined to try reads as a bug."""
    _write_peer(home, "cell", "")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "AMBIENT")
    seen: list[str] = []

    tokens.resolve_token("cell", identity_is_explicit=True, warn=seen.append)

    assert any("NO AMBIENT FALLBACK" in m for m in seen)


def test_the_refusal_NAMES_THE_FILE_it_tells_you_to_fix(home, monkeypatch):
    """The message says "Fix the file" — so it must say WHICH file.

    >>> THIS IS THE TEST THE PREVIOUS ONE COULD NOT BE. <<< The refusal above
    asserts only that the phrase "NO AMBIENT FALLBACK" appears, and it passed
    unchanged while the path rendered as the literal "(None)" — `peer_src` is
    assigned only on a SUCCESSFUL read, and this branch is reachable only when
    every read FAILED, so it was None on every execution. A defect that ships in
    the cannot-evaluate branch is invisible to an assertion about the happy
    string; only asserting on the payload the operator actually needs can see it.

    Found by lab-ovh reviewing #190, reproduced by execution before being
    written down. Guard: `str(None)` must never satisfy this.
    """
    peer = _write_peer(home, "cell", "")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "AMBIENT")
    seen: list[str] = []

    tokens.resolve_token("cell", identity_is_explicit=True, warn=seen.append)

    refusals = [m for m in seen if "NO AMBIENT FALLBACK" in m]
    assert refusals, "the refusal warning did not fire at all"
    assert all("None" not in r.split("NO AMBIENT FALLBACK")[0] for r in refusals), (
        "the refusal rendered its path as 'None' — it names no file to fix"
    )
    assert any(str(peer) in r for r in refusals), (
        f"the refusal must name {peer}; got: {refusals}"
    )


def test_a_MISSING_peer_file_still_falls_through(home, monkeypatch):
    """The boundary. Nothing was named-and-present, so there is no decision to
    honour — this must keep working or every unprovisioned cell breaks."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "AMBIENT")

    res = tokens.resolve_token("cell", identity_is_explicit=True)

    assert res.token == "AMBIENT"


def test_a_WRONG_peer_token_is_SELECTED_not_worked_around(home, monkeypatch):
    """>>> A FALLBACK THAT SUCCEEDS IS THE DEFECT WEARING A PASS. <<<

    If the named cell's credential is present and parseable but WRONG, it must
    still be the one selected — so the gateway rejects it and the operator learns
    the file is wrong. Quietly substituting a credential that happens to work
    turns a visible 403 into a silent identity swap.
    """
    _write_peer(home, "cell", "WRONG-BUT-PARSEABLE")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "AMBIENT-THAT-WOULD-WORK")

    res = tokens.resolve_token("cell", identity_is_explicit=True)

    assert res.token == "WRONG-BUT-PARSEABLE"
    assert res.source == "peer-token"


# ── PRECEDENCE COMPLETENESS (lab's item 1) ───────────────────────────────────
# Asserted explicitly rather than trusting that the happy paths imply the rest.

@pytest.mark.parametrize("has_file,has_peer,has_env,explicit,expect", [
    (True,  True,  True,  True,  "FILE"),
    (True,  True,  True,  False, "FILE"),
    (True,  False, False, True,  "FILE"),
    (False, True,  True,  True,  "PEER"),
    (False, True,  True,  False, "ENV"),   # identity NOT named -> ambient wins
    (False, True,  False, True,  "PEER"),
    (False, True,  False, False, "PEER"),
    (False, False, True,  True,  "ENV"),
    (False, False, True,  False, "ENV"),
    (False, False, False, True,  None),
])
def test_precedence_matrix(home, monkeypatch, tmp_path,
                           has_file, has_peer, has_env, explicit, expect):
    arg = None
    if has_file:
        f = tmp_path / "explicit.token"
        f.write_text("FILE", encoding="utf-8")
        arg = str(f)
    if has_peer:
        _write_peer(home, "cell", "PEER")
    if has_env:
        monkeypatch.setenv("MESH_GATEWAY_TOKEN", "ENV")

    res = tokens.resolve_token("cell", arg, identity_is_explicit=explicit)

    assert (res.token if res else None) == expect


def test_brain_ask_env_keys_are_honoured(home, monkeypatch):
    """Unifying the ORDER must not unify the VARIABLES."""
    monkeypatch.setenv("GBRAIN_TOKEN", "BRAIN")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "MESH")

    res = tokens.resolve_token(
        None, env_keys=("GBRAIN_TOKEN", "SWARPH_BRAIN_TOKEN"))

    assert res.token == "BRAIN"
