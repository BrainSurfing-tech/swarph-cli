"""$0-service provider core — billing-scrub + the CLI command map.

A swarph **service** wraps a *subscription* LLM CLI as a $0 HTTP lane. The
load-bearing security property: such a lane must NEVER be able to fall back to
metered API billing (the €50-Google-alert lesson, made structural). A
subscription CLI authenticates from its own config files (``~/.claude``,
``~/.codex``, ``~/.gemini/antigravity``, …) — *not* from an API-key env var. So
we strip every known provider API-key var from the subprocess env: if a stray
``ANTHROPIC_API_KEY`` were present, the CLI could silently bill it instead of
the subscription. Scrubbing makes the $0 path the only path.

No fastapi here — this module is the pure, importable security logic; the HTTP
app lives in ``service/app.py`` behind the optional ``[service]`` extra.
"""

from __future__ import annotations

import subprocess

# Union of metered API-key env vars, stripped for EVERY lane (a claude lane must
# not be able to bill OpenAI either). These are unambiguously "use a paid API
# key" — subscription auth never lives here, so stripping them only removes the
# metered-fallback path.
_SCRUB_KEYS = frozenset({
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
    "DEEPSEEK_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "PERPLEXITY_API_KEY",
})

# provider -> argv prefix (the prompt is appended as the final arg). The prompt
# already carries any system context (the app composes it) so these stay simple.
# NOTE: the gemini lane (agy) reads the prompt ONLY from argv and hard-caps it
# (MAX_ARG_LEN ~4128) — very long prompts will be rejected by agy, not truncated.
_COMMANDS = {
    "claude": ["claude", "-p"],
    "codex": ["codex", "exec"],
    "gemini": ["agy", "--print"],
}

SUPPORTED = tuple(_COMMANDS.keys())

_DEFAULT_TIMEOUT = 120  # seconds; mirrors CLAUDE_SUBSCRIPTION_TIMEOUT default


def scrub_billing_env(provider: str, base_env: dict) -> dict:
    """Return a COPY of ``base_env`` with every metered API-key var removed.

    ``provider`` is accepted for symmetry / future per-provider nuance; today the
    scrub is the full union for defense in depth. The input dict is never mutated.
    """
    return {k: v for k, v in base_env.items() if k not in _SCRUB_KEYS}


def provider_command(provider: str, prompt: str) -> list:
    """Build the subprocess argv for ``provider`` running ``prompt`` one-shot."""
    prefix = _COMMANDS.get(provider)
    if prefix is None:
        raise ValueError(
            f"unsupported provider {provider!r}; supported: {', '.join(SUPPORTED)}")
    return [*prefix, prompt]


def run_provider(provider: str, prompt: str, timeout: int = _DEFAULT_TIMEOUT,
                 base_env: dict | None = None, home_root=None) -> str:
    """Run the provider's subscription CLI one-shot under an ISOLATED HOME.

    The spawned CLI gets a disposable HOME carrying only this provider's own auth
    (swarph_shared.agent_isolation), so it cannot read the operator's GitHub token
    (GH_TOKEN / ~/.config/gh), git credentials, or ssh-agent socket (#2a). Returns
    stdout (stripped). Raises ``ValueError`` for an unknown provider and
    ``RuntimeError`` on a non-zero exit (stderr head only — never echo the env).
    """
    import os
    from pathlib import Path

    from swarph_shared.agent_isolation import build_isolated_env, prepare_isolated_home

    argv = provider_command(provider, prompt)
    root = Path(home_root) if home_root is not None else Path.home() / ".swarph" / "drone-homes"
    home = prepare_isolated_home(provider, root)
    source = os.environ if base_env is None else base_env
    env = build_isolated_env(source, home, provider)
    proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{provider} CLI exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()
