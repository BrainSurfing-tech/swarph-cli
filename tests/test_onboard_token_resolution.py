

def test_help_text_names_the_resolution_order_the_code_implements():
    """>>> THE HELP TEXT IS WHERE A USER LEARNS WHERE TO PUT THEIR CREDENTIAL. <<<

    0.41.4 shipped the #243 fix (per-peer token path) while --help still
    advertised the PRE-fix order: $MESH_GATEWAY_TOKEN -> ~/.swarph/secrets.toml
    -> prompt. Both are RETIRED and absent on migrated cells, and the one
    location that works went unmentioned. A user following --help would place
    the credential in two dead paths and conclude the verb is broken — which is
    the #243 defect itself, reintroduced through the documentation.

    So bind the summary to the mechanism: every location the resolver actually
    reads must appear in the help a user is told to read.
    """
    from swarph_cli.commands import onboard

    help_text = onboard._build_parser().format_help()

    assert ".peer_token" in help_text, (
        "--help omits the per-peer credential path the resolver actually uses"
    )
    assert "SWARPH_SELF" in help_text, (
        "--help must say SWARPH_SELF is required; the verb refuses to guess"
    )


def test_onboard_persists_the_minted_peer_token(tmp_path, monkeypatch):
    """>>> THE GATEWAY MINTS THE PEER'S CREDENTIAL ONCE, IN THE REGISTER
    RESPONSE. DISCARDING IT LEAVES A PEER THAT CANNOT AUTHENTICATE. <<<

    Earlier releases read only `registered_unratified` and dropped `peer_token`.
    The server never re-mints, so the credential became unrecoverable without an
    admin revoke — and onboarding APPEARED to succeed, with the failure
    surfacing later, elsewhere, as "auth doesn't work".
    """
    import os
    from pathlib import Path
    from swarph_cli.commands import onboard

    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("SWARPH_SELF", "witness")
    (tmp_path / ".config" / "swarph").mkdir(parents=True)
    (tmp_path / ".config" / "swarph" / "witness.peer_token").write_text("wtok")

    calls = []

    def fake_post(url, body, token, *, method="POST"):
        calls.append(url)
        if url.endswith("/peers/register"):
            return 200, {"registered_unratified": True,
                         "peer_token": "MINTED-SECRET", "token_status": "minted"}
        return 200, {}

    monkeypatch.setattr(onboard, "_post_json", fake_post)
    monkeypatch.setattr(onboard, "verify_subscription_setup", lambda *a, **k: None,
                        raising=False)
    try:
        onboard.run_onboard(["newcell", "--gateway", "http://gw"])
    except Exception:
        pass  # later scaffold steps are not what this test pins

    dest = tmp_path / ".config" / "swarph" / "newcell.peer_token"
    assert dest.exists(), "the minted token was DISCARDED — peer cannot authenticate"
    assert dest.read_text().strip() == "MINTED-SECRET"
    assert oct(dest.stat().st_mode)[-3:] == "600", "credential must be 0600 from creation"
