"""#684 — dreaming is discoverable as a verb with the GC4i exit contract intact."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from swarph_cli.commands import dreaming as dreaming_cmd
from swarph_cli.commands import guide
from swarph_cli import main as swarph_main


def test_dreaming_is_a_registered_verb():
    assert "dreaming" in swarph_main._VERB_HANDLERS


def test_dreaming_help_names_all_four_exit_codes(capsys):
    rc = dreaming_cmd.run_dreaming(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Exit codes" in out
    assert "adjudicated" in out.lower()
    assert "findings" in out.lower() or "disagree" in out.lower()
    assert "refused" in out.lower()
    assert "adjudicated nothing" in out.lower()
    assert "SINGLE-CELL" in out
    for n in ("0", "1", "2", "3"):
        assert n in out


def test_guide_teaches_dreaming_under_memory_and_howto():
    text = guide._load_guide()
    assert "swarph dreaming run" in text
    # Memory topic
    assert "Memory and the brain" in text
    mem = text.split("## Memory and the brain", 1)[1].split("\n## ", 1)[0]
    assert "dreaming" in mem
    # I want to... table
    assert "keep my memory from rotting between sessions" in text


def test_run_against_tiny_corpus_leaves_live_byte_identical(tmp_path):
    """Accept (b): live corpus unchanged; report lands under --out."""
    corpus = tmp_path / "mem"
    corpus.mkdir()
    live = corpus / "MEMORY.md"
    body = "# index\n\n- [[fact-one]]\n"
    live.write_text(body, encoding="utf-8")
    (corpus / "fact-one.md").write_text("# Fact one\nplain claim.\n", encoding="utf-8")
    out = tmp_path / "out"
    before = {p.name: p.read_bytes() for p in corpus.iterdir()}
    rc = dreaming_cmd.run_dreaming([
        "run", "--corpus", str(corpus), "--out", str(out), "--verify-only", "--no-enrich",
    ])
    assert rc in (0, 1, 3), f"unexpected refuse rc={rc}"
    after = {p.name: p.read_bytes() for p in corpus.iterdir()}
    assert after == before, "live corpus must stay byte-identical"
    assert (out / "dreaming-report.md").exists()
    assert (out / "findings.json").exists()
