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
    # #578: no gateway host ships as a default, so every send needs one
    # configured. Set once here rather than in each test that uses the fixture.
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gw.test:8788")
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
    monkeypatch.setenv("MESH_GATEWAY_URL", "http://gw.test:8788")  # #578: no host default ships
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


# ── NO GUARD: shell-active characters in --content are DELIVERED, not refused ──
#
# A first cut of #458 refused --content values containing a backtick or $(.
# drop-on-meta-edge measured the check and found it INVERTED — it fires exactly
# where quoting WORKED and is silent exactly where quoting FAILED:
#
#   single-quoted, CORRECT     'see `foo` here'  -> backticks REACH argv -> refused
#   double-quoted, CORRUPTED   "see `echo BAD`"  -> substituted, GONE    -> silent
#
# Backticks arriving intact is the SIGNATURE OF CORRECT QUOTING. So the guard's
# every refusal was a false positive by construction, and its true positive was
# unreachable — by the time argv exists, the shell has already consumed the
# evidence. These tests now assert the OPPOSITE of what they used to.

@pytest.mark.parametrize("body", [
    "a `backtick` body",
    "a $(whoami) body",
])
def test_shell_active_content_is_DELIVERED_byte_identical(body, sent):
    """>>> THE INVERSION. <<< A backtick that reached argv means the shell left it
    literal — the author typed exactly this. Refusing it would discard a correct,
    intact body, and markdown-quoted terms are ordinary in agent-composed DMs."""
    assert mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content", body]) == 0
    assert sent["body"]["content"] == body, "delivered content must be byte-identical"


def test_plain_content_still_sends(sent):
    assert mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content", "plain body"]) == 0
    assert sent["body"]["content"] == "plain body"


def test_content_and_content_file_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        mesh.run_mesh(["send", "c2", "--kind", "fyi", "--content", "x",
                       "--content-file", str(tmp_path / "b.txt")])


# ── the same treatment on the sibling verbs ───────────────────────────────────

def test_channel_post_delivers_backticks(monkeypatch):
    cap = {}
    monkeypatch.setattr(ch, "post_json",
                        lambda url, body, token, **k: (cap.update(body=body), (200, {"id": 9}))[1])
    assert ch.run_channel(["post", "ann", "--content", "a `backtick`"]) == 0
    assert cap["body"]["content"] == "a `backtick`"


def test_channel_post_content_file(tmp_path, monkeypatch):
    cap = {}
    monkeypatch.setattr(ch, "post_json",
                        lambda url, body, token, **k: (cap.update(body=body), (200, {"id": 3}))[1])
    p = tmp_path / "b.txt"
    p.write_text(HOSTILE, encoding="utf-8")
    assert ch.run_channel(["post", "ann", "--content-file", str(p)]) == 0
    assert cap["body"]["content"] == HOSTILE


def test_board_cards_add_delivers_backticks(monkeypatch):
    cap = {}
    monkeypatch.setattr(board, "_post_json",
                        lambda url, body, *a, **k: (cap.update(body=body), (200, {"id": 1}))[1])
    # numeric project id: skips the list-projects resolution round-trip, which is
    # not what this test is about.
    assert board.run_board(["cards", "add", "--project", "1", "--title", "t",
                            "--body", "a `backtick`"]) == 0
    assert cap["body"]["body"] == "a `backtick`"


# ── #650: the TITLE field gets the same escape hatch ─────────────────────────
#
# #458's remedy was applied to --body and not to --title, and a title is the
# field MOST likely to carry a command name or code identifier — the exact
# strings with backticks. #650's own card title was mangled at creation: a
# backticked `pgrep …` inside a double-quoted --title EXECUTED in the shell and
# its output was stored as the title.

def test_cards_add_title_file_is_verbatim_but_for_one_trailing_newline(tmp_path, monkeypatch):
    """A title is a one-line field; a body is not. The file's content arrives
    byte-identical EXCEPT the single trailing newline an editor or `echo`
    appends — that one is stripped by decision, not by accident."""
    cap = {}
    monkeypatch.setattr(board, "_post_json",
                        lambda url, body, *a, **k: (cap.update(body=body), (200, {"id": 1}))[1])
    p = tmp_path / "title.txt"
    p.write_text(HOSTILE, encoding="utf-8")
    assert board.run_board(["cards", "add", "--project", "1",
                            "--title-file", str(p)]) == 0
    assert cap["body"]["title"] == HOSTILE.removesuffix("\n")
    assert HOSTILE.endswith("\n"), "fixture must carry the editor newline this test is about"


