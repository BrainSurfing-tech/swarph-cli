"""A near-miss verb must ERROR, not silently become a billed LLM prompt.

Reported by peer cell `droplet` 2026-07-26: `swarph inbox` (the real verb is
`swarph mesh inbox`) is not in _VERB_HANDLERS, so it falls through to the
one-shot path, becomes `prompt="inbox"`, and bills a provider call that answers
a question nobody asked. Same for `swarph watch`, `swarph sidecar`, and every
typo of a real verb.

(`monitor` was in this list until card #122 made it a real verb -- swapped for
`sidecar`, which is likewise a SUBcommand mistaken for a top-level one.)

A bare identifier-shaped token is a MISTYPED VERB, not a prompt -- real one-shot
prompts are sentences. `--` remains the escape hatch for the rare single-word
prompt.

Run: venv/bin/python -m pytest tests/test_main_unknown_verb_guard.py -v
"""
import pytest

from swarph_cli import main as main_mod


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """Any provider call during these tests is itself the bug."""
    def boom(*a, **k):
        raise AssertionError("an unknown verb reached the billed one-shot path")
    monkeypatch.setattr(main_mod, "_run_one_shot", boom)
    return boom


@pytest.mark.parametrize("verb", ["inbox", "watch", "sidecar", "bord", "memry"])
def test_unknown_verb_errors_instead_of_billing(verb, capsys):
    rc = main_mod.main([verb])
    assert rc != 0, f"`swarph {verb}` must fail, not silently prompt an LLM"
    err = capsys.readouterr().err
    assert verb in err, "the rejected token must be named"


def test_close_typo_suggests_the_real_verb(capsys):
    main_mod.main(["bord"])
    assert "board" in capsys.readouterr().err, "difflib should surface `board`"


def test_nested_verb_miss_points_at_the_parent(capsys):
    """`inbox` has no close top-level match; the user still needs a way forward."""
    main_mod.main(["inbox"])
    err = capsys.readouterr().err
    assert "--help" in err or "swarph mesh" in err, "give the user somewhere to go"


def test_multiword_prompt_still_reaches_one_shot(monkeypatch):
    """A real prompt is a sentence -- the guard must not eat it."""
    seen = []
    monkeypatch.setattr(main_mod, "_run_one_shot", lambda args: seen.append(args.prompt) or 0)
    monkeypatch.setattr(main_mod.asyncio, "run", lambda coro: coro)
    main_mod.main(["what is the capital of France?"])
    assert seen == ["what is the capital of France?"]


def test_double_dash_escapes_the_guard(monkeypatch):
    """`swarph -- inbox` explicitly means the word, not the verb."""
    seen = []
    monkeypatch.setattr(main_mod, "_run_one_shot", lambda args: seen.append(args.prompt) or 0)
    monkeypatch.setattr(main_mod.asyncio, "run", lambda coro: coro)
    main_mod.main(["--", "inbox"])
    assert seen == ["inbox"], "the escape hatch must reach the one-shot path"


def test_known_verbs_are_unaffected(monkeypatch):
    """Regression guard: dispatch of a real verb is untouched."""
    called = []
    monkeypatch.setattr(main_mod, "_dispatch_verb", lambda v, a: called.append((v, a)) or 0)
    assert main_mod.main(["board", "list"]) == 0
    assert called == [("board", ["list"])]


def test_flags_before_a_prompt_are_not_treated_as_verbs(monkeypatch):
    seen = []
    monkeypatch.setattr(main_mod, "_run_one_shot", lambda args: seen.append(args.prompt) or 0)
    monkeypatch.setattr(main_mod.asyncio, "run", lambda coro: coro)
    main_mod.main(["--provider", "claude", "summarise the mesh board"])
    assert seen == ["summarise the mesh board"]


def test_bare_invocation_still_prints_the_banner(capsys):
    assert main_mod.main([]) == 0
    cap = capsys.readouterr()
    assert (cap.out + cap.err).strip(), "no args => banner, not an error"
