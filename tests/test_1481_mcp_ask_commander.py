"""card #1481 phase 1 — ``swarph_ask_commander``, the MCP twin of ``swarph commander ask``.

The HTTP layer is faked UNDER the real ``mesh._post_json``, so the request the
tool sends is the request the CLI sends, byte for byte, and a gateway refusal
travels the same road it does in production. No live gateway, no real token.
"""
from __future__ import annotations

import asyncio
import email.message
import inspect
import io
import json
import secrets
import string
import urllib.error
import urllib.parse
import urllib.request

import pytest

from swarph_cli.commands import commander, mcp_server
from swarph_cli.main import main

GW = "http://gw.test:8788"
TOKEN = secrets.token_urlsafe(32)  # shaped like the gateway mint; never a literal in source
ASK = ("ship it?", ["yes", "no"], 1297)

needs_sdk = pytest.mark.skipif(mcp_server.mcp is None, reason="optional MCP SDK not installed")


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._body).encode()


def _wire(monkeypatch, reply):
    """Fake the socket under the real _post_json. reply: (status, body) or an exception."""
    sent: list[urllib.request.Request] = []

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "refused", email.message.Message(),
                io.BytesIO(json.dumps(body).encode()))
        return _Resp(status, body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


@pytest.fixture(autouse=True)
def _cell(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SWARPH_SELF", "cell-a")
    monkeypatch.setenv("MESH_GATEWAY_URL", GW)
    for var in ("SWARPH_CELL", "SWARPH_PROXY_SOCKET"):
        monkeypatch.delenv(var, raising=False)
    who: list[str] = []

    def resolve(name, token_file=None, **kw):
        who.append(name)
        return TOKEN

    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", resolve)
    return who


def _tool_result(name, args):
    out = asyncio.run(mcp_server.mcp.call_tool(name, args))
    # mcp 1.x returns (content, structured) for a dict result; older shapes return content only.
    structured = out[1] if isinstance(out, tuple) else None
    return structured if structured is not None else json.loads(out[0].text)


# --- (a) the tool exists and speaks the CLI's words ------------------------


@needs_sdk
def test_tool_is_registered_with_the_cli_arguments():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    schema = tools["swarph_ask_commander"].inputSchema
    assert set(schema["properties"]) == {"question", "options", "card"}
    assert schema["required"] == ["question"]  # options and card are optional, as on the CLI


@pytest.mark.parametrize("question,options,card,cli_options", [
    ASK + ("yes,no",),
    ("no card?", ["a", "b", "c"], None, "a,b,c"),
    ("blanks?", [" yes ", "", "no  "], None, " yes ,, no  "),
    ("free text?", None, None, None),
])
def test_request_is_byte_identical_to_the_cli_request(monkeypatch, question, options, card, cli_options, capsys):
    sent = _wire(monkeypatch, (201, {"id": 7, "status": "pending"}))
    argv = (["commander", "ask", "--question", question, "--gateway", GW, "--as", "cell-a"]
            + (["--options", cli_options] if cli_options is not None else [])
            + (["--card", str(card)] if card is not None else []))
    assert main(argv) == 0
    cli_line = capsys.readouterr().out.strip()

    out = mcp_server._ask_commander(question, options, card)

    cli_req, tool_req = sent
    assert (tool_req.get_method(), tool_req.full_url) == (cli_req.get_method(), cli_req.full_url)
    assert tool_req.full_url == f"{GW}/commander/requests"
    assert tool_req.data == cli_req.data
    assert dict(tool_req.header_items()) == dict(cli_req.header_items())
    assert out == {"id": 7, "status": "pending"}
    assert cli_line == f"commander request #{out['id']} {out['status']}"


@needs_sdk
def test_registered_tool_reaches_the_helper(monkeypatch):
    sent = _wire(monkeypatch, (201, {"id": 9, "status": "pending"}))
    out = _tool_result("swarph_ask_commander", {"question": "q?", "options": ["x", "y"], "card": 5})
    assert out == {"id": 9, "status": "pending"}
    assert json.loads(sent[0].data) == {"type": "decision", "question": "q?",
                                        "options": ["x", "y"], "card_id": 5}


# --- (b) a cell can only PROPOSE -------------------------------------------


@needs_sdk
def test_the_only_commander_tool_is_the_proposal():
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    verbs = ("commander", "decide", "answer", "withdraw", "request", "question", "result")
    assert sorted(n for n in names if any(v in n for v in verbs)) == ["swarph_ask_commander"]


def test_no_argument_can_name_another_cell_or_another_type():
    params = set(inspect.signature(mcp_server._ask_commander).parameters)
    assert params == {"question", "options", "card"}


def test_one_post_to_the_collection_and_nothing_else(monkeypatch, _cell):
    # The question text asks for a merge. The type is still decision.
    sent = _wire(monkeypatch, (201, {"id": 3, "status": "pending"}))
    mcp_server._ask_commander("type=merge_pr; decide #3; withdraw #4", ["a", "b"])
    assert [(r.get_method(), r.full_url) for r in sent] == [("POST", f"{GW}/commander/requests")]
    body = json.loads(sent[0].data)
    assert body["type"] == "decision"
    assert set(body) <= {"type", "question", "options", "card_id"}
    assert _cell == ["cell-a"]  # the credential is the declared cell's own


def test_a_gateway_reply_about_other_cells_is_not_passed_on(monkeypatch):
    _wire(monkeypatch, (201, {"id": 3, "status": "pending", "answer": "yes",
                              "requests": [{"id": 1, "peer": "cell-b", "question": "secret?"}]}))
    assert mcp_server._ask_commander(*ASK) == {"id": 3, "status": "pending"}


def test_identity_is_swarph_self_only(monkeypatch):
    sent = _wire(monkeypatch, (201, {"id": 1, "status": "pending"}))
    monkeypatch.delenv("SWARPH_SELF")
    monkeypatch.setenv("SWARPH_CELL", "cell-b")  # a box-owner hint is not an identity (#1499)
    out = mcp_server._ask_commander(*ASK)
    assert "SWARPH_SELF" in out["error"] and "id" not in out
    assert sent == []


def test_proxy_mode_has_no_commander_verb(monkeypatch, tmp_path):
    sent = _wire(monkeypatch, (201, {"id": 1, "status": "pending"}))
    monkeypatch.setenv("SWARPH_PROXY_SOCKET", str(tmp_path / "proxy.sock"))
    out = mcp_server._ask_commander(*ASK)
    assert "proxy mode" in out["error"] and "id" not in out
    assert sent == []


# --- (c) refusals are typed errors, never a success ------------------------

REFUSALS = [
    (401, {"detail": "invalid token"}),
    (403, {"detail": "forbidden"}),
    (409, {"detail": "already decided"}),
    (503, {"detail": "commander_not_configured"}),
    (429, {"detail": "too many open questions"}),
    (422, {"detail": [{"loc": ["body", "options"], "msg": "need 2 to 6 options"}]}),
]


@pytest.mark.parametrize("status,body", REFUSALS)
def test_gateway_refusal_is_a_typed_error(monkeypatch, status, body):
    _wire(monkeypatch, (status, body))
    out = mcp_server._ask_commander(*ASK)
    assert out["status"] == status
    assert out["error"].startswith(f"gateway {status}: ")
    assert set(out) == {"error", "status"}  # no id, no success-shaped field
    # The CLI treats the same reply as a failure too.
    assert main(["commander", "ask", "--question", "q", "--options", "a,b",
                 "--gateway", GW, "--as", "cell-a"]) == 1


@needs_sdk
@pytest.mark.parametrize("status,body,marker", [
    (403, {"detail": "forbidden"}, "forbidden"),
    (503, {"detail": "commander_not_configured"}, "commander_not_configured"),
])
def test_refusal_through_the_registered_tool(monkeypatch, status, body, marker):
    _wire(monkeypatch, (status, body))
    out = _tool_result("swarph_ask_commander", {"question": "q", "options": ["a", "b"]})
    assert out["status"] == status and marker in out["error"] and "id" not in out


def test_unreachable_gateway_is_an_error(monkeypatch):
    _wire(monkeypatch, urllib.error.URLError("connection refused"))
    out = mcp_server._ask_commander(*ASK)
    assert out["status"] == 0 and "connection refused" in out["error"] and "id" not in out


def test_no_gateway_configured_is_an_error(monkeypatch):
    sent = _wire(monkeypatch, (201, {"id": 1, "status": "pending"}))
    monkeypatch.delenv("MESH_GATEWAY_URL")
    out = mcp_server._ask_commander(*ASK)
    assert "MESH_GATEWAY_URL" in out["error"] and "id" not in out
    assert sent == []


# --- R1 (plan-review #5862): a 2xx reply with no integer id proposed nothing --

NO_ID = [
    (200, {}),
    (201, {}),
    (200, {"status": "pending"}),
    (201, {"id": None, "status": "pending"}),
    (201, {"id": "7", "status": "pending"}),
    (201, {"id": 7.0, "status": "pending"}),
    (201, {"id": True, "status": "pending"}),  # bool is an int subclass
    (201, [{"id": 7, "status": "pending"}]),
]
HEAD = "a" * 40
CLI_VERBS = {
    "ask": ["commander", "ask", "--question", "q?", "--options", "a,b"],
    "merge_pr": ["commander", "request", "merge_pr", "--repo", "o/r", "--pr", "12", "--head", HEAD],
    "release_finish": ["commander", "request", "release_finish", "--version", "0.9.0",
                       "--pr", "12", "--head", HEAD],
}


def _cli(verb):
    return main(CLI_VERBS[verb] + ["--gateway", GW, "--as", "cell-a"])


@pytest.mark.parametrize("status,body", NO_ID)
def test_tool_2xx_without_an_integer_id_is_a_typed_error(monkeypatch, status, body):
    _wire(monkeypatch, (status, body))
    out = mcp_server._ask_commander(*ASK)
    assert out == {"error": f"gateway {status} returned no request id; the proposal is not confirmed",
                   "status": status}


@needs_sdk
def test_registered_tool_2xx_without_an_id_is_a_typed_error(monkeypatch):
    _wire(monkeypatch, (200, {}))
    out = _tool_result("swarph_ask_commander", {"question": "q", "options": ["a", "b"]})
    assert out["status"] == 200 and "no request id" in out["error"] and "id" not in out


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("status,body", NO_ID)
def test_cli_2xx_without_an_integer_id_exits_nonzero_with_one_line(monkeypatch, capsys, verb, status, body):
    _wire(monkeypatch, (status, body))
    assert _cli(verb) == 1
    io_ = capsys.readouterr()
    assert io_.out == ""  # never "commander request #None None"
    assert io_.err == f"swarph commander: gateway {status} returned no request id; the proposal is not confirmed\n"


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("status", (200, 201))
def test_cli_well_formed_reply_is_unchanged(monkeypatch, capsys, verb, status):
    _wire(monkeypatch, (status, {"id": 7, "status": "pending"}))
    assert _cli(verb) == 0
    io_ = capsys.readouterr()
    assert (io_.out, io_.err) == ("commander request #7 pending\n", "")


# --- R2: the docstring states the deployed gateway's limits -----------------


@needs_sdk
def test_description_states_the_deployed_gateway_limits():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    desc = " ".join(tools["swarph_ask_commander"].description.split())
    for fact in ("at most 360 characters", "0 to 6 options", "at most 40 characters"):
        assert fact in desc, fact
    assert "1 to 80" not in desc and "2 to 6" not in desc  # the card's old figures


# --- S1/S2 (second-seat review #5897): a gateway-supplied string is never echoed

LEAK = secrets.token_urlsafe(32)  # a second credential the gateway puts in a reply; never the caller's own
KNOWN_STATUSES = ("pending", "answered", "approved", "rejected", "executed", "failed", "stale", "withdrawn")
# Not echoed: carries a token, is not a lowercase word, or is not a string at all.
UNSAFE_STATUS = [
    f"Authorization: Bearer {LEAK}", f"token={LEAK}", LEAK, f"pending {LEAK}", TOKEN,
    "PENDING", "pending\nforged: line", "x" * 25, "pending-2", "", 7, ["pending"], {"a": 1},
]


@pytest.mark.parametrize("status", KNOWN_STATUSES + ("queued_for_review",))
def test_tool_echoes_a_known_or_lowercase_word_status(monkeypatch, status):
    _wire(monkeypatch, (201, {"id": 7, "status": status}))
    assert mcp_server._ask_commander(*ASK) == {"id": 7, "status": status}


@pytest.mark.parametrize("status", UNSAFE_STATUS)
def test_tool_never_echoes_any_other_status(monkeypatch, status):
    _wire(monkeypatch, (201, {"id": 7, "status": status, "note": f"Authorization: Bearer {LEAK}"}))
    out = mcp_server._ask_commander(*ASK)
    assert out == {"id": 7, "status": "unknown"}
    assert LEAK not in json.dumps(out) and TOKEN not in json.dumps(out)


@needs_sdk
def test_registered_tool_does_not_echo_a_bearer_in_status(monkeypatch):
    _wire(monkeypatch, (201, {"id": 7, "status": f"Authorization: Bearer {LEAK}"}))
    out = _tool_result("swarph_ask_commander", {"question": "q", "options": ["a", "b"]})
    assert out == {"id": 7, "status": "unknown"}


# redact_text catches these shapes every time. A bare foreign token it misses 1 time in 5, so the
# bare case is only pinned for the caller's own bearer, which is removed by exact match.
LEAKY_DETAILS = [
    f"Authorization: Bearer {LEAK}", f"forbidden token={LEAK}", f"bad credential {TOKEN}",
    f"Bearer {TOKEN}", [f"Authorization: Bearer {LEAK}"],
]


@pytest.mark.parametrize("detail", LEAKY_DETAILS)
def test_tool_refusal_detail_carries_no_part_of_a_token(monkeypatch, detail):
    _wire(monkeypatch, (403, {"detail": detail}))
    out = mcp_server._ask_commander(*ASK)
    assert out["status"] == 403 and out["error"].startswith("gateway 403: ")
    assert LEAK not in json.dumps(out) and TOKEN not in json.dumps(out)


def _token_redact_text_misses() -> str:
    from swarph_cli.redact import redact_text
    for _ in range(5000):  # redact_text misses about 1 in 5 today
        tok = secrets.token_urlsafe(32)
        if tok in redact_text(f"bad credential {tok}", caller="test"):
            return tok
    pytest.skip("redact_text now catches every bare token; the exact-match removal is no longer load-bearing")


@pytest.mark.parametrize("token", [None, "", "short", b"x" * 40, 7])
def test_safe_text_tolerates_a_token_that_is_not_a_usable_credential(token):
    assert commander.safe_text("swarph commander: needs_sso", token) == "swarph commander: needs_sso"


@pytest.mark.parametrize("verb", [None, *CLI_VERBS])  # None is the MCP tool
def test_own_bearer_is_removed_even_where_redact_text_misses_it(monkeypatch, capsys, verb):
    tok = _token_redact_text_misses()
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: tok)
    _wire(monkeypatch, (403, {"detail": f"bad credential {tok}"}))
    if verb is None:
        shown = json.dumps(mcp_server._ask_commander(*ASK))
    else:
        assert _cli(verb) == 1
        io_ = capsys.readouterr()
        shown = io_.out + io_.err
    assert "bad credential" in shown and tok not in shown


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("status", UNSAFE_STATUS)
def test_cli_success_never_prints_an_unsafe_status(monkeypatch, capsys, verb, status):
    _wire(monkeypatch, (201, {"id": 7, "status": status}))
    assert _cli(verb) == 0
    io_ = capsys.readouterr()
    assert (io_.out, io_.err) == ("commander request #7 unknown\n", "")


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("detail", LEAKY_DETAILS)
def test_cli_refusal_detail_prints_no_part_of_a_token(monkeypatch, capsys, verb, detail):
    _wire(monkeypatch, (403, {"detail": detail}))
    assert _cli(verb) == 1
    io_ = capsys.readouterr()
    assert io_.out == "" and io_.err.startswith("swarph commander: ") and io_.err.count("\n") == 1
    assert LEAK not in io_.err and TOKEN not in io_.err


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("status,body", REFUSALS)
def test_cli_ordinary_refusals_print_exactly_what_main_prints(monkeypatch, capsys, verb, status, body):
    _wire(monkeypatch, (status, body))
    assert _cli(verb) == 1
    io_ = capsys.readouterr()
    # main: print(f"swarph commander: {payload.get('detail')}", file=sys.stderr)
    assert (io_.out, io_.err) == ("", f"swarph commander: {body['detail']}\n")


@pytest.mark.parametrize("status,body", REFUSALS)
def test_tool_ordinary_refusals_are_unchanged(monkeypatch, status, body):
    _wire(monkeypatch, (status, body))
    assert mcp_server._ask_commander(*ASK) == {"error": f"gateway {status}: {body['detail']}", "status": status}


# --- F1 (validate #5911): a FRAGMENT of the caller's token is still a credential ---------------
# The gateway mints swp_ + 43 url-safe characters. A refusal that echoes the last 24 characters,
# the first half or any other run of 8 or more must not reach the caller.

def _swp() -> str:
    return "swp_" + secrets.token_urlsafe(32)


def _leaks(shown: str, tok: str, k: int = 8) -> bool:
    """True when any run of k characters of tok appears in shown. Written apart from the scrub."""
    return any(tok[i:i + k] in shown for i in range(len(tok) - k + 1))


@pytest.fixture
def tok(monkeypatch):
    t = _swp()
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: t)
    return t


