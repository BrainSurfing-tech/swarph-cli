

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
