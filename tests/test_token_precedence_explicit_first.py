"""#332 — an EXPLICIT --token-file must beat an ambient $MESH_GATEWAY_TOKEN.

REGRESSION SHIPPED IN 0.41.6, found by gpt-ops on a live 401 and confirmed by
two independent source reads. `onboard._resolve_token` returned the environment
token BEFORE it evaluated `token_file_arg`, so a stale shared credential in the
environment silently overrode the credential the operator named on the command
line. `ratify` and `daemon` delegate to that resolver and inherited it.

WHY IT WAS INERT UNTIL 2026-08-05: before the shared-token rotation, a stale env
value and a valid token file usually agreed, so the wrong precedence produced the
right credential. The rotation made them disagree and the defect became a 401 that
says UNAUTHORIZED rather than "I ignored the file you handed me."

The rule these tests pin: AN EXPLICIT ARGUMENT IS A DECISION, NOT A HINT. When the
operator names a credential, that credential is used or the command fails — it is
never silently replaced by ambient state, and it never falls through to a fallback
that might succeed for the wrong reason.
"""
import os
from pathlib import Path

import pytest

from swarph_cli.commands import daemon as daemon_cmd
from swarph_cli.commands import mesh as mesh_cmd
from swarph_cli.commands import onboard as onboard_cmd
from swarph_cli.commands import ratify as ratify_cmd

STALE = "stale-shared-token-retired-by-the-rotation"
WANTED = "explicit-per-peer-token-the-operator-named"