def test_cards_edit_title_file_strips_one_trailing_newline(tmp_path, monkeypatch):
    cap = {}
    monkeypatch.setattr(board, "_patch_json",
                        lambda url, body, *a, **k: (cap.update(body=body),
                                                   (200, {"id": 1, "body_version": 2}))[1])
    p = tmp_path / "title.txt"
    p.write_text(HOSTILE, encoding="utf-8")
    assert board.run_board(["cards", "edit", "1", "--title-file", str(p)]) == 0
    assert cap["body"]["title"] == HOSTILE.removesuffix("\n")


def test_cards_add_refuses_two_stdin_readers(monkeypatch, capsys):
    """--title - and --body - share one stdin: the second read gets "", and ""
    is a REAL value (it clears). Refuse the collision instead of posting a
    titleless card that reports success."""
    sent = {}
    monkeypatch.setattr(board, "_post_json",
                        lambda url, body, *a, **k: (sent.update(body=body), (200, {"id": 1}))[1])
    rc = board.run_board(["cards", "add", "--project", "1", "--title", "-", "--body", "-"])
    assert rc == 1
    assert not sent, "nothing may be posted when the invocation is ambiguous"
    assert "only one field can read stdin" in capsys.readouterr().err


def test_cards_edit_refuses_two_stdin_readers(monkeypatch, capsys):
    """On edit the drained second read is worse than a lost title: the patch
    would CLEAR the stored title and print success."""
    sent = {}
    monkeypatch.setattr(board, "_patch_json",
                        lambda url, body, *a, **k: (sent.update(body=body),
                                                   (200, {"id": 1, "body_version": 2}))[1])
    rc = board.run_board(["cards", "edit", "1", "--title", "-", "--body", "-"])
    assert rc == 1
    assert not sent
    assert "only one field can read stdin" in capsys.readouterr().err


def test_cards_add_title_and_title_file_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit):
        board.run_board(["cards", "add", "--project", "1", "--title", "t",
                         "--title-file", str(tmp_path / "t.txt")])


def test_cards_add_requires_a_title_source():
    """The group is required: dropping --title must not silently produce a
    titleless card."""
    with pytest.raises(SystemExit):
        board.run_board(["cards", "add", "--project", "1"])


def test_missing_title_file_is_a_refusal_not_a_traceback(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(board, "_post_json",
                        lambda url, body, *a, **k: (sent.update(body=body), (200, {"id": 1}))[1])
    rc = board.run_board(["cards", "add", "--project", "1",
                          "--title-file", str(tmp_path / "nope.txt")])
    assert rc == 1
    assert not sent, "nothing may be posted when the title could not be read"


def test_title_help_carries_the_458_warning(capsys):
    """The hazardous flag is the one whose help must name the safe path —
    #458's warning text on --title, not only on --body. Asserted against the
    --title ENTRY specifically: the blob also contains --body's help, which
    carries the same warning, so a blob-wide assertion passes even when
    --title's help says nothing (mutation-verified)."""
    import re
    with pytest.raises(SystemExit):
        board.run_board(["cards", "add", "--help"])
    out = capsys.readouterr().out
    # Anchored on the option-list entry ("\n  --title TITLE "), NOT the usage
    # line — the usage renders the mutex group as "(--title TITLE | ...)" and
    # an unanchored search matches there first (measured).
    m = re.search(r"\n  --title TITLE(.*?)(?:\n  --|\Z)", out, re.S)
    assert m, "--title option entry not found in cards add --help"
    assert "#458" in m.group(1)
    assert "--title-file" in out


def test_resolve_content_never_rejects_a_shell_active_value():
    """Unit-level statement of the same rule: resolve_content has no reject path
    for --content. Anything the shell handed us is delivered as received."""
    assert resolve_content("a `tick` and $(sub)", None, "--content") == "a `tick` and $(sub)"


def test_board_cards_add_without_body_still_works():
    """--body stays optional; the non-required group must not force one."""
    assert resolve_content(None, None, "--body") is None


def test_content_error_names_the_flag_it_came_from(tmp_path):
    """ContentError survives for the case that IS detectable: an unreadable file.
    That failure is real, local, and knowable — unlike shell corruption, which is
    already finished by the time this process starts."""
    with pytest.raises(ContentError) as exc:
        resolve_content(None, str(tmp_path / "nope.txt"), "--body")
    assert "--body-file" in str(exc.value)
