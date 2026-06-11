"""The two compression levers. archival_split is mechanical + lossless;
shorthand is the model call (lossy, bounded to index-over-source)."""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class ArchivalResult:
    live: str       # head + pointer (stays auto-loaded)
    archive: str    # cold tail (moves to <name>)


def archival_split(source: str, boundary: str, archive_name: str) -> ArchivalResult | None:
    """Split at the first line matching `boundary`. Returns None (refuse) if no
    boundary line is found — never infer a split on a messy file. Lossless:
    live(head) + archive(tail) reconstruct the source minus the pointer line."""
    lines = source.splitlines(keepends=True)
    cut = next((i for i, ln in enumerate(lines) if re.search(boundary, ln)), None)
    if cut is None:
        return None
    head = "".join(lines[:cut])
    tail = "".join(lines[cut:])
    pointer = (f"\n## Archived\n\nHistorical content below the boundary moved to "
               f"`{archive_name}` (loaded on demand). Live content is above.\n")
    return ArchivalResult(live=head.rstrip() + "\n" + pointer, archive=tail)


SHORTHAND_SYSTEM = (
    "You compress a machine-read INDEX into telegraphic shorthand. RULES: "
    "(1) Preserve EVERY markdown link []() verbatim — they are pointers to "
    "source-of-truth and must survive. (2) Per entry, keep the title + a terse "
    "hook (~5-15 words); drop full sentences, dates, hashes, repeated framing. "
    "(3) Fluent-but-terse, NOT cryptic abbreviations. (4) One entry per input "
    "entry — never merge or drop entries. Output ONLY the compressed index."
)


async def shorthand(source: str, *, system_prompt: str = SHORTHAND_SYSTEM, chat=None) -> str:
    """Compress an index via the model. `chat` is an injectable async callable
    (messages, system_prompt, **kw)->resp with .text; defaults to swarph_mesh."""
    if chat is None:
        from swarph_mesh import ChatMessage, SwarphCall
        sc = SwarphCall(provider="claude", caller="swarph-compress")
        async def chat(messages, system_prompt=None, **kw):
            return await sc.chat(messages=messages, system_prompt=system_prompt, **kw)
        msgs = [ChatMessage(role="user", content=source)]
    else:
        # test path: lightweight message dicts are fine for the fake
        msgs = [{"role": "user", "content": source}]
    resp = await chat(msgs, system_prompt=system_prompt, temperature=0.0, max_tokens=8000)
    return resp.text
