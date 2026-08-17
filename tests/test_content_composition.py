"""Board #458 — a free-text body must never be composed through a shell.

``--content '...'`` is assembled by the sender's shell, which EXECUTES backtick
and ``$(...)`` spans and splices their (usually empty) output into the argument.
DM 24217 lost two words that way and still returned 200. These tests pin the two
halves of the fix: ``--content-file``/stdin carry the body byte-identical, and a
``--content`` value carrying shell-active characters is REFUSED by name.
"""

from __future__ import annotations

import pytest

from swarph_cli.commands import board, channel as ch, mesh
from swarph_cli.commands._content import ContentError, resolve_content

# The body that broke: backticked terms, a command substitution, an apostrophe
# (which is what ended the single-quoting and let bash reach the backticks at
# all), and a trailing newline.
HOSTILE = "the `unhonoured` wake and the `monitor` gap; $(whoami) in the sender's shell\n"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "c1")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


@pytest.fixture()
def sent(monkeypatch):
    """Capture the body mesh send would POST."""
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body)
        return (200, {"id": 1, "from_node": "c1", "to_node": "c2", "kind": "fyi"})

    monkeypatch.setattr(mesh, "_post_json", fake)
    return cap


# ── the fix: file and stdin are byte-identical ────────────────────────────────

def test_content_file_round_trips_byte_identical(tmp_path, sent):
    p = tmp_path / "body.txt"
    p.write_text(HOSTILE, encoding="utf-8")
    assert mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content-file", str(p)]) == 0
    assert sent["body"]["content"] == HOSTILE


def test_content_dash_reads_stdin_byte_identical(monkeypatch, sent):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(HOSTILE))
    assert mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content", "-"]) == 0
    assert sent["body"]["content"] == HOSTILE


def test_file_content_is_not_guarded(tmp_path):
    """The guard must NOT apply to file bodies — that is the whole point."""
    p = tmp_path / "b.txt"
    p.write_text(HOSTILE, encoding="utf-8")
    assert resolve_content(None, str(p)) == HOSTILE


def test_missing_content_file_is_a_refusal_not_a_traceback(tmp_path, sent):
    rc = mesh.run_mesh(["send", "c2", "--kind", "fyi",
                        "--content-file", str(tmp_path / "nope.txt")])
    assert rc == 1
    assert not sent, "nothing may be sent when the body could not be read"


# ── the guard: --content refuses shell-active characters, by name ─────────────

@pytest.mark.parametrize("bad,name", [
    ("a `backtick` body", "backtick"),
    ("a $(whoami) body", "command substitution"),
])
def test_content_with_shell_active_chars_is_refused(bad, name, sent, capsys):
    rc = mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content", bad])
    assert rc == 1
    err = capsys.readouterr().err
    assert name in err
    assert "--content-file" in err
    assert not sent, "a refused body must never reach the gateway"


def test_plain_content_still_sends(sent):
    assert mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content", "plain body"]) == 0
    assert sent["body"]["content"] == "plain body"


def test_content_and_content_file_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content", "x",
                       "--content-file", str(tmp_path / "b.txt")])


# ── the same treatment on the sibling verbs ───────────────────────────────────

def test_channel_post_refuses_backticks(monkeypatch, capsys):
    monkeypatch.setattr(ch, "post_json", lambda *a, **k: pytest.fail("must not post"))
    assert ch.run_channel(["post", "ann", "--content", "a `backtick`"]) == 1
    assert "backtick" in capsys.readouterr().err


def test_channel_post_content_file(tmp_path, monkeypatch):
    cap = {}
    monkeypatch.setattr(ch, "post_json",
                        lambda url, body, token, **k: (cap.update(body=body), (200, {"id": 3}))[1])
    p = tmp_path / "b.txt"
    p.write_text(HOSTILE, encoding="utf-8")
    assert ch.run_channel(["post", "ann", "--content-file", str(p)]) == 0
    assert cap["body"]["content"] == HOSTILE


def test_board_cards_add_body_refuses_backticks(monkeypatch, capsys):
    monkeypatch.setattr(board, "_post_json", lambda *a, **k: pytest.fail("must not post"))
    assert board.run_board(["cards", "add", "--project", "p", "--title", "t",
                            "--body", "a `backtick`"]) == 1
    assert "backtick" in capsys.readouterr().err


def test_board_cards_say_refuses_backticks(monkeypatch, capsys):
    monkeypatch.setattr(board, "_http_get_json", lambda *a, **k: pytest.fail("must not read"))
    assert board.run_board(["cards", "say", "1", "--content", "a `backtick`"]) == 1
    assert "backtick" in capsys.readouterr().err


def test_board_cards_add_without_body_still_works():
    """--body stays optional; the non-required group must not force one."""
    assert resolve_content(None, None, "--body") is None


def test_content_error_names_the_flag_it_came_from():
    with pytest.raises(ContentError) as exc:
        resolve_content("a `tick`", None, "--body")
    assert "--body-file" in str(exc.value)
