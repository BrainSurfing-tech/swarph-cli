"""The backend abstraction (spec §4). ``bench`` OWNS a thin, provider-agnostic
backend interface — it does NOT reuse the LLM services layer's :878x/:879x
lane map (decision #3: bench owns its own).

A backend maps ``(model_id, prompt, system) -> BackendResult`` (tokens in /
thought / out, latency, text, whether tokens are ESTIMATED). ``system``
threads to every backend (metered: ``system_instruction``; a CLI-shelling
backend would prepend it — see :class:`SubscriptionBackend`).

Ships these backends:

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
- :class:`RuleBackend` — in-process ``rule:<module:callable>``. No credentials.
- :class:`TypedHttpBackend` — POST to a Jev-compatible ``/v1/systemone``
  (laya-serve). No credentials; per-item latency is the HTTP round trip.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol


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
    KIND = "semantic"
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


class MeteredMistralBackend:
    KIND = "semantic"
    """Mistral AI's own API — the fifth bench lane (card #323).

    WHY THIS EXISTS: ``swarph bench`` could already PRICE mistral (90
    mistral/devstral entries in ``data/llm_prices.json``, including
    ``mistral-medium-3-5`` and ``devstral-small``) and had no way to CALL it.
    The cost half of the question was answerable and the quality half was not
    reachable at all — the number was available, the measurement was not.

    Shaped deliberately like :class:`MeteredGeminiBackend`: lazy import inside
    :meth:`generate`, credentials checked by :meth:`missing_creds` BEFORE any
    network call, and provider/network errors returned in ``BackendResult.error``
    rather than raised — a failed arm must not abort a multi-arm run, and it must
    not silently score as an empty answer either.

    >>> ``tokens_thought`` IS REPORTED AS 0 AND ``estimated`` STAYS False. <<<
    That pair is a deliberate, honest claim and not an oversight: Mistral's chat
    completions return ``usage`` with prompt/completion counts only — there is no
    thinking-token field to read. So the in/out numbers are MEASURED (estimated
    False) while thought is genuinely ABSENT rather than estimated-as-zero. A
    reader comparing lanes must not mistake "this provider does not report it"
    for "this model did no thinking"; the runner should surface thought as
    unavailable for this backend rather than as a zero it can average.
    """

    #: env var checked when no api_key is passed to __init__.
    ENV_VARS = ("MISTRAL_API_KEY",)

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key

    def missing_creds(self) -> list[str]:
        if self._api_key or any(os.environ.get(v) for v in self.ENV_VARS):
            return []
        return [self.ENV_VARS[0]]

    def credentials_ok(self) -> bool:
        return not self.missing_creds()

    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        try:
            from mistralai import Mistral
        except ImportError as exc:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=0.0, estimated=False,
                error=f"mistralai not installed: {exc} "
                      f"(pip install swarph-cli[mistral] or `pip install mistralai`)",
            )
        api_key = self._api_key or os.environ.get("MISTRAL_API_KEY")
        if not api_key:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=0.0, estimated=False,
                error="no API key: set MISTRAL_API_KEY",
            )
        # system threads as a leading system message — Mistral has no separate
        # system_instruction field, so this is the provider-shaped equivalent of
        # the Gemini backend's `system_instruction`, NOT a prompt-prepend hack.
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        t0 = time.time()
        try:
            resp = Mistral(api_key=api_key).chat.complete(model=model_id, messages=messages)
        except Exception as exc:   # surface, never swallow — a silent empty answer
            return BackendResult(  # would score as a wrong answer rather than a failure
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=round(time.time() - t0, 2), estimated=False, error=str(exc),
            )
        latency_s = round(time.time() - t0, 2)
        usage = getattr(resp, "usage", None)
        choices = getattr(resp, "choices", None) or []
        text = ""
        if choices:
            text = getattr(getattr(choices[0], "message", None), "content", "") or ""
        return BackendResult(
            text=text,
            tokens_in=(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0,
            tokens_thought=0,          # not reported by this provider — see class docstring
            tokens_out=(getattr(usage, "completion_tokens", 0) or 0) if usage else 0,
            latency_s=latency_s,
            estimated=False,
        )


class SubscriptionBackend:
    KIND = "semantic"
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


def _load_rule(spec: str) -> Callable[..., Any]:
    """``module:callable`` -> the callable. The module path uses dots; the
    callable name is the segment after the last colon."""
    mod_name, sep, fn_name = spec.rpartition(":")
    if not sep or not mod_name or not fn_name:
        raise ValueError(f"rule model id must be module:callable, got {spec!r}")
    fn = getattr(importlib.import_module(mod_name), fn_name)
    if not callable(fn):
        raise TypeError(f"{spec} is not callable")
    return fn


def _rule_subject(prompt: str) -> Any:
    """A JSON bench prompt is the systemone template. The rule sees its
    ``state`` (the item), not the question text. A plain prompt is unchanged.
    """
    try:
        parsed = json.loads(prompt) if isinstance(prompt, str) else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and "state" in parsed:
        return parsed["state"]
    return prompt


def _call_rule(fn: Callable[..., Any], prompt: Any, system: str) -> Any:
    params = inspect.signature(fn).parameters
    if len(params) >= 2:
        return fn(prompt, system)
    return fn(prompt)


def _request_body(prompt: str, system: str) -> dict:
    """The POST body for ``/v1/systemone``.

    A prompt that is itself a JSON object with ``state`` and ``questions``
    is sent unchanged. That is how a pack carries a pre-registered template
    (question type, instructions, criteria) without this backend inventing
    them. Anything else is wrapped as one choice question whose instructions
    are the prompt. That wrapper has no criteria, so a live laya-serve
    rejects it; the gold packs must send the JSON form.
    """
    try:
        parsed = json.loads(prompt) if isinstance(prompt, str) else None
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and "state" in parsed and "questions" in parsed:
        return parsed
    return {
        "state": {"system": system, "prompt": prompt},
        "questions": {
            "answer": {"type": "choice", "instructions": prompt},
        },
    }


def _systemone_url(model_id: str) -> str:
    url = (model_id or "").strip()
    if not url:
        raise ValueError("typed-http model id is empty")
    if url.rstrip("/").endswith("/v1/systemone"):
        return url.rstrip("/")
    return url.rstrip("/") + "/v1/systemone"


# Frozen on card #941 (lab-ovh msg 48915): the verdict uses 0.50 only.
_NOUL_THRESHOLD = 0.50


def _noul_question(body: dict) -> Optional[dict]:
    questions = body.get("questions") if isinstance(body, dict) else None
    if not isinstance(questions, dict):
        return None
    named = questions.get("answer")
    if isinstance(named, dict) and named.get("type") == "noul":
        return named
    for block in questions.values():
        if isinstance(block, dict) and block.get("type") == "noul":
            return block
    return None


def _noul_labels(question: Optional[dict]) -> tuple[str, str]:
    """Positive and negative labels named on the template.

    ``labels.true`` / ``labels.false`` when the pack set them. Otherwise the
    frozen S1 pair YES / NO (msg 48915: P(true) >= 0.50 -> YES).
    """
    labels = {}
    if isinstance(question, dict) and isinstance(question.get("labels"), dict):
        labels = question["labels"]
    pos = labels.get("true")
    neg = labels.get("false")
    return (
        str(pos) if pos is not None else "YES",
        str(neg) if neg is not None else "NO",
    )


def _typed_value(payload: dict, request: Optional[dict] = None,
                 threshold: Optional[float] = None) -> tuple[Any, Optional[float]]:
    """Pull the typed answer out of a Jev /v1/systemone response.

    Prefers the question named ``answer``; otherwise the first question.
    ``choice``, ``noul``, ``yes``, then ``score``. A ``noul`` value is
    P(true): >= 0.50 maps to the template's positive label, below that to
    the negative. The raw probability is returned alongside the label.
    Choice and score are returned as the server sent them.
    """
    answers = payload.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise ValueError("systemone response has no answers")
    block = answers.get("answer")
    if not isinstance(block, dict):
        block = next(iter(answers.values()))
    if not isinstance(block, dict):
        raise ValueError("systemone answer is not an object")
    for key in ("choice", "noul", "yes", "score"):
        if key in block and block[key] is not None:
            if key == "noul":
                p_true = float(block[key])
                pos, neg = _noul_labels(_noul_question(request or {}))
                cut = _NOUL_THRESHOLD if threshold is None else threshold
                label = pos if p_true >= cut else neg
                return label, p_true
            return block[key], None
    raise ValueError("systemone answer has no choice, noul, yes, or score")


class RuleBackend:
    KIND = "typed"
    """In-process callable. ``model_id`` is ``module:callable``.

    The callable receives ``(prompt, system)`` when it takes two arguments,
    otherwise ``(prompt)``. A JSON prompt with ``state`` is unwrapped first:
    the callable sees that state, not the template. Its return value is the
    categorical label. No credentials. Latency is the call itself.
    """

    def missing_creds(self) -> list[str]:
        return []

    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        t0 = time.perf_counter()
        try:
            label = _call_rule(_load_rule(model_id), _rule_subject(prompt), system)
            text = json.dumps({"answer": label})
        except Exception as exc:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=round(time.perf_counter() - t0, 6),
                estimated=False, error=str(exc),
            )
        return BackendResult(
            text=text,
            tokens_in=0,
            tokens_thought=0,
            tokens_out=0,
            latency_s=round(time.perf_counter() - t0, 6),
            estimated=False,
        )


class TypedHttpBackend:
    KIND = "typed"
    """POST a bench item to a Jev-compatible ``/v1/systemone`` server.

    ``model_id`` is the server base (``http://127.0.0.1:<port>``) or a full
    ``.../v1/systemone`` URL. A prompt that is a JSON object with ``state``
    and ``questions`` is the POST body, unchanged — that is the pre-registered
    template. No API key is read or sent. Latency is the round trip.
    A ``noul`` answer is P(true): >= 0.50 becomes the template's positive
    label and below 0.50 the negative. The raw P(true) stays in the result
    text. Choice answers are the server's label, unthresholded.
    """

    def missing_creds(self) -> list[str]:
        return []

    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        t0 = time.perf_counter()
        try:
            endpoint = _systemone_url(model_id)
            body = _request_body(prompt, system)
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            payload = json.loads(raw.decode() or "{}")
            if not isinstance(payload, dict):
                raise ValueError("systemone response is not an object")
            label, p_true = _typed_value(payload, body)
            rendered = {"answer": label}
            if p_true is not None:
                rendered["p_true"] = p_true
            text = json.dumps(rendered)
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        except Exception as exc:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=round(time.perf_counter() - t0, 6),
                estimated=False, error=str(exc),
            )
        return BackendResult(
            text=text,
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_thought=0,
            tokens_out=int(usage.get("output_tokens") or 0),
            latency_s=round(time.perf_counter() - t0, 6),
            estimated=False,
        )


def _dig(payload: dict, dotted: str):
    cur: Any = payload
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _redact(text: str, secrets: list[tuple[str, str, str]]) -> str:
    """Replace the exact key and ``<scheme> <key>`` with ``<redacted:ENVNAME>``."""
    if not text:
        return text
    for value, envname, scheme in secrets:
        if not value:
            continue
        if scheme:
            text = text.replace(f"{scheme} {value}", f"<redacted:{envname}>")
        text = text.replace(value, f"<redacted:{envname}>")
    return text


class HttpBackend:
    """Authenticated HTTP arm. typed-http is the no-auth typed case of this shape.

    The key is read from the named env var at dispatch and placed only in the
    declared header. A non-2xx error is ``HTTP <status> <reason>`` and never
    the response body.
    """

    def __init__(self, *, name: str, kind: str, base_url: str, path: str,
                 auth: Optional[dict] = None, usage: Optional[dict] = None,
                 price: Optional[dict] = None, egress: str = "external",
                 noul_threshold: Optional[float] = None, transport=None):
        self.name = name
        self.KIND = kind
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.auth = auth
        self.usage = usage or {}
        self.price = price
        self.egress = egress
        self.noul_threshold = noul_threshold
        self.transport = transport
        self.calls = 0

    def missing_creds(self) -> list[str]:
        if not self.auth:
            return []
        env = self.auth.get("env") or ""
        if not os.environ.get(env):
            return [f"{env} (provider {self.name})"]
        return []

    def secrets(self) -> list[tuple[str, str, str]]:
        if not self.auth:
            return []
        env = self.auth.get("env") or ""
        value = os.environ.get(env) or ""
        if not value:
            return []
        return [(value, env, self.auth.get("scheme") or "")]

    def generate(self, model_id: str, prompt: str, system: str = "") -> BackendResult:
        t0 = time.perf_counter()
        secrets = self.secrets()
        try:
            endpoint = self.base_url + self.path
            if self.KIND == "typed":
                body = _request_body(prompt, system)
            else:
                body = {
                    "model": model_id or self.name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                }
            headers = {"Content-Type": "application/json"}
            if self.auth:
                env = self.auth["env"]
                value = os.environ.get(env) or ""
                scheme = self.auth.get("scheme") or ""
                token = f"{scheme} {value}".strip() if scheme else value
                headers[self.auth["header"]] = token
            raw_body = json.dumps(body).encode()
            self.calls += 1
            if self.transport is not None:
                status, reason, raw = self.transport(endpoint, headers, raw_body)
            else:
                req = urllib.request.Request(
                    endpoint, data=raw_body, headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        status, reason, raw = resp.status, resp.reason, resp.read()
                except urllib.error.HTTPError as exc:
                    status, reason, raw = exc.code, exc.reason, exc.read()
            if status < 200 or status >= 300:
                return BackendResult(
                    text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                    latency_s=round(time.perf_counter() - t0, 6),
                    estimated=False, error=_redact(f"HTTP {status} {reason}", secrets))
            payload = json.loads(raw.decode() or "{}")
            if not isinstance(payload, dict):
                raise ValueError("response is not an object")
            if self.KIND == "typed":
                label, p_true = _typed_value(payload, body, self.noul_threshold)
                rendered = {"answer": label}
                if p_true is not None:
                    rendered["p_true"] = p_true
                text = json.dumps(rendered)
                in_path = self.usage.get("in") or "usage.input_tokens"
                out_path = self.usage.get("out") or "usage.output_tokens"
            else:
                choices = payload.get("choices") or []
                message = (choices[0].get("message") or {}) if choices else {}
                text = message.get("content") or ""
                if not text:
                    raise ValueError("semantic response has no content")
                in_path = self.usage.get("in") or "usage.prompt_tokens"
                out_path = self.usage.get("out") or "usage.completion_tokens"
            tin = _dig(payload, in_path)
            tout = _dig(payload, out_path)
            text = _redact(text, secrets)
        except Exception as exc:
            return BackendResult(
                text="", tokens_in=0, tokens_thought=0, tokens_out=0,
                latency_s=round(time.perf_counter() - t0, 6),
                estimated=False, error=_redact(str(exc), secrets))
        return BackendResult(
            text=text,
            tokens_in=int(tin or 0),
            tokens_thought=0,
            tokens_out=int(tout or 0),
            latency_s=round(time.perf_counter() - t0, 6),
            estimated=tin is None or tout is None,
        )
