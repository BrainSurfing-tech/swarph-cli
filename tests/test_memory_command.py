import json
from swarph_cli.commands import memory


def test_memory_registered_in_dispatch():
    """Test that the memory verb is registered in the CLI dispatch table."""
    from swarph_cli import main as m
    # the dispatch table maps the verb to the dotted run_ path
    table = getattr(m, "_VERB_HANDLERS", None)
    assert table is not None, "locate the dispatch dict in main.py and update this test's accessor"
    assert table["memory"] == "swarph_cli.commands.memory.run_memory"


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


def test_list_pages_calls_list_pages_tool_with_filters(monkeypatch):
    captured = {}
    pages = [{"slug": "reference_swarph_mesh", "type": "reference"},
             {"slug": "reference_okf_google", "type": "reference"}]
    _fake_post(monkeypatch, captured, pages)

    result = memory.list_pages("http://x/mcp", "tok", type_="reference", tag="auth", limit=25)

    assert [p["slug"] for p in result] == ["reference_swarph_mesh", "reference_okf_google"]
    assert captured["body"]["params"]["name"] == "list_pages"
    assert captured["body"]["params"]["arguments"] == {"type": "reference", "tag": "auth", "limit": 25}


def test_parse_links_extracts_wiki_links_dedup_ordered():
    body = ("See [[project_gbrain_shipped]] and [[reference_okf_google]].\n"
            "Also [[project_gbrain_shipped]] again and a [markdown](path) link.")
    assert memory.parse_links(body) == ["project_gbrain_shipped", "reference_okf_google"]


def test_links_reads_page_then_extracts(monkeypatch):
    captured = {}
    page = {"slug": "a", "content": "links to [[b]] and [[c]]"}
    _fake_post(monkeypatch, captured, page)
    assert memory.links("http://x/mcp", "tok", "a") == ["b", "c"]
    assert captured["body"]["params"]["name"] == "get_page"


def test_parse_links_uses_shared_okf_grammar():
    from swarph_cli.commands import memory, okf_links
    # memory.parse_links must now BE the shared parser (single grammar, no drift)
    assert memory.parse_links is okf_links.parse_okf_links
    body = "see [[project_x|the X project]] and [[ref_y#section]] and ![[embed_z]] and [docs](guide.md)"
    assert memory.parse_links(body) == ["project_x", "ref_y", "embed_z", "guide.md"]


def test_links_resolves_alias_and_heading_via_shared_parser(monkeypatch):
    from swarph_cli.commands import memory
    page = {"slug": "hub", "content": "[[a|alias]] [[b#h]] ![[c]]"}
    inner = __import__("json").dumps({"result": {"content": [{"type": "text", "text": __import__("json").dumps(page)}]}})
    monkeypatch.setattr(memory.brain_ask, "_http_post", lambda *a, **k: inner)
    assert memory.links("http://x/mcp", "tok", "hub") == ["a", "b", "c"]


def test_memory_navigate_dispatches_and_is_failsafe(monkeypatch):
    from swarph_cli.commands import mcp_server
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_endpoint", lambda: "http://x/mcp")
    monkeypatch.setattr(mcp_server.brain_ask, "_resolve_token", lambda *a, **k: "tok")
    monkeypatch.setattr(mcp_server.brain_ask, "_self_name", lambda: "lab-ovh")
    monkeypatch.setattr(mcp_server.memory, "get_page", lambda u, t, s: {"slug": s, "content": "hi"})

    assert mcp_server._memory_navigate("get", slug="foo")["slug"] == "foo"

    # fail-safe: an unexpected op or an exploding backend NEVER raises
    def boom(*a, **k):
        raise RuntimeError("gbrain down")
    monkeypatch.setattr(mcp_server.memory, "list_pages", boom)
    assert mcp_server._memory_navigate("list", tag="auth") == []
    assert mcp_server._memory_navigate("bogus") == []
