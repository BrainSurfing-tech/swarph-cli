"""The backend abstraction (spec §4).

MeteredGeminiBackend's real call shape is verified structurally (the exact
``genai.Client(api_key=..., vertexai=False)`` / ``models.generate_content``
call, matching ``model_showdown.py::run_metered`` and the swarph-news usage)
by MOCKING ``google.genai.Client`` — this is UNVERIFIED against a live
provider in this test suite (no network, no real key ever touches these
tests). It IS verified live elsewhere (swarph-news uses the identical shape).

SubscriptionBackend is a stub/interface only; nothing here exercises a real
subscription provider.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from swarph_cli.bench.backends import MeteredGeminiBackend, SubscriptionBackend, estimate_tokens


# ── estimate_tokens ──────────────────────────────────────────────────────────

def test_estimate_tokens_roughly_four_chars_per_token():
    assert estimate_tokens("a" * 40) == 10


def test_estimate_tokens_minimum_one():
    assert estimate_tokens("") == 1
    assert estimate_tokens(None) == 1


# ── MeteredGeminiBackend: credential preflight ───────────────────────────────

def test_missing_creds_when_no_key_anywhere(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    backend = MeteredGeminiBackend()
    missing = backend.missing_creds()
    assert missing and "GEMINI_API_KEY" in missing[0]
    assert backend.credentials_ok() is False


def test_missing_creds_satisfied_by_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-fake")
    backend = MeteredGeminiBackend()
    assert backend.missing_creds() == []
    assert backend.credentials_ok() is True


def test_missing_creds_satisfied_by_google_api_key_alias(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-fake")
    backend = MeteredGeminiBackend()
    assert backend.missing_creds() == []


def test_missing_creds_satisfied_by_constructor_arg(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    backend = MeteredGeminiBackend(api_key="explicit-key")
    assert backend.missing_creds() == []


def test_generate_without_key_errors_clearly_not_a_traceback(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    backend = MeteredGeminiBackend()
    result = backend.generate("gemini-2.5-flash-lite", "hi")
    assert result.error is not None
    assert "API key" in result.error


# ── MeteredGeminiBackend: the real call shape (MOCKED, no network) ──────────

def test_generate_calls_client_with_vertexai_false_explicit(monkeypatch):
    """CRITICAL per the brief: vertexai=False must be passed EXPLICITLY —
    GOOGLE_GENAI_USE_VERTEXAI=true in the environment 401s API-key auth
    otherwise. This is the exact bug that bit swarph-news."""
    monkeypatch.setenv("GEMINI_API_KEY", "sk-fake")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")  # the trap env

    fake_resp = MagicMock()
    fake_resp.text = '{"answer": 68}'
    fake_resp.usage_metadata.prompt_token_count = 12
    fake_resp.usage_metadata.thoughts_token_count = 3
    fake_resp.usage_metadata.candidates_token_count = 7

    fake_client_cls = MagicMock()
    fake_client_instance = fake_client_cls.return_value
    fake_client_instance.models.generate_content.return_value = fake_resp

    import google.genai as real_genai
    monkeypatch.setattr(real_genai, "Client", fake_client_cls)

    backend = MeteredGeminiBackend()
    result = backend.generate("gemini-2.5-flash-lite", "what is 17*4?", system="be terse")

    assert result.error is None
    assert result.text == '{"answer": 68}'
    assert result.tokens_in == 12
    assert result.tokens_thought == 3
    assert result.tokens_out == 7
    assert result.estimated is False

    fake_client_cls.assert_called_once_with(api_key="sk-fake", vertexai=False)
    _, kwargs = fake_client_instance.models.generate_content.call_args
    assert kwargs["model"] == "gemini-2.5-flash-lite"
    assert kwargs["contents"] == "what is 17*4?"
    assert kwargs["config"] is not None  # system_instruction threaded through


def test_generate_no_system_omits_config(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-fake")
    fake_resp = MagicMock()
    fake_resp.text = "ok"
    fake_resp.usage_metadata = None

    fake_client_cls = MagicMock()
    fake_client_cls.return_value.models.generate_content.return_value = fake_resp
    import google.genai as real_genai
    monkeypatch.setattr(real_genai, "Client", fake_client_cls)

    backend = MeteredGeminiBackend()
    result = backend.generate("gemini-2.5-flash-lite", "hi", system="")
    assert result.tokens_in == 0 and result.tokens_out == 0  # no usage_metadata -> zeros, not a crash

    _, kwargs = fake_client_cls.return_value.models.generate_content.call_args
    assert kwargs["config"] is None


def test_generate_provider_exception_is_captured_not_raised(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-fake")
    fake_client_cls = MagicMock()
    fake_client_cls.return_value.models.generate_content.side_effect = RuntimeError("401 unauthorized")
    import google.genai as real_genai
    monkeypatch.setattr(real_genai, "Client", fake_client_cls)

    backend = MeteredGeminiBackend()
    result = backend.generate("gemini-2.5-flash-lite", "hi")
    assert result.error == "401 unauthorized"
    assert result.text == ""


# ── SubscriptionBackend: stub/interface only ─────────────────────────────────

def test_subscription_backend_without_call_fn_flags_missing_creds():
    backend = SubscriptionBackend()
    missing = backend.missing_creds()
    assert missing and "call_fn" in missing[0]
    assert backend.credentials_ok() is False


def test_subscription_backend_generate_without_call_fn_errors():
    backend = SubscriptionBackend()
    result = backend.generate("some-model", "hi")
    assert result.error is not None
    assert result.estimated is True


def test_subscription_backend_with_call_fn_estimates_tokens():
    def fake_cli(model_id, prompt, system):
        return "a response of some length"

    backend = SubscriptionBackend(call_fn=fake_cli)
    assert backend.missing_creds() == []
    result = backend.generate("some-model", "a prompt", system="ctx")
    assert result.error is None
    assert result.estimated is True  # ALWAYS flagged for subscription
    assert result.tokens_in == estimate_tokens("a prompt")
    assert result.tokens_out == estimate_tokens("a response of some length")


def test_subscription_backend_call_fn_exception_is_captured():
    def boom(model_id, prompt, system):
        raise RuntimeError("cli crashed")

    backend = SubscriptionBackend(call_fn=boom)
    result = backend.generate("some-model", "hi")
    assert result.error == "cli crashed"
    assert result.estimated is True
