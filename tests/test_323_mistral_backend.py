"""#323 — `swarph bench` could PRICE mistral and could not CALL it.

The price table already carried 90 mistral/devstral entries (including
`mistral-medium-3-5` and `devstral-small`); `backends.py` had no adapter, so
the cost half of the question was answerable and the quality half was not
reachable at all.

These tests exercise the paths that run WITHOUT a network or an API key —
credential preflight, the two failure branches, and the honest-zero claim on
`tokens_thought`. A live call is deliberately not tested here: it would be a
metered charge on every CI run, and mistral's lane is now pay-as-you-go.
"""
import sys
import types

import pytest

from swarph_cli.bench.backends import BackendResult, MeteredMistralBackend


def test_missing_creds_names_the_env_var_it_wants():
    """preflight must say WHICH credential, not just that one is missing.

    `runner.preflight` calls this BEFORE any network call so a missing key
    surfaces as one clear warning instead of a mid-run 401 traceback.
    """
    assert MeteredMistralBackend().missing_creds() == ["MISTRAL_API_KEY"]
    assert MeteredMistralBackend(api_key="k").missing_creds() == []


def test_env_var_satisfies_preflight(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "from-env")
    assert MeteredMistralBackend().missing_creds() == []
    assert MeteredMistralBackend().credentials_ok() is True


def test_a_missing_dependency_is_an_ERROR_not_an_empty_answer(monkeypatch):
    """>>> THE DISTINCTION THIS WHOLE ADAPTER TURNS ON. <<<

    A backend that returned `text=""` with no error would score as a WRONG
    ANSWER in a benchmark, not as a failed call — and cost-per-useful-answer
    would silently count it in the denominator's failure column for the wrong
    reason. Every non-answer must carry `error`.
    """
    monkeypatch.setitem(sys.modules, "mistralai", None)  # force ImportError
    r = MeteredMistralBackend(api_key="k").generate("mistral-medium-3-5", "hi")
    assert r.error and "mistralai" in r.error
    assert r.text == ""
    assert r.estimated is False, "a failure is not an ESTIMATE, it is a failure"


