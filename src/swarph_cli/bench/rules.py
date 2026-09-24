"""Thin adapters from the rules the mesh runs today to card #941's labels.

Frozen by lab-ovh (card #941, msg 48944). Each function calls the current
rule and adds no decision of its own. That message's closing line says the
rubric (science-claude, msg 48930) wins where the two vocabularies differ,
so claim/mention are returned as CLAIM/MENTION and act-now/routine as
ACT/ROUTINE. S1's YES/NO already matches the rubric.
"""
from __future__ import annotations

from swarph_cli.commands.codegraph_hook import prompt_has_coding_keywords
from swarph_cli.dreaming.verify import _is_dated_record, _is_superseded
from swarph_cli.scripts.dm_notify_filter import _format_dm


def s1_code_question(state) -> str:
    """YES when the live codegraph gate would keep the prompt."""
    text = state["text"] if isinstance(state, dict) else state
    return "YES" if prompt_has_coding_keywords(text) else "NO"


def s2_claim_vs_mention(state) -> str:
    """mention when the extractor found no value, or the line is superseded
    or a dated record; otherwise claim. verify.py's current decision.
    """
    text = state["text"]
    asserted = state["asserted"]
    mention = (
        asserted is None
        or _is_superseded(text)
        or _is_dated_record(text, asserted)
    )
    return "MENTION" if mention else "CLAIM"


def s3_dm_triage(state) -> str:
    """routine when today's notify filter would drop the DM; otherwise act-now."""
    if _format_dm(state["dm"]) is None:
        return "ROUTINE"
    return "ACT"
