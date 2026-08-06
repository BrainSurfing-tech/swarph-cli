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
    """The card's premise, asserted rather than assumed.

    #323 rests on "bench can already price mistral". That claim came from a
    grep, and a grep over this file also matches `e5-mistral-7b-instruct` and
    bedrock pixtral — models that are NOT Mistral AI's chat lane. Pin the two
    metered arms by exact key so the premise cannot rot silently.
    """
    import json
    from pathlib import Path
    import swarph_cli.bench as bench_pkg
    prices = json.loads(
        (Path(bench_pkg.__file__).parent / "data" / "llm_prices.json").read_text()
    )["prices"]
    for arm in ("mistral-medium-3-5", "devstral-small"):
        assert arm in prices, f"{arm} is not priced; #323's premise no longer holds"