# Every resolver that takes `--token-file` as its sole positional concern.
# ratify and daemon delegate to onboard; they are listed SEPARATELY on purpose,
# because "it delegates" is a claim about today's source and this suite is what
# makes it a claim about behaviour.
DELEGATING_RESOLVERS = [
    pytest.param(onboard_cmd._resolve_token, id="onboard"),
    pytest.param(ratify_cmd._resolve_token, id="ratify"),
    pytest.param(daemon_cmd._resolve_token, id="daemon"),
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("MESH_GATEWAY_TOKEN", "SWARPH_SELF"):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("resolve", DELEGATING_RESOLVERS)
def test_explicit_raw_token_file_beats_stale_env(resolve, tmp_path, monkeypatch):
    """gpt-ops case 1: stale env + valid explicit RAW file -> explicit wins."""
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", STALE)
    f = tmp_path / "gpt-lc.peer_token"
    f.write_text(WANTED, encoding="utf-8")
    assert resolve(str(f)) == WANTED


@pytest.mark.parametrize("resolve", DELEGATING_RESOLVERS)
def test_explicit_env_style_file_beats_stale_env(resolve, tmp_path, monkeypatch):
    """gpt-ops case 2: stale env + env-STYLE explicit file -> explicit wins.

    Both file shapes must work behind the one flag; this is droplet's
    "one parser behind one flag" invariant, now also on the explicit path.
    """
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", STALE)
    f = tmp_path / "mesh.env"
    f.write_text(
        "# deployment credential\n"
        f"MESH_GATEWAY_TOKEN={WANTED}\n"
        "OTHER_VAR=irrelevant\n",
        encoding="utf-8",
    )
    assert resolve(str(f)) == WANTED


@pytest.mark.parametrize("resolve", DELEGATING_RESOLVERS)
def test_explicit_file_is_selected_even_when_it_will_be_rejected(
    resolve, tmp_path, monkeypatch
):
    """gpt-ops case 3 — THE ONE THAT DISCRIMINATES A FIX FROM A DISENGAGEMENT.

    A WRONG explicit token must still be the token that gets USED, so the
    gateway can refuse it. If the resolver quietly fell back to the env value
    here, the caller would get a 200 and read it as proof the explicit path
    works — a green result produced by the very bug. Verifying only the
    positive branch cannot tell "fixed" from "still ignoring the argument".
    """
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", STALE)
    f = tmp_path / "wrong.token"
    f.write_text("definitely-not-a-valid-token", encoding="utf-8")
    got = resolve(str(f))
    assert got == "definitely-not-a-valid-token"
    assert got != STALE, "fell through to ambient env — the defect, wearing a pass"


@pytest.mark.parametrize("resolve", DELEGATING_RESOLVERS)
def test_absent_explicit_preserves_env_fallback(resolve, monkeypatch):
    """gpt-ops case 4: no explicit file -> env still wins. The fix is NARROW.

    This is what keeps the change safe for every cell that is working today:
    behaviour only differs when --token-file was actually passed.
    """
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", STALE)
    assert resolve(None) == STALE


@pytest.mark.parametrize("resolve", DELEGATING_RESOLVERS)
def test_absent_explicit_falls_through_to_per_peer_token(
    resolve, tmp_path, monkeypatch
):
    """#243's per-peer fallback survives the reorder (no env, no explicit)."""
    monkeypatch.setenv("SWARPH_SELF", "test-cell")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    peer_dir = tmp_path / ".config" / "swarph"
    peer_dir.mkdir(parents=True)
    (peer_dir / "test-cell.peer_token").write_text(WANTED, encoding="utf-8")
    assert resolve(None) == WANTED


def test_mesh_resolver_already_correct_and_stays_correct(tmp_path, monkeypatch):
    """The CONTROL. mesh's resolver was already right.

    Without this row the suite could pass on a change that made every resolver
    agree by breaking the correct one. It also records WHICH sites were already
    right, so the next reader does not re-audit them.
    """
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", STALE)
    f = tmp_path / "raw.token"
    f.write_text(WANTED, encoding="utf-8")
    assert mesh_cmd._resolve_token("any-cell", str(f)) == WANTED


@pytest.mark.parametrize("resolve", DELEGATING_RESOLVERS)
def test_explicit_missing_file_raises_rather_than_falling_back(
    resolve, tmp_path, monkeypatch
):
    """An explicit path that does not exist is an ERROR, not a hint to guess.

    Falling back here would resurrect the defect in its most confusing form:
    a typo'd --token-file that silently authenticates as somebody else.
    """
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", STALE)
    with pytest.raises(Exception) as exc:
        resolve(str(tmp_path / "does-not-exist.token"))
    msg = str(exc.value)
    assert STALE not in msg, "must not leak the ambient token"
    # gpt-ops' REVISE on PR #187: asserting only the non-leak lets a future edit
    # delete the operator's MAP of what was skipped without failing anything.
    # The pre-#332 refusal listed all four sources because the verb kept looking;
    # it no longer does, so the contract is now "name the cause AND the doors
    # deliberately left shut". Pin the contract, not just the absence of a leak.
    assert "NO FALLBACK WAS ATTEMPTED" in msg
    for untried in ("MESH_GATEWAY_TOKEN", "secrets.toml", "peer_token"):
        assert untried in msg, f"refusal must name {untried} as deliberately untried"


@pytest.mark.parametrize("resolve", DELEGATING_RESOLVERS)
def test_explicit_scoped_file_beats_a_VALID_SHARED_ROOT_token(
    resolve, tmp_path, monkeypatch
):
    """>>> THE SECURITY INVARIANT, NOT A RELIABILITY ONE. <<<

    Every other case here uses a STALE env value, which frames the defect as
    "the wrong credential produces a 401". That framing is wrong and gpt-ops
    asked for this scenario by name so it cannot be missed.

    The ambient $MESH_GATEWAY_TOKEN is the SHARED token, and the shared token is
    ROOT at the gateway: `_is_root_token` is true for it, DM reads return an
    empty WHERE clause (the firehose), board read+write bypass role/ownership/
    grants, and it carries peer=None so nothing is attributable. So preferring
    the ambient value over an explicit per-peer file is not a wrong-credential
    bug — it is a SILENT ESCALATION FROM A SCOPED IDENTITY TO UNATTRIBUTABLE ROOT.

    While the shared value was CURRENT, that escalation succeeded and everything
    read green. It only became visible when rotation made the value stale — i.e.
    the 401 was the good outcome. An operator who names a narrow credential must
    get that credential, ESPECIALLY when a more powerful one is lying around.
    """
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "VALID-SHARED-ROOT-TOKEN-grants-firehose")
    scoped = tmp_path / "gpt-lc.peer_token"
    scoped.write_text(WANTED, encoding="utf-8")
    got = resolve(str(scoped))
    assert got == WANTED
    assert "SHARED-ROOT" not in got, (
        "escalated to the shared ROOT credential despite an explicit scoped file"
    )


def test_token_file_help_does_not_advertise_a_removed_prompt():
    """The help text is a CONTRACT WITH THE OPERATOR, and it had rotted twice.

    Both strings still described `env -> secrets -> prompt` — an order that was
    wrong after #332 AND a prompt that #243 deleted months earlier. Nothing fails
    when help text lies, which is exactly why it drifts; this is the cheapest
    thing that makes it fail.
    """
    import argparse

    from swarph_cli.commands import onboard as onboard_cmd
    from swarph_cli.commands import ratify as ratify_cmd

    for build in (onboard_cmd._build_parser, ratify_cmd._build_parser):
        helps = [
            a.help for a in build()._actions
            if getattr(a, "dest", None) == "token_file" and a.help
        ]
        assert helps, "no --token-file argument found to check"
        for text in helps:
            assert "prompt" not in text.lower().replace("never prompts", ""), (
                "help still advertises a prompt removed in #243"
            )
            assert "peer_token" in text, (
                "help omits the per-peer fallback that #243 added"
            )
