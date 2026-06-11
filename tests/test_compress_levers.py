from swarph_cli.compress.levers import archival_split, ArchivalResult


def test_archival_split_lossless_roundtrip():
    src = "# Manual\nlive line\n## Session 3\nold\n## Session 4\nolder\n"
    r = archival_split(src, boundary=r"^## Session", archive_name="LOG.md")
    # live = head + pointer; archive = the cold tail
    assert "live line" in r.live and "## Session 3" not in r.live
    assert "## Session 3" in r.archive and "## Session 4" in r.archive
    assert "LOG.md" in r.live                     # pointer present
    # lossless: concat archive tail back == original tail
    assert r.archive.rstrip().endswith("older")


def test_archival_refuse_no_boundary():
    src = "# Manual\njust live content, no cold tail\n"
    r = archival_split(src, boundary=r"^## Session", archive_name="LOG.md")
    assert r is None                              # no boundary -> refuse (leave)


import asyncio
from swarph_cli.compress.levers import shorthand


class _FakeResp:
    def __init__(self, text):
        self.text = text
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0


def test_shorthand_calls_model_and_returns_text():
    src = "- [a](a.md) — verbose hook with redundant prose here\n"
    async def fake_chat(messages, system_prompt=None, **kw):
        return _FakeResp("- [a](a.md) — terse hook\n")
    out = asyncio.run(shorthand(src, system_prompt="x", chat=fake_chat))
    assert out == "- [a](a.md) — terse hook\n"