def _fragments(t: str) -> dict:
    h = len(t) // 2
    return {"prefix": t[:20], "suffix": t[-20:], "middle": t[12:32], "first_half": t[:h],
            "second_half": t[h:], "last_24": t[-24:], "run_of_8": t[20:28]}


# How the gateway might carry a fragment in the `detail` field.
SHAPES = {
    "bare": lambda f: f,
    "glued_to_text": lambda f: f"rejected:{f}xyz",
    "url_quoted": lambda f: urllib.parse.quote(f, safe=""),
    "inside_a_dict": lambda f: {"input": f, "loc": ["body", "question"]},
    "inside_a_list_of_dicts": lambda f: [{"loc": ["body"], "msg": f"bad {f}"}],
}


def test_every_run_of_8_or_more_is_removed_at_every_length_and_position():
    leaks = []
    for _ in range(200):  # fresh tokens, as in the validate sweep
        t = _swp()
        for n in range(8, 41):
            for start in (0, (len(t) - n) // 2, len(t) - n):
                shown = commander.safe_text(f"gateway 403: {t[start:start + n]}", t)
                if _leaks(shown, t):
                    leaks.append((n, start))
    assert leaks == []


def test_a_run_of_7_is_below_the_bar_and_a_run_of_8_is_not():
    t = "swp_" + "Qx7Lm2Vb9Zk4Rt8Wn3Pd6Hs1Jc5Gf0Ya-Eu_Bo"
    assert commander.safe_text(f"see {t[4:11]}", t) == f"see {t[4:11]}"
    assert commander.safe_text(f"see {t[4:12]}", t) == "see [REDACTED]"


@pytest.mark.parametrize("status,_body", REFUSALS)
def test_tool_refusal_detail_carries_no_fragment_of_the_token(monkeypatch, tok, status, _body):
    bad = []
    for fname, frag in _fragments(tok).items():
        for sname, shape in SHAPES.items():
            _wire(monkeypatch, (status, {"detail": shape(frag)}))
            out = mcp_server._ask_commander(*ASK)
            if _leaks(json.dumps(out), tok) or not out.get("error", "").startswith(f"gateway {status}: "):
                bad.append((fname, sname))
    assert bad == []


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("status", [401, 403, 422])
def test_cli_refusal_detail_carries_no_fragment_of_the_token(monkeypatch, capsys, tok, verb, status):
    bad = []
    for fname, frag in _fragments(tok).items():
        for sname, shape in SHAPES.items():
            _wire(monkeypatch, (status, {"detail": shape(frag)}))
            assert _cli(verb) == 1
            io_ = capsys.readouterr()
            if io_.out or _leaks(io_.err, tok) or not io_.err.startswith("swarph commander: "):
                bad.append((fname, sname))
    assert bad == []


def _lowercase_run_token():
    run = "".join(secrets.choice(string.ascii_lowercase) for _ in range(20))
    return f"swp_{secrets.token_urlsafe(8)}{run}{secrets.token_urlsafe(16)}", run


def test_a_status_that_is_a_fragment_of_the_token_is_not_echoed(monkeypatch, capsys):
    t, run = _lowercase_run_token()
    monkeypatch.setattr("swarph_cli.commands.mesh._resolve_token", lambda *a, **k: t)
    for n in (8, 14, 20):
        _wire(monkeypatch, (201, {"id": 7, "status": run[:n]}))
        assert mcp_server._ask_commander(*ASK) == {"id": 7, "status": "unknown"}
        for verb in CLI_VERBS:
            assert _cli(verb) == 0
            assert capsys.readouterr().out == "commander request #7 unknown\n"


def test_a_url_quoted_fragment_of_a_token_with_reserved_characters_is_removed():
    t = "swp_" + secrets.token_urlsafe(10) + "/+=" + secrets.token_urlsafe(10) + "/" + secrets.token_urlsafe(10)
    quoted = urllib.parse.quote(t, safe="")
    assert quoted != t
    for start in (0, 6, len(quoted) - 24):
        assert not _leaks(commander.safe_text(f"x {quoted[start:start + 24]}", quoted), quoted)
        assert not _leaks(commander.safe_text(f"x {quoted[start:start + 24]}", t), quoted)


def test_the_scrub_leaves_ordinary_replies_alone():
    ordinary = [f"swarph commander: {b['detail']}" for _s, b in REFUSALS] + [
        "swarph commander: needs_sso", "swarph commander: not_pending", "swarph commander: digest_mismatch",
        "gateway 409: commander request 7 is approved, not pending", "ValueError: bad json", *KNOWN_STATUSES,
    ]
    for _ in range(200):
        t = _swp()
        assert [commander.safe_text(x, t) for x in ordinary] == ordinary
        assert [commander.safe_status(x, t) for x in KNOWN_STATUSES] == list(KNOWN_STATUSES)


# --- R2: a missing or null status is "None", as main printed it; any other unsafe value is "unknown" ------

@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("body", [{"id": 7}, {"id": 7, "status": None}])
def test_cli_missing_or_null_status_prints_what_main_prints(monkeypatch, capsys, verb, body):
    _wire(monkeypatch, (201, body))
    assert _cli(verb) == 0
    # main: print(f"commander request #{payload.get('id')} {payload.get('status')}")
    assert capsys.readouterr().out == f"commander request #7 {body.get('status')}\n" == "commander request #7 None\n"


@pytest.mark.parametrize("body", [{"id": 7}, {"id": 7, "status": None}])
def test_tool_missing_or_null_status_is_the_string_none(monkeypatch, body):
    _wire(monkeypatch, (201, body))
    assert mcp_server._ask_commander(*ASK) == {"id": 7, "status": "None"}


def test_safe_status_maps_none_to_none_and_every_other_unsafe_value_to_unknown():
    assert commander.safe_status(None) == "None" and commander.safe_status(None, TOKEN) == "None"
    assert [commander.safe_status(v, TOKEN) for v in UNSAFE_STATUS] == ["unknown"] * len(UNSAFE_STATUS)
    assert commander.safe_status("None") == "unknown"  # a gateway's own "None" is not a word of the set


# --- a gateway id is printed only after the integer check (tool and all three verbs) -------------

HOSTILE_IDS = [
    f"Authorization: Bearer {LEAK}", LEAK, TOKEN, f"token={LEAK}", "7\nforged: line", "\x1b[2J7",
    "7", 7.0, True, None, [7], {"a": LEAK},
]


@pytest.mark.parametrize("rid", HOSTILE_IDS, ids=range(len(HOSTILE_IDS)))
def test_a_non_integer_id_is_never_echoed_by_the_tool(monkeypatch, rid):
    _wire(monkeypatch, (201, {"id": rid, "status": "pending"}))
    out = mcp_server._ask_commander(*ASK)
    assert out == {"error": commander.unconfirmed(201), "status": 201}
    assert LEAK not in json.dumps(out) and TOKEN not in json.dumps(out) and "forged" not in json.dumps(out)


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("rid", HOSTILE_IDS, ids=range(len(HOSTILE_IDS)))
def test_a_non_integer_id_is_never_echoed_by_the_cli(monkeypatch, capsys, verb, rid):
    _wire(monkeypatch, (201, {"id": rid, "status": "pending"}))
    assert _cli(verb) == 1
    io_ = capsys.readouterr()
    assert io_.out == ""
    assert io_.err == f"swarph commander: {commander.unconfirmed(201)}\n"


# --- R1 and G2: one printable line, and a token split by whitespace or a control character -------

ESCAPE = "\x1b[2J\x1b[31mAPPROVED"


@pytest.mark.parametrize("verb", CLI_VERBS)
@pytest.mark.parametrize("detail", [ESCAPE, "refused\ngranted_at=1999", "a\r\nb\tc\u2028d\x00e\x07f"])
def test_cli_refusal_detail_is_one_printable_line(monkeypatch, capsys, verb, detail):
    _wire(monkeypatch, (403, {"detail": detail}))
    assert _cli(verb) == 1
    io_ = capsys.readouterr()
    assert io_.out == "" and io_.err.count("\n") == 1 and io_.err.startswith("swarph commander: ")
    assert all(ch.isprintable() for ch in io_.err[:-1])


@pytest.mark.parametrize("detail", [ESCAPE, "refused\ngranted_at=1999", "a\r\nb\tc\u2028d\x00e\x07f"])
def test_tool_error_is_one_printable_line(monkeypatch, detail):
    _wire(monkeypatch, (403, {"detail": detail}))
    err = mcp_server._ask_commander(*ASK)["error"]
    assert err.startswith("gateway 403: ") and all(ch.isprintable() for ch in err)


SEPARATORS = (" ", "\n", "\r\n", "\t", "\x1b[0m", "\u200b", " and ")


def test_a_token_split_by_whitespace_or_a_control_character_is_still_removed(monkeypatch, capsys, tok):
    bad = []
    for sep in SEPARATORS:
        for cut in (5, 8, 20, 30, 39):
            detail = tok[:cut] + sep + tok[cut:]
            _wire(monkeypatch, (403, {"detail": detail}))
            if _leaks(json.dumps(mcp_server._ask_commander(*ASK)), tok):
                bad.append(("tool", sep, cut))
            for verb in CLI_VERBS:
                assert _cli(verb) == 1
                if _leaks(capsys.readouterr().err, tok):
                    bad.append((verb, sep, cut))
    assert bad == []


def test_the_sweep_holds_for_a_split_token_too():
    leaks = []
    for _ in range(100):
        t = _swp()
        for cut in range(4, 40):
            for sep in (" ", "\n"):
                if _leaks(commander.safe_text(f"gateway 403: {t[:cut]}{sep}{t[cut:]}", t), t):
                    leaks.append((cut, sep))
    assert leaks == []


def _has_all_three(run: str) -> bool:
    return any(c.isdigit() for c in run) and any(c.isupper() for c in run) and any(c.islower() for c in run)


def _foreign_token() -> str:
    """Another peer's token as the gateway mints one. The credential-like rule needs upper, lower
    and a digit in a run; a random 24-character slice lacks one about 1.7% of the time (a whole
    token 0.07%), so the draw keeps the slices the tests use inside the rule, not at its edge."""
    while True:
        t = _swp()
        if all(_has_all_three(r) for r in (t, t[:32], t[-32:], t[:24], t[-24:])):
            return t


def test_another_peers_token_in_a_refusal_is_removed_whole_or_cut_to_24_or_more(monkeypatch, capsys, tok):
    leaks = []
    for _ in range(100):
        f = _foreign_token()
        for shown in (f, f"x{f}y", f[:32], f[-32:], f[:24], f[-24:], f"Authorization: Bearer {f}",
                      f'{{"token": "{f}"}}'):
            if _leaks(commander.safe_text(f"gateway 403: {shown}", tok), f, 24):
                leaks.append(shown[:6])
    assert leaks == []


# --- G1: `swarph commander authorization check` prints key=value lines that scripts read ------

def _check_cli():
    return main(["commander", "authorization", "check", "merge", "--gateway", GW, "--as", "cell-a"])


def _row(**over):
    row = {"id": 3, "status": "active", "text": "merge any PR that passed both reviews",
           "granted_at": "2026-11-18T00:00:00+00:00"}
    return {"authorizations": [{**row, **over}]}


def test_check_prints_an_ordinary_row_exactly_as_main_does(monkeypatch, capsys):
    _wire(monkeypatch, (200, _row()))
    assert _check_cli() == 0
    io_ = capsys.readouterr()
    # main: f"id={id}\ntext={text or ''}\ngranted_at={granted_at or ''}\n"
    assert (io_.out, io_.err) == (
        "id=3\ntext=merge any PR that passed both reviews\ngranted_at=2026-11-18T00:00:00+00:00\n", "")


@pytest.mark.parametrize("over,expect", [
    ({"granted_at": None}, "id=3\ntext=merge any PR that passed both reviews\ngranted_at=\n"),
    ({"granted_at": ""}, "id=3\ntext=merge any PR that passed both reviews\ngranted_at=\n"),
    ({"text": None}, "id=3\ntext=\ngranted_at=2026-11-18T00:00:00+00:00\n"),
    ({"id": None}, "id=None\ntext=merge any PR that passed both reviews\ngranted_at=2026-11-18T00:00:00+00:00\n"),
    ({"granted_at": "2026-11-18T00:00:00.123456+00:00"},
     "id=3\ntext=merge any PR that passed both reviews\ngranted_at=2026-11-18T00:00:00.123456+00:00\n"),
    ({"granted_at": "2026-11-18T00:00:00Z"},
     "id=3\ntext=merge any PR that passed both reviews\ngranted_at=2026-11-18T00:00:00Z\n"),
])
def test_check_empty_and_null_fields_print_as_main_does(monkeypatch, capsys, over, expect):
    _wire(monkeypatch, (200, _row(**over)))
    assert _check_cli() == 0
    assert capsys.readouterr().out == expect


def test_check_never_prints_a_token_from_any_field(monkeypatch, capsys, tok):
    foreign = _foreign_token()
    cases = {
        "own, text after Bearer": _row(text=f"Authorization: Bearer {tok}"),
        "own, text bare": _row(text=tok),
        "own, last 24 of text": _row(text=f"see {tok[-24:]}"),
        "own, first half of text": _row(text=tok[:len(tok) // 2]),
        "own, granted_at": _row(granted_at=tok),
        "own, granted_at after a timestamp": _row(granted_at=f"2026-11-18T00:00:00+00:00 {tok}"),
        "own, string id": _row(id=f"3 {tok}"),
        "own, text split by a space": _row(text=f"{tok[:20]} {tok[20:]}"),
        "foreign, text": _row(text=foreign),
        "foreign, text after Bearer": _row(text=f"Authorization: Bearer {foreign}"),
        "foreign, granted_at": _row(granted_at=foreign),
        "foreign, string id": _row(id=foreign),
    }
    bad = []
    for name, body in cases.items():
        _wire(monkeypatch, (200, body))
        assert _check_cli() == 0
        shown = capsys.readouterr().out
        if _leaks(shown, tok) or _leaks(shown, foreign, 24):
            bad.append(name)
    assert bad == []


@pytest.mark.parametrize("text", [
    ESCAPE + "\ngranted_at=1999", "ok\ngranted_at=1999", "ok\r\nid=1", "ok\u2028granted_at=1999", "ok\x00\x07\x1b[2J",
])
def test_check_text_cannot_forge_a_line_or_carry_an_escape(monkeypatch, capsys, text):
    _wire(monkeypatch, (200, _row(text=text)))
    assert _check_cli() == 0
    out = capsys.readouterr().out
    lines = out.split("\n")
    assert lines[-1] == "" and len(lines) == 4
    assert [ln.split("=", 1)[0] for ln in lines[:3]] == ["id", "text", "granted_at"]
    assert lines[2] == "granted_at=2026-11-18T00:00:00+00:00"
    assert all(ch.isprintable() for ch in out.replace("\n", ""))


@pytest.mark.parametrize("bad_id", ["3", 3.0, 3.5, True, [3], {"a": 1}, "3\ngranted_at=1999"])
def test_check_id_must_be_an_integer(monkeypatch, capsys, bad_id):
    _wire(monkeypatch, (200, _row(id=bad_id)))
    assert _check_cli() == 0
    assert capsys.readouterr().out.startswith("id=unknown\ntext=")


@pytest.mark.parametrize("granted", ["1999", "now", "2026-11-18", "x\ngranted_at=1999", ["2026"], 7, True,
                                     "2026-11-18T00:00:00+00:00\nid=1", "2026-11-18T00:00:00+00:00 extra"])
def test_check_granted_at_must_be_a_timestamp(monkeypatch, capsys, granted):
    _wire(monkeypatch, (200, _row(granted_at=granted)))
    assert _check_cli() == 0
    assert capsys.readouterr().out.endswith("\ngranted_at=unknown\n")


def test_check_picks_the_highest_integer_id_and_does_not_crash_on_mixed_ids(monkeypatch, capsys):
    rows = [{"id": "x", "status": "active", "text": "a", "granted_at": ""},
            {"id": 5, "status": "active", "text": "b", "granted_at": ""},
            {"id": 2, "status": "active", "text": "c", "granted_at": ""}]
    _wire(monkeypatch, (200, {"authorizations": rows}))
    assert _check_cli() == 0
    assert capsys.readouterr().out == "id=5\ntext=b\ngranted_at=\n"


# --- (d) no token value in any output --------------------------------------


@pytest.mark.parametrize("reply", [
    (201, {"id": 7, "status": "pending"}),
    (403, {"detail": "forbidden"}),
    # A gateway that echoes the bearer. redact_text catches the Bearer and key=value shapes
    # every time but misses ~1 in 5 BARE urlsafe tokens, so the bare shape is not a test.
    (401, {"detail": f"Authorization: Bearer {TOKEN}"}),
    (503, {"detail": "commander_not_configured"}),
    urllib.error.URLError("down"),
])
def test_token_never_reaches_the_result_or_any_log(monkeypatch, capsys, caplog, reply):
    caplog.set_level(0)
    _wire(monkeypatch, reply)
    out = mcp_server._ask_commander(*ASK)
    io_ = capsys.readouterr()
    seen = json.dumps(out) + io_.out + io_.err + caplog.text
    assert TOKEN not in seen


def test_an_exception_that_carries_the_token_is_redacted(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"Authorization: Bearer {TOKEN}")

    monkeypatch.setattr("swarph_cli.commands.commander.post_request", boom)
    out = mcp_server._ask_commander(*ASK)
    assert "RuntimeError" in out["error"] and TOKEN not in json.dumps(out)