def test_no_api_key_is_an_ERROR_not_an_empty_answer(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    fake = types.ModuleType("mistralai")
    fake.Mistral = object
    monkeypatch.setitem(sys.modules, "mistralai", fake)
    r = MeteredMistralBackend().generate("mistral-medium-3-5", "hi")
    assert r.error and "MISTRAL_API_KEY" in r.error
    assert r.text == ""


def test_provider_exception_surfaces_with_latency_and_does_not_raise(monkeypatch):
    """A failed arm must not abort a multi-arm run — and must not vanish.

    Latency is still recorded: how long a call took BEFORE failing is real
    data (a 30s timeout and an instant 401 are different failures).
    """
    class Boom:
        def __init__(self, api_key=None): self.chat = self
        def complete(self, **kw): raise RuntimeError("429 rate limited")
    fake = types.ModuleType("mistralai"); fake.Mistral = Boom
    monkeypatch.setitem(sys.modules, "mistralai", fake)
    r = MeteredMistralBackend(api_key="k").generate("devstral-small", "hi")
    assert "429" in r.error
    assert r.latency_s >= 0.0
    assert isinstance(r, BackendResult)


def _fake_ok(captured):
    class Msg:  content = "answer text"
    class Choice: message = Msg()
    class Usage:  prompt_tokens = 11; completion_tokens = 7
    class Resp:   choices = [Choice()]; usage = Usage()
    class Client:
        def __init__(self, api_key=None): self.chat = self
        def complete(self, **kw): captured.update(kw); return Resp()
    m = types.ModuleType("mistralai"); m.Mistral = Client
    return m


def test_usage_is_MEASURED_and_thought_is_an_honest_zero(monkeypatch):
    """>>> DO NOT 'FIX' tokens_thought BY ESTIMATING IT. <<<

    Mistral's chat completions return prompt/completion counts and NOTHING
    for thinking tokens. So in/out are MEASURED (`estimated=False`) while
    thought is genuinely ABSENT. Reporting a guessed value here would make a
    provider that does not report thinking indistinguishable from a model
    that did none — and cost-per-useful-answer compares lanes on exactly that.
    """
    cap = {}
    monkeypatch.setitem(sys.modules, "mistralai", _fake_ok(cap))
    r = MeteredMistralBackend(api_key="k").generate("mistral-medium-3-5", "q")
    assert (r.tokens_in, r.tokens_out) == (11, 7)
    assert r.tokens_thought == 0
    assert r.estimated is False, "in/out came from usage; they are not estimates"
    assert r.total_tokens == 18
    assert r.text == "answer text"


def test_system_threads_as_a_SYSTEM_MESSAGE_not_a_prompt_prepend(monkeypatch):
    """Mistral has no `system_instruction` field, so the provider-shaped
    equivalent is a leading system-role message. Pinning this because the
    lazy alternative — gluing `system` onto the front of `prompt` — changes
    what the model is told AND corrupts the prompt-token count the whole
    cost metric is built on.
    """
    cap = {}
    monkeypatch.setitem(sys.modules, "mistralai", _fake_ok(cap))
    MeteredMistralBackend(api_key="k").generate("devstral-small", "USERQ", system="SYS")
    msgs = cap["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1] == {"role": "user", "content": "USERQ"}
    assert "SYS" not in msgs[1]["content"], "system leaked into the user prompt"


def test_no_system_sends_no_system_message(monkeypatch):
    cap = {}
    monkeypatch.setitem(sys.modules, "mistralai", _fake_ok(cap))
    MeteredMistralBackend(api_key="k").generate("devstral-small", "USERQ")
    assert [m["role"] for m in cap["messages"]] == ["user"]


def test_the_three_bench_arms_are_actually_priced():
    """The card's premise, asserted rather than assumed — WHEN IT CAN BE.

    #323 rests on "bench can already price mistral". That claim came from a
    grep, and a grep over that file also matches `e5-mistral-7b-instruct` and
    bedrock pixtral — models that are NOT Mistral AI's chat lane. So the two
    metered arms are pinned by exact key, and the premise cannot rot silently.

    >>> BUT THE PRICE TABLE IS A GENERATED CACHE AND IS GITIGNORED. <<<
    (.gitignore:9 — `refresh_prices.py` writes it.) It exists on a developer box
    that has run the refresh and CAN NEVER EXIST IN CI, which clones fresh. The
    first version of this test read it unconditionally: it verified the premise
    ON THE ONE MACHINE THAT WROTE IT and failed on all four CI legs.

    A PREMISE-CHECK THAT ONLY RUNS WHERE THE ANSWER IS ALREADY KNOWN IS NOT A
    CHECK. So this SKIPS WITH A NAMED REASON when the cache is absent — an
    inability to evaluate, made visible — rather than passing vacuously or
    failing on an environment fact. The always-runnable half of the premise is
    the test below, which needs no cache.
    """
    import json
    from pathlib import Path
    import pytest
    import swarph_cli.bench as bench_pkg

    cache = Path(bench_pkg.__file__).parent / "data" / "llm_prices.json"
    if not cache.exists():
        pytest.skip(
            "llm_prices.json is a GENERATED, GITIGNORED cache (refresh_prices.py) "
            "and is absent here — CI clones fresh, so this premise is UNVERIFIABLE "
            "in this environment. Not a pass: an unevaluated claim. Run "
            "`python -m swarph_cli.bench.refresh_prices` to check it locally."
        )
    prices = json.loads(cache.read_text())["prices"]
    for arm in ("mistral-medium-3-5", "devstral-small"):
        assert arm in prices, f"{arm} is not priced; #323's premise no longer holds"


def test_the_pricing_PATH_works_for_a_mistral_key_without_the_cache(monkeypatch):
    """The half of the premise that IS verifiable everywhere.

    The generated table's CONTENTS cannot be checked in CI, but the RESOLVER can.
    `prices.load()` is monkeypatched with a controlled table so this exercises
    the REAL `cost_usd` -> `lookup` path with no cache on disk.

    (The first version of this test computed the arithmetic itself and asserted
    its own multiplication — it never called the resolver at all, and would have
    passed with `cost_usd` deleted. Caught by reading it back before pushing.)
    """
    from swarph_cli.bench import prices

    monkeypatch.setattr(prices, "load",
                        lambda force=False: {"mistral-medium-3-5": {"in": 2.0, "out": 6.0}})

    cost = prices.cost_usd("mistral-medium-3-5", tokens_in=1_000_000,
                           tokens_thought=0, tokens_out=1_000_000)
    assert cost == 8.0, f"expected 2.0 + 6.0 per Mtok, got {cost}"

    # >>> THE CONTROL THAT MAKES IT MEAN SOMETHING. <<< A resolver that returned
    # the FALLBACK bucket for everything would also produce a non-zero number
    # here, so a bare `cost > 0` proves nothing. An UNKNOWN key must resolve
    # DIFFERENTLY from the priced one, or the exact-hit path is not being taken.
    fallback = prices.cost_usd("definitely-not-a-model-323", tokens_in=1_000_000,
                               tokens_thought=0, tokens_out=1_000_000)
    assert fallback != cost, (
        "an unknown model priced identically to the mistral arm — the exact-hit "
        "path is not being exercised, so this test cannot see a pricing break"
    )


# --- THE WIRING, asserted over the SURFACE ----------------------------------
# This PR originally shipped MeteredMistralBackend with ZERO CONSUMERS: the
# class existed, was tested, and could not be SELECTED, because
# `_default_backends()` is a hand-maintained dict it was never added to. It read
# as "the mistral lane exists" on every summary while `swarph bench` still could
# not call it — the exact gap the card was filed to close, reproduced one layer
# over.
#
# MUTATION-CHECKED: removing the registry entry left the whole suite GREEN, so
# nothing in the repo asserted the wiring. That is why review caught it and the
# tests did not.
#
# Asserted over EVERY backend rather than over mistral specifically: a test that
# names one lane cannot catch the next lane somebody forgets.
def test_every_backend_class_is_actually_SELECTABLE():
    import inspect
    from swarph_cli.bench import backends as backends_mod
    from swarph_cli.commands.bench import _default_backends

    # CONCRETE lanes only. `Backend` itself is the PROTOCOL every lane
    # implements — it is defined here and is correctly not selectable, so a
    # name-suffix filter alone reports it as an unwired backend. Excluded by
    # WHAT IT IS (a typing.Protocol) rather than by its name, so a future
    # protocol or ABC is handled without another special case.
    # (The first version of this test flagged it — a false positive in the test
    # written to catch false negatives.)
    defined = {
        name for name, obj in vars(backends_mod).items()
        if inspect.isclass(obj)
        and name.endswith("Backend")
        and obj.__module__ == backends_mod.__name__
        and not getattr(obj, "_is_protocol", False)
        and not getattr(obj, "__abstractmethods__", ())
    }
    # Vacuity guard FIRST: an empty enumeration passes the subset check below
    # trivially — the 0/0 failure, inside the test written to prevent it.
    assert defined, "no backend classes found — the enumeration is broken"
    assert "MeteredMistralBackend" in defined, "enumeration is undercounting"

    selectable = {type(b).__name__ for b in _default_backends().values()}
    missing = defined - selectable
    assert not missing, (
        f"backend class(es) defined but NOT selectable from `_default_backends()`: "
        f"{sorted(missing)} — a producer with zero consumers reads as shipped and "
        f"cannot be run by anyone"
    )


def test_the_existing_metered_key_still_means_GEMINI():
    """>>> THE CONTROL, and it guards a real hazard. <<<

    The lazy way to wire mistral would have been to rebind `metered`. Every
    existing `id:metered` model spec would then silently change provider —
    same command, same output shape, different lane and different bill. Mistral
    is its own key precisely so that cannot happen.
    """
    from swarph_cli.bench.backends import MeteredGeminiBackend
    from swarph_cli.commands.bench import _default_backends

    assert isinstance(_default_backends()["metered"], MeteredGeminiBackend)
