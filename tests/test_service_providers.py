"""Tests for the $0-service provider core (no fastapi needed).

The load-bearing security property: a $0 lane must NEVER be able to fall back to
metered API billing. ``scrub_billing_env`` strips every known provider API-key
var from the subprocess env, so the wrapped CLI can only use its $0 subscription
auth (which lives in CLI config files, not these env vars).
"""

from __future__ import annotations

import pytest

from swarph_cli.service import providers as pv


# --- billing-scrub (the security core) -------------------------------------

def test_scrub_strips_all_known_metered_keys():
    base = {"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o",
            "GEMINI_API_KEY": "g", "GOOGLE_API_KEY": "g2", "XAI_API_KEY": "x",
            "GROK_API_KEY": "gk", "DEEPSEEK_API_KEY": "d",
            "PATH": "/usr/bin", "HOME": "/home/u"}
    env = pv.scrub_billing_env("claude", base)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "GOOGLE_API_KEY", "XAI_API_KEY", "GROK_API_KEY", "DEEPSEEK_API_KEY"):
        assert k not in env, f"{k} not scrubbed — metered-billing leak"
    # non-key env survives (the CLI still needs PATH/HOME for its subscription auth)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/u"


def test_scrub_strips_every_provider_regardless_of_lane():
    # a claude lane must not be able to bill OpenAI either (defense in depth)
    env = pv.scrub_billing_env("claude", {"OPENAI_API_KEY": "sk-o", "PATH": "/b"})
    assert "OPENAI_API_KEY" not in env


def test_scrub_does_not_mutate_input():
    base = {"ANTHROPIC_API_KEY": "sk", "PATH": "/b"}
    pv.scrub_billing_env("claude", base)
    assert base["ANTHROPIC_API_KEY"] == "sk"  # original env untouched


# --- provider command map --------------------------------------------------

def test_provider_command_claude():
    assert pv.provider_command("claude", "hi") == ["claude", "-p", "hi"]


def test_provider_command_codex():
    assert pv.provider_command("codex", "hi") == ["codex", "exec", "hi"]


def test_provider_command_gemini():
    assert pv.provider_command("gemini", "hi") == ["agy", "--print", "hi"]


def test_provider_command_unknown_raises():
    with pytest.raises(ValueError):
        pv.provider_command("nope", "hi")


def test_supported_providers_exposed():
    assert set(pv.SUPPORTED) == {"claude", "codex", "gemini"}


# --- run_provider (subprocess, mocked) -------------------------------------

def test_run_provider_scrubs_env_and_returns_stdout(monkeypatch):
    captured = {}

    class _P:
        returncode = 0
        stdout = "answer\n"
        stderr = ""

    def fake_run(argv, env=None, **kw):
        captured["argv"] = argv
        captured["env"] = env
        return _P()

    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    out = pv.run_provider("claude", "hi",
                          base_env={"ANTHROPIC_API_KEY": "sk", "PATH": "/b"})
    assert out == "answer"
    assert "ANTHROPIC_API_KEY" not in captured["env"]  # scrubbed at the subprocess boundary
    assert captured["argv"] == ["claude", "-p", "hi"]


def test_run_provider_raises_on_nonzero(monkeypatch):
    class _P:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(pv.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(RuntimeError):
        pv.run_provider("claude", "hi", base_env={})
