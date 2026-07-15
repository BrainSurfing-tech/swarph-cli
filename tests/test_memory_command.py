import json
from swarph_cli.commands import memory


def _fake_post(monkeypatch, captured, response_obj):
    """Stub brain_ask._http_post (the shared transport seam) to capture the
    outbound MCP request and return a canned MCP tools/call envelope."""
    def _post(url, body, token, accept="application/json", timeout=20):
        captured["url"] = url
        captured["body"] = body
        captured["token"] = token
        # gbrain MCP wraps tool output as {"result": {"content": [{"type":"text","text": <json-str>}]}}
        return json.dumps({"result": {"content": [{"type": "text", "text": json.dumps(response_obj)}]}})
    monkeypatch.setattr(memory.brain_ask, "_http_post", _post)


def test_get_page_calls_get_page_tool_and_returns_dict(monkeypatch):
    captured = {}
    page = {"slug": "project_gbrain_shipped", "type": "project", "content": "# gbrain\n$0 memory"}
    _fake_post(monkeypatch, captured, page)
    monkeypatch.setattr(memory.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(memory.brain_ask, "_resolve_token", lambda *a, **k: "tok")

    result = memory.get_page("http://x/mcp", "tok", "project_gbrain_shipped")

    assert result["slug"] == "project_gbrain_shipped"
    assert captured["body"]["method"] == "tools/call"
    assert captured["body"]["params"]["name"] == "get_page"
    assert captured["body"]["params"]["arguments"] == {"slug": "project_gbrain_shipped"}


def test_mcp_call_handles_sse_framing(monkeypatch):
    """gbrain replies over SSE (data:-prefixed) — _mcp_call must unwrap it."""
    page = {"slug": "s", "content": "x"}
    inner = json.dumps({"result": {"content": [{"type": "text", "text": json.dumps(page)}]}})
    sse = f"event: message\ndata: {inner}\n\n"
    monkeypatch.setattr(memory.brain_ask, "_http_post", lambda *a, **k: sse)
    assert memory._mcp_call("http://x/mcp", "tok", "get_page", {"slug": "s"}) == page


def test_run_memory_bad_token_file_is_fail_safe_not_a_traceback(monkeypatch, tmp_path, capsys):
    """Critical #1 regression test: a bad --token-file (missing) must NOT raise
    an uncaught FileNotFoundError — it must be caught, printed to stderr, and
    return 1, matching the fail-safe already used for the get_page call."""
    monkeypatch.setattr(memory.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    missing = tmp_path / "does-not-exist.token"

    rc = memory.run_memory(["--token-file", str(missing), "get", "some-slug"])

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


def test_run_memory_get_no_page_returns_1(monkeypatch, capsys):
    captured_call = {}
    _fake_post(monkeypatch, captured_call, None)
    monkeypatch.setattr(memory.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(memory.brain_ask, "_resolve_token", lambda *a, **k: "tok")

    rc = memory.run_memory(["get", "missing-slug"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "missing-slug" in captured.err


def test_run_memory_get_json_output(monkeypatch, capsys):
    captured_call = {}
    page = {"slug": "project_gbrain_shipped", "type": "project", "content": "# gbrain"}
    _fake_post(monkeypatch, captured_call, page)
    monkeypatch.setattr(memory.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(memory.brain_ask, "_resolve_token", lambda *a, **k: "tok")

    rc = memory.run_memory(["get", "project_gbrain_shipped", "--json"])

    assert rc == 0
    captured = capsys.readouterr()
    printed = json.loads(captured.out)
    assert printed == page
