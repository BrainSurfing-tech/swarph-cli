"""A swarph-OWNED SLM client. OpenAI chat-completions shape, stdlib only.

WHY THIS EXISTS. enrich previously required `workers.slm_client` — a module from a
PRIVATE hedge-fund-mcp layout that no installed wheel carries (#734). So ENRICH had
never run anywhere except the one box holding that repo, and `swarph dreaming` shipped
with a third of itself permanently skipped.

>>> VENDORING `workers` WAS REJECTED, AND THE REASON IS MEASURED. <<< NINE directories
named `workers/` exist on lab-ovh alone (lab-orchestrator, lead-contagion-project,
rho-inc2-decide, three hedge-fund-mcp copies, ...), 15 to 177 modules each, and none
carries an `__init__.py` — they are repo-root namespace dirs, not packages. Putting a
top-level `workers` into a SHARED site-packages would decide, globally and silently,
which project's code every `from workers.X import Y` on that box resolves to. Nor is
the client self-contained: it pulls `workers.omega_logger`, `workers.storage_hub` and
conditionally `database.timeseries_db`.

OPENAI SHAPE, NOT OLLAMA-NATIVE, DELIBERATELY. One client then serves:
  - local Ollama, $0        verified 2026-09-16 against nemotron-mini:4b on gpu-wsl,
                            POST /v1/chat/completions returned the OpenAI envelope
  - any cheap hosted API    opencode / DeepSeek and anything else OpenAI-compatible
  - gbrain's own endpoint   which already returns the OpenAI shape
Same code for all three; the choice is an env var, not a code change.

>>> NO HOST DEFAULT (#578). <<< The endpoint comes from the environment or it does not
exist. A baked address is a remedy with an expiry date — it is correct on the day and
silently wrong after the next machine move, and the failure lands on whoever inherits
it. An unset endpoint makes available() False, enrich SKIPS with ENRICH_SKIPPED_NO_SLM,
and that is a DEGRADE, never a crash (#734). The model is required for the same reason:
a default model name expires exactly like a default host.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

ENDPOINT_ENV = "SWARPH_SLM_ENDPOINT"
MODEL_ENV = "SWARPH_SLM_MODEL"
KEY_ENV = "SWARPH_SLM_API_KEY"      # optional; omitted for local Ollama
TIMEOUT_ENV = "SWARPH_SLM_TIMEOUT"  # optional, seconds


def configured() -> tuple[str, str] | None:
    """(endpoint, model) from the environment, or None when either is unset.

    Both or neither: a configured endpoint with no model would pick one implicitly,
    which is the same expiry problem one level down.
    """
    ep = (os.environ.get(ENDPOINT_ENV) or "").strip().rstrip("/")
    model = (os.environ.get(MODEL_ENV) or "").strip()
    return (ep, model) if ep and model else None


class SLMClient:
    """Minimal OpenAI chat-completions client. `generate(prompt) -> str`.

    The method is `generate` and returns the RAW string, matching what
    dreaming.enrich calls. It deliberately does NOT offer generate_json: enrich
    needs many proposals, and a dict-coercing helper silently keeps only the
    first element of a JSON array — a partial result wearing a success.
    """

    def __init__(self, endpoint: str | None = None, model: str | None = None,
                 api_key: str | None = None, timeout: float | None = None):
        cfg = configured()
        self.endpoint = (endpoint or (cfg[0] if cfg else "")).rstrip("/")
        self.model = model or (cfg[1] if cfg else "")
        if not self.endpoint or not self.model:
            raise RuntimeError(
                f"SLM client needs {ENDPOINT_ENV} and {MODEL_ENV}; no host default (#578)")
        self.api_key = api_key if api_key is not None else os.environ.get(KEY_ENV)
        self.timeout = float(timeout if timeout is not None
                             else os.environ.get(TIMEOUT_ENV) or 120)

    def generate(self, prompt: str) -> str:
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.endpoint}/v1/chat/completions", data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            payload = json.loads(r.read().decode())
        # Fail loudly on a shape we did not expect rather than returning "" — an
        # empty string would read to enrich as "the model proposed nothing", which
        # is a DIFFERENT state from "the endpoint answered something else".
        return payload["choices"][0]["message"]["content"]


def available() -> bool:
    """True iff an endpoint+model are configured AND the endpoint answers.

    Reachability is part of the question: a configured-but-unreachable endpoint
    makes enrich spend its whole run failing one call per session. Cheap probe,
    short timeout, never raises.
    """
    if configured() is None:
        return False
    ep = configured()[0]
    try:
        req = urllib.request.Request(f"{ep}/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception:
        return False
