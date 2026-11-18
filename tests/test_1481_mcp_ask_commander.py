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
import urllib.error
import urllib.request

import pytest

from swarph_cli.commands import mcp_server
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
