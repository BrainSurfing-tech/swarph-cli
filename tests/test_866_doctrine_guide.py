"""#866 — bundled doctrine topic pinned to science-claude post 42503.

Invent no wording on this side. The guide section must match the approved
fixture (a verbatim extract of card #866 post 42503). A silent fork fails.
"""

from __future__ import annotations

from pathlib import Path

from swarph_cli.commands.guide import _load_guide, _split_topics
from swarph_cli.commands import onboard as onboard_mod

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "doctrine_approved_42503.md"


def _fixture_doctrine_section() -> str:
    text = FIXTURE.read_text(encoding="utf-8")
    marker = "## Doctrine\n"
    assert marker in text, "fixture must contain a ## Doctrine section"
    return text[text.index(marker) :].rstrip() + "\n"


def test_guide_has_doctrine_topic():
    topics = _split_topics(_load_guide())
    assert "doctrine" in topics, "swarph guide --list must include doctrine"
    assert topics["doctrine"].startswith("## Doctrine")


def test_doctrine_section_matches_approved_fixture_byte_for_byte():
    """Silent fork lock: edit GUIDE and the fixture together, or this fails."""
    bundled = _split_topics(_load_guide())["doctrine"]
    approved = _fixture_doctrine_section()
    assert bundled == approved, (
        "GUIDE.md ## Doctrine drifted from tests/fixtures/doctrine_approved_42503.md "
        "(card #866 post 42503). Do not paraphrase — update both from a new "
        "science-claude card post, or restore the approved text."
    )


def test_doctrine_names_authority_and_approved_post():
    section = _split_topics(_load_guide())["doctrine"]
    assert "proven/docs/THE_TEN.md" in section
    assert "42503" in section
    assert "#866" in section or "card #866" in section
    assert "2026-09-17" in section
    # Five intake fields — not the stale four from pack_intake_standard.md.
    assert "WHO RECEIVES THE REFUSAL" in section
    assert "REJECTS" in section
    assert "Law Zero" in section
    assert "A DM IS AN OBLIGATION" in section


def test_doctrine_is_not_described_as_a_safeguard():
    """science-claude §5: THE_TEN is vocabulary, not a control."""
    section = _split_topics(_load_guide())["doctrine"].lower()
    assert "not a safeguard" in section or "not a control" in section
    # Must not claim the file stops you.
    assert "will stop you" not in section


def test_onboard_surfaces_guide_doctrine(monkeypatch, capsys):
    """A successful onboard must print the doctrine command (not paste the body)."""
    # Find the banner helper by executing the print fragment via the module's
    # known final strings — read source for the required line.
    text = Path(onboard_mod.__file__).read_text(encoding="utf-8")
    assert "swarph guide doctrine" in text, (
        "onboard must surface `swarph guide doctrine` so a clean install receives it"
    )
    assert "42503" in text
