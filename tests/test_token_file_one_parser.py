"""One parser behind one flag: `--token-file` must accept the documented shape.

REGRESSION droplet hit on 0.39.1, monitor DOWN ~4 minutes:

    swarph monitor start --token-file /root/.mesh.env
    UnicodeEncodeError: 'latin-1' codec can't encode character '—'
        at http/client.py putheader -> one_value.encode('latin-1')

The em-dash was on line 5, INSIDE A COMMENT. `_read_token_file` did
`read_text().strip()` and returned the ENTIRE FILE, so comments and unrelated
variables went into the Authorization header.

Meanwhile `swarph daemon` read the SAME FLAG through onboard._resolve_token,
which had always skipped comments and parsed KEY=VALUE. TWO PARSERS, ONE FLAG --
the same one-thing-two-meanings shape as a cursor that meant both "observed" and
"woken". The daemon was never affected; only the mesh/monitor path was.

Run: venv/bin/python -m pytest tests/test_token_file_one_parser.py -v
"""
import pytest

from swarph_cli.commands import mesh

TOKEN = "abc123def456abc123def456abc123def456abc12"


def _write(tmp_path, body):
    p = tmp_path / ".mesh.env"
    p.write_text(body, encoding="utf-8")
    return p


def test_droplets_exact_file_shape(tmp_path):
    """The reproduction: env-style, with an em-dash in a comment AFTER the token."""
    p = _write(tmp_path, (
        "# droplet mesh env\n"
        f"MESH_GATEWAY_TOKEN={TOKEN}\n"
        "\n"
        "# Discord webhook for droplet — notifications\n"
        "DISCORD_WEBHOOK=https://example/x\n"
    ))
    got = mesh._read_token_file(p)
    assert got == TOKEN, "must return the TOKEN, not the file"
    got.encode("latin-1")  # must be header-safe


def test_bare_single_line_token_still_works(tmp_path):
    """The original supported shape must not regress."""
    assert mesh._read_token_file(_write(tmp_path, TOKEN + "\n")) == TOKEN


def test_bare_token_with_comments_around_it(tmp_path):
    p = _write(tmp_path, f"# a note\n\n{TOKEN}\n# trailing — note\n")
    assert mesh._read_token_file(p) == TOKEN


def test_quotes_are_stripped(tmp_path):
    assert mesh._read_token_file(_write(tmp_path, f'MESH_GATEWAY_TOKEN="{TOKEN}"\n')) == TOKEN


def test_unrelated_keys_are_skipped(tmp_path):
    p = _write(tmp_path, f"DISCORD_WEBHOOK=https://x\nMESH_GATEWAY_TOKEN={TOKEN}\n")
    assert mesh._read_token_file(p) == TOKEN


def test_non_ascii_token_is_a_NAMED_error_not_a_codec_traceback(tmp_path):
    """droplet's hardening: fail where the operator can act, naming file and line."""
    p = _write(tmp_path, f"MESH_GATEWAY_TOKEN=tok—en\n")
    with pytest.raises(RuntimeError) as e:
        mesh._read_token_file(p)
    msg = str(e.value)
    assert str(p) in msg, "name the FILE"
    assert "line 1" in msg, "name the LINE"
    assert "non-ASCII" in msg
    assert "Authorization header" in msg, "say why it cannot be used"
    assert "latin-1" not in msg.lower() or "codec" not in msg.lower(), (
        "must not read as an HTTP-layer codec error — that points at the wrong layer"
    )


def test_empty_file_names_what_was_expected(tmp_path):
    with pytest.raises(RuntimeError) as e:
        mesh._read_token_file(_write(tmp_path, "# only a comment\n\n"))
    assert "MESH_GATEWAY_TOKEN" in str(e.value), "tell the operator the accepted shape"


def test_agrees_with_the_daemons_parser(tmp_path, monkeypatch):
    """THE POINT OF THE FIX: one flag must not mean two things.

    MESH_GATEWAY_TOKEN must be cleared: onboard._resolve_token checks the ENV
    FIRST and only then the file, so with a token in the ambient environment
    this test compares the env value against the file value and fails for a
    reason that has nothing to do with parsing. It passed in isolation and
    failed in the full suite -- a test that reads ambient state is not testing
    what its name says.
    """
    from swarph_cli.commands import onboard
    monkeypatch.delenv("MESH_GATEWAY_TOKEN", raising=False)
    p = _write(tmp_path, (
        "# droplet mesh env\n"
        f"MESH_GATEWAY_TOKEN={TOKEN}\n"
        "# Discord webhook for droplet — notifications\n"
    ))
    p.chmod(0o600)
    assert mesh._read_token_file(p) == onboard._resolve_token(str(p)) == TOKEN
