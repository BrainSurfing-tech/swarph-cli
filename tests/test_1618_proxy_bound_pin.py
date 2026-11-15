"""One pin for the client reply bound, found in plan-review 5507 (card 1615, swarph-cli PR 742).

Offered to the builder as-is; it uses the PR's own capped_proxy fixture (the real Proxy.handle).
U+007F (DEL) is one byte in the gateway reply and six in the 0.75.0 proxy's ASCII re-encoding, so it is
the case that needs the factor 6. Bounds of 2x, 3x and 5x pass every test in the PR; this one fails them.
"""
import json
import sys

import pytest

if sys.platform not in {"linux", "darwin"}:
    pytest.skip("local proxy is POSIX-only", allow_module_level=True)

from swarph_cli import local_proxy
from swarph_cli.commands import mcp_server as m
from test_1614_proxy_inbox import capped_proxy, messages  # noqa: F401
from test_1611_mcp_proxy import proxy  # noqa: F401


def test_a_full_gateway_reply_of_del_characters_fits_the_client_bound(capped_proxy):
    ctx = capped_proxy
    ctx.messages = messages(6)
    for row in ctx.messages[:5]:
        row["content"] = "\x7f" * 200_000  # the gateway's own limit per DM
    ctx.messages[5]["content"] = "\x7f"
    body = {"messages": sorted(ctx.messages, key=lambda r: r["id"], reverse=True), "n": 6}
    short = local_proxy.MAX_LINE - len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    assert 0 < short < 200_000
    ctx.messages[5]["content"] = "\x7f" * (1 + short)
    result = m._dm_unread()
    assert "error" not in result, result
    assert result["n"] == 6
    assert max(ctx.response_sizes) == local_proxy.MAX_LINE  # the largest reply the proxy accepts
    lines = [len(json.dumps(reply).encode()) + 1 for _, reply in ctx.wire if reply.get("ok")]
    assert 5 * local_proxy.MAX_LINE < max(lines) <= local_proxy.MAX_REPLY_LINE
