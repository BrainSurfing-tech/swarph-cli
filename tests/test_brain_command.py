"""Tests for ``swarph brain serve`` — run the gbrain HTTP brain server.

gbrain is an external binary (the sovereign $0 semantic-memory server); the verb
is a thin launcher with swarph-blessed defaults. ``os.execvp`` is patched so no
real process is replaced.
"""

from __future__ import annotations

from unittest.mock import patch

from swarph_cli.commands import brain as br


def test_brain_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    assert "brain" in _VERB_HANDLERS
    assert _VERB_HANDLERS["brain"] == "swarph_cli.commands.brain.run_brain"


def test_brain_no_subcommand_prints_help():
    assert br.run_brain([]) == 0


def test_brain_serve_missing_gbrain_returns_2_with_hint(capsys):
    with patch.object(br, "_find_gbrain", return_value=None):
        rc = br.run_brain(["serve"])
    assert rc == 2
    assert "gbrain" in capsys.readouterr().err.lower()


def test_brain_serve_execs_gbrain_with_defaults():
    with patch.object(br, "_find_gbrain", return_value="/usr/bin/gbrain"), \
            patch("os.execvp") as ex:
        br.run_brain(["serve"])
    binary, argv = ex.call_args[0]
    assert binary == "/usr/bin/gbrain"
    # gbrain serve --http with the swarph defaults (loopback + 1yr token TTL)
    assert argv[:3] == ["/usr/bin/gbrain", "serve", "--http"]
    assert "--port" in argv and "8792" in argv
    assert "--bind" in argv and "127.0.0.1" in argv
    assert "--token-ttl" in argv and "31536000" in argv


def test_brain_serve_honors_flags():
    with patch.object(br, "_find_gbrain", return_value="gbrain"), \
            patch("os.execvp") as ex:
        br.run_brain(["serve", "--port", "9001", "--bind", "100.1.2.3", "--token-ttl", "3600"])
    _, argv = ex.call_args[0]
    assert "9001" in argv and "100.1.2.3" in argv and "3600" in argv
