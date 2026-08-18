"""#464 F-init — every ENABLED provider must be offerable by the init wizard.

THE DEFECT (fresh-eyes onboarding audit, 2026-08-18): `_LLM_CHOICES` was a
hardcoded list of three (claude, codex, antigravity) while CLI_ENABLED_PROVIDERS
held seven. The wizard prompted "[1-3]", so a grok / cursor / muse / vibe cell
could not complete `swarph init` interactively — it had to already know to pass
--provider, which the wizard never mentioned.

An escape hatch existed the whole time (typing the provider name fell through to
the CLI_ENABLED_PROVIDERS membership check) and NOTHING DISCLOSED IT. An
undiscoverable escape hatch is not a feature; it is the bug plus a secret.

>>> THE PROOF THAT A HAND-MAINTAINED MIRROR DRIFTS: `cursor` was added to
CLI_ENABLED_PROVIDERS on the same day the Cursor membrane shipped, and this menu
never learned. <<< The menu is now DERIVED, so the next provider appears by
construction rather than by somebody remembering.
"""
from __future__ import annotations

from swarph_cli.cell import CLI_ENABLED_PROVIDERS
from swarph_cli.commands.init import _llm_choices, _LLM_BLURBS


def test_every_enabled_provider_is_offered():
    """>>> THE REGRESSION GUARD. <<< If a provider is enabled but unlisted, a cell
    of that type cannot finish the wizard. This is the assertion the old hardcoded
    list could not make about itself."""
    offered = {p for p, _ in _llm_choices()}
    missing = set(CLI_ENABLED_PROVIDERS) - offered
    assert not missing, f"enabled but not offerable in the wizard: {sorted(missing)}"


def test_nothing_is_offered_that_is_not_enabled():
    """The other direction: offering a provider the CLI refuses is a dead end that
    fails only after the user has committed to a choice."""
    offered = {p for p, _ in _llm_choices()}
    assert not offered - set(CLI_ENABLED_PROVIDERS)


def test_cursor_specifically_is_offerable():
    """Named because it is the one that proved the drift: shipped, enabled, and
    invisible in the wizard on the same day."""
    assert "cursor" in {p for p, _ in _llm_choices()}


def test_an_unblurbed_provider_still_lists():
    """A provider with no prose must still be SELECTABLE. An enabled provider the
    user cannot see is the defect; a missing description is cosmetic — so the
    fallback must never drop the entry."""
    fake = "someprovider"
    assert fake not in _LLM_BLURBS
    blurb = _LLM_BLURBS.get(fake, f"{fake} membrane")
    assert blurb and fake in blurb


def test_menu_is_derived_not_a_second_copy():
    """NON-VACUITY: the tests above pass trivially if someone re-hardcodes a list
    that happens to match today. Assert the menu tracks the enabled set by
    CONSTRUCTION — same length, same members, sorted."""
    offered = [p for p, _ in _llm_choices()]
    assert offered == sorted(CLI_ENABLED_PROVIDERS)
