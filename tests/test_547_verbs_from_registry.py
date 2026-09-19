"""#547 -- the banner, the guide and the command registry name the SAME set of verbs.

Measured 2026-08-21: banner 8, guide 15, registry 46 -- three surfaces, three answers, none
of them looked wrong. On 2026-09-19 the registry held 52 and the guide 15. The banner now
derives from the registry; the guide is checked PER VERB against it; and the can-fail is
executed here, not described: a throwaway verb added to the registry turns the assertion red.
"""
import re
import pytest
from swarph_cli.main import _VERB_HANDLERS, _print_banner, registered_verbs
from swarph_cli.commands import guide as g


def _banner_verbs(capsys):
    _print_banner()
    err = capsys.readouterr().err
    m = re.search(r"Verbs \(\d+\)[^\n]*\n((?:  [^\n]*\n)+)", err)
    assert m, "banner carries no derived verbs block"
    return {v.strip() for v in m.group(1).replace("\n", " ").split(",") if v.strip()}


def _assert_three_surfaces_equal(capsys):
    reg = set(registered_verbs())
    ban = _banner_verbs(capsys)
    gui = g.verbs_in_guide()
    assert ban == reg, (f"banner != registry: banner-only {sorted(ban - reg)}, "
                        f"registry-only {sorted(reg - ban)}")
    assert gui == reg, (f"guide != registry: guide-only {sorted(gui - reg)}, "
                        f"registry-only {sorted(reg - gui)}")


def test_banner_guide_registry_are_the_same_set(capsys):
    _assert_three_surfaces_equal(capsys)


def test_can_fail_a_registry_verb_the_guide_does_not_teach_reds_it(capsys, monkeypatch):
    """The accept check asks for the can-fail EXECUTED: add a throwaway verb, show red, remove."""
    monkeypatch.setitem(_VERB_HANDLERS, "zz-throwaway-547", "swarph_cli.commands.guide.run_guide")
    with pytest.raises(AssertionError) as red:
        _assert_three_surfaces_equal(capsys)
    assert "zz-throwaway-547" in str(red.value)
    monkeypatch.delitem(_VERB_HANDLERS, "zz-throwaway-547")
    _assert_three_surfaces_equal(capsys)


def test_guide_resolves_every_registered_verb_per_verb(capsys):
    topics = g._split_topics(g._load_guide())
    for v in registered_verbs():
        assert g._verb_entry(topics, v), f"no guide line teaches swarph {v}"
        rc = g.run_guide([v])
        out = capsys.readouterr().out
        assert rc == 0 and out.strip(), v


def test_search_highlight_returns_the_verb_not_a_topic_list(capsys):
    rc = g.run_guide(["--search", "highlight"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "swarph highlight" in out.splitlines()[0]
