"""#243 — `swarph onboard` resolved ONLY the credential the R1 migration retired.

Measured on lab-ovh 2026-08-03: $MESH_GATEWAY_TOKEN UNSET, ~/.swarph/secrets.toml
ABSENT, and 10+ files present at ~/.config/swarph/<peer>.peer_token. Every other
verb resolves the per-peer file; onboard (and ratify, which re-exports it, and
daemon, which COPIED it) were left on the old path.

The gateway never required the shared token — POST /peers/register accepts a
per-peer token and mints (measured). So this was never permissions, only a lookup
that never learned.
"""
import pytest

from swarph_cli.commands import onboard


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch, tmp_path):
    """The bug is invisible on a box that still has the old credential, so the
    fixture removes BOTH retired sources — which is the state of every cell
    since the migration."""
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("SWARPH_SELF", raising=False)
    monkeypatch.setattr(onboard.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _write_peer_token(home, peer, value="tok-per-peer"):
    d = home / ".config" / "swarph"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{peer}.peer_token").write_text(value, encoding="utf-8")


def test_it_resolves_the_PER_PEER_token(_no_ambient_credentials, monkeypatch):
    """>>> THE BUG. <<< With no shared token anywhere — the state of every cell
    since R1 — onboard must find the per-peer file rather than prompting."""
    _write_peer_token(_no_ambient_credentials, "lab-ovh")
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    assert onboard._resolve_token(None) == "tok-per-peer"


def test_it_REFUSES_instead_of_prompting_when_nothing_is_found(_no_ambient_credentials):
    """getpass on a non-tty is a guaranteed EOFError, and this verb runs from
    scripts, cron and spawned cells. The refusal must NAME the places it looked —
    the prompt is what made a missing credential look like a broken verb."""
    with pytest.raises(RuntimeError) as e:
        onboard._resolve_token(None)
    msg = str(e.value)
    assert "MESH_GATEWAY_TOKEN" in msg
    assert "peer_token" in msg
    assert "SWARPH_SELF IS UNSET" in msg, (
        "an unset SWARPH_SELF must be named as unset — guessing a peer name makes "
        "a cell hunt ANOTHER CELL'S token and blame the credential")


def test_it_does_NOT_guess_a_peer_name(_no_ambient_credentials):
    """MEASURED 2026-07-29 on 6 of 6 cells: a default self-name makes a cell look
    for lab-ovh's token, find nothing, and report a TOKEN fault. Harmless on
    lab-ovh, silently wrong everywhere else."""
    _write_peer_token(_no_ambient_credentials, "lab-ovh")   # present but not ours
    with pytest.raises(RuntimeError):
        onboard._resolve_token(None)                        # SWARPH_SELF unset


def test_the_SHARED_token_still_wins_when_no_peer_token_exists(
        _no_ambient_credentials, monkeypatch):
    """NON-VACUITY, in the form that SURVIVES the precedence change.

    #243's guarantee was "an operator with a working shared-token setup must be
    unaffected, or the fix trades one broken population for another". Still
    right, still tested — but its scope narrowed. Where no per-cell credential
    exists, the shared token is the only credential and must win.
    """
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok-shared")
    assert onboard._resolve_token(None) == "tok-shared"


def test_per_peer_token_now_OUTRANKS_the_shared_one_when_the_cell_is_named(
        _no_ambient_credentials, monkeypatch):
    """>>> THIS REVERSES A DELIBERATE ORDERING FROM #243, ON PURPOSE. <<<

    #243 placed the per-peer lookup AFTER $MESH_GATEWAY_TOKEN so the change would
    be purely additive. That was the safe call then and it is the wrong call now,
    for a reason #243 could not have seen: the R1 enforce-flip RETIRED the shared
    token, so on a box still carrying it in the environment "additive" means
    authenticating with a corpse while the valid credential sits unread on disk.
    Reported from workstation-lc against 0.41.6, reproduced by lab-ovh on main.

    And the sharper half: a process-global env var cannot vary per cell, so on a
    host running several (workstation-lc runs three, with three distinct peer
    tokens) `--as <cell>` selected an identity WITHOUT carrying its credential —
    every call authenticated as whatever the env held, whichever cell was named,
    and nothing warned.

    The flip is scoped to "the identity is explicit" precisely to keep as much of
    #243's additivity as possible: an operator who names no cell is unaffected,
    which the test above still asserts.
    """
    _write_peer_token(_no_ambient_credentials, "lab-ovh", "tok-per-peer")
    monkeypatch.setenv("SWARPH_SELF", "lab-ovh")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok-shared-RETIRED")
    assert onboard._resolve_token(None) == "tok-per-peer"


def test_ratify_and_daemon_resolve_through_THE_SAME_function():
    """>>> ratify RE-EXPORTED and was fixed for free; daemon COPIED and was not.
    <<< A copy made to keep two things identical keeps them identical only until
    one is fixed. Both now delegate, so the next credential migration cannot
    break one and not the others."""
    from swarph_cli.commands import ratify, daemon
    import inspect
    for mod in (ratify, daemon):
        src = inspect.getsource(mod._resolve_token)
        assert "from swarph_cli.commands.onboard import _resolve_token" in src, (
            f"{mod.__name__} does not delegate — it has its own copy and will "
            "diverge at the next migration")
