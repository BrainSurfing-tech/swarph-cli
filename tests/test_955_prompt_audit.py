"""card #955 F1-F10. Docstrings must match the code, and the three call sites
must not pass temperature or max_tokens."""
import inspect

from swarph_cli.commands import mcp_server as mcp
from swarph_cli.commands import brain_ask
from swarph_cli.compress import levers, verify


def test_since_is_not_documented_as_reading_start():
    doc = mcp.swarph_timeline_navigate.__doc__
    assert "since reads date" in doc
    assert "since reads start" not in doc
    assert "YYYY-MM-DD" in doc
    assert "3d" in doc
    assert "[]" in doc


def test_memory_doc_names_params_and_drops_the_false_tool():
    doc = mcp.swarph_memory_navigate.__doc__
    assert "tag is the reliable filter" in doc
    assert "out|in|both" in doc
    assert "semantic search" not in doc
    assert "--depth" not in doc and "--direction" not in doc


def test_empty_list_is_documented_as_could_not_run():
    assert "could not run" in mcp.swarph_codegraph_query.__doc__
    assert "could not run" in mcp.swarph_search.__doc__


def test_add_and_describe_doc_match_the_return():
    add = mcp.swarph_add.__doc__
    assert "non-interactively" in add
    assert "installed" in add and "code" in add and "detail" in add
    desc = mcp.swarph_describe.__doc__
    for key in ("class", "publisher", "name", "version", "sha256"):
        assert key in desc
    assert '{"error"' in desc or "error" in desc


def test_those_three_call_sites_do_not_pass_sampling_knobs():
    for src in (
        inspect.getsource(verify.verify_expand),
        inspect.getsource(levers.shorthand),
        inspect.getsource(brain_ask._synthesize),
    ):
        assert "temperature" not in src
        assert "max_tokens" not in src
    expand = inspect.getsource(verify.verify_expand)
    assert "json_schema" in expand
    assert "parsed" in expand
    assert "json.loads" in expand or "_json.loads" in expand
