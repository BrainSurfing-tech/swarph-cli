"""The backend abstraction (spec §4). ``bench`` OWNS a thin, provider-agnostic
backend interface — it does NOT reuse the LLM services layer's :878x/:879x
lane map (decision #3: bench owns its own).

A backend maps ``(model_id, prompt, system) -> BackendResult`` (tokens in /
thought / out, latency, text, whether tokens are ESTIMATED). ``system``
threads to every backend (metered: ``system_instruction``; a CLI-shelling
backend would prepend it — see :class:`SubscriptionBackend`).

Ships two backends:

- :class:`MeteredGeminiBackend` — google-genai Developer API, DEFAULT for v1
  (real ``usage_metadata``: prompt/candidates/thoughts tokens). CRITICAL:
  passes ``vertexai=False`` EXPLICITLY — a ``GOOGLE_GENAI_USE_VERTEXAI=true``
  env 401s API-key auth (this exact bug bit the reference lab; see
  ``model_showdown.py::run_metered``).
- :class:`SubscriptionBackend` — a STUB/interface only. Tokens are always
  ESTIMATED (``~len(text)/4``, flagged via ``estimated=True``) and it does
  NOT couple to any specific subscription lane map; a caller wires an actual
  $0 CLI/OIDC path in by passing ``call_fn``. Not exercised against a live
  provider anywhere in this package.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol


@dataclass
class BackendResult:
    text: str
    tokens_in: int
    tokens_thought: int
    tokens_out: int
    latency_s: float
    estimated: bool
    error: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + (self.tokens_thought or 0) + self.tokens_out


class Backend(Protocol):
    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        ...

    def missing_creds(self) -> list[str]:
        """-> human-readable names of missing REQUIRED credentials, e.g.
        ``["GEMINI_API_KEY (or GOOGLE_API_KEY)"]``. Empty = ready to dispatch.
        Checked by :func:`swarph_cli.bench.runner.preflight` BEFORE any
        network call, so a missing key surfaces as one clear warning instead
        of a mid-run 401 traceback."""
        ...


def estimate_tokens(text: Optional[str]) -> int:
    """Rough ~4-chars/token estimate for backends that report no usage
    metadata (ported from the reference ``_est_tokens``)."""
    return max(1, round(len(text or "") / 4))


class MeteredGeminiBackend:
    """google-genai Developer API — the v1 DEFAULT metered backend. Real
    ``usage_metadata`` (prompt/candidates/thoughts token counts), pennies per
    call, no subscription-quota throttling.

    ``google-genai`` is imported LAZILY (inside :meth:`generate`, not at
    module import time) so importing :mod:`swarph_cli.bench.backends` never
    requires the dependency unless the metered backend is actually used —
    consistent with swarph-cli's dependency-light-core-paths convention
    (``[mcp]``/``[gateway]``/``[service]`` extras).
    """

    #: env vars checked (in order) when no api_key is passed to __init__.
    ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key

    def missing_creds(self) -> list[str]:
        if self._api_key:
            return []
        if any(os.environ.get(v) for v in self.ENV_VARS):
            return []
        return [f"{self.ENV_VARS[0]} (or {self.ENV_VARS[1]})"]

    def credentials_ok(self) -> bool:
        return not self.missing_creds()

    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=0.0, estimated=False,
                error=f"google-genai not installed: {exc} "
                      f"(pip install swarph-cli[bench] or `pip install google-genai`)",
            )
        api_key = self._api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=0.0, estimated=False,
                error="no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY)",
            )
        t0 = time.time()
        try:
            # vertexai=False EXPLICITLY: if GOOGLE_GENAI_USE_VERTEXAI=true is
            # set in the environment, Vertex rejects API-key auth with a 401.
            # This is not optional — it's the exact bug that bit swarph-news.
            client = genai.Client(api_key=api_key, vertexai=False)
            cfg = types.GenerateContentConfig(system_instruction=system) if system else None
            resp = client.models.generate_content(model=model_id, contents=prompt, config=cfg)
        except Exception as exc:  # provider/network errors surface, not swallow silently
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=round(time.time() - t0, 2), estimated=False, error=str(exc),
            )
        latency_s = round(time.time() - t0, 2)
        usage = getattr(resp, "usage_metadata", None)
        return BackendResult(
            text=getattr(resp, "text", "") or "",
            tokens_in=(usage.prompt_token_count or 0) if usage else 0,
            tokens_thought=(getattr(usage, "thoughts_token_count", 0) or 0) if usage else 0,
            tokens_out=(usage.candidates_token_count or 0) if usage else 0,
            latency_s=latency_s,
            estimated=False,
        )


class SubscriptionBackend:
    """STUB/interface-only backend for a $0 subscription path (e.g. a node's
    own CLI/OIDC lane). Deliberately NOT coupled to the reference stack's
    :878x/:879x lane map (decision #3) — a caller supplies ``call_fn`` to
    wire an actual subscription transport; without one, every call fails
    clearly rather than silently degrading.

    Token counts are ALWAYS estimated (``estimated=True``) — subscription
    CLIs commonly emit no usage metadata at all. This is UNVERIFIED against
    any live subscription provider; it exists so ``bench`` has a second
    backend shape to validate against without hardcoding a real one.
    """

    def __init__(self, call_fn: Optional[Callable[[str, str, str], str]] = None):
        # call_fn(model_id, prompt, system) -> raw response text
        self._call_fn = call_fn

    def missing_creds(self) -> list[str]:
        """Not env-checkable (the credential is a node's own CLI/OIDC
        session, which varies per deployment) — per decision, this FLAGS
        rather than hard-checks: no ``call_fn`` wired means there is no
        transport at all, which the preflight treats the same as a missing
        credential so it's skipped with a clear reason instead of erroring
        mid-run."""
        if self._call_fn is None:
            return ["subscription CLI/OIDC auth (not env-checkable; no call_fn wired — stub backend)"]
        return []

    def credentials_ok(self) -> bool:
        return not self.missing_creds()

    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        if self._call_fn is None:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=0.0, estimated=True,
                error="SubscriptionBackend is a stub — no call_fn wired for a real "
                      "$0 subscription transport",
            )
        t0 = time.time()
        try:
            text = self._call_fn(model_id, prompt, system) or ""
        except Exception as exc:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=round(time.time() - t0, 2), estimated=True, error=str(exc),
            )
        latency_s = round(time.time() - t0, 2)
        return BackendResult(
            text=text,
            tokens_in=estimate_tokens(prompt),
            tokens_thought=0,
            tokens_out=estimate_tokens(text),
            latency_s=latency_s,
            estimated=True,
        )
