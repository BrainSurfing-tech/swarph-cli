"""FastAPI $0-service app — a subscription LLM CLI exposed as a $0 HTTP lane.

`POST /delegate` (bearer-authed) runs the provider's subscription CLI one-shot
with a billing-scrubbed env (see ``providers.scrub_billing_env``) and returns the
completion at ``cost_usd: 0.0``. Behind the optional ``[service]`` extra.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import providers as pv


class DelegateRequest(BaseModel):
    prompt: str
    system: str | None = None
    timeout: int | None = None


def build_app(provider: str, token: str, timeout: int = pv._DEFAULT_TIMEOUT) -> FastAPI:
    if provider not in pv.SUPPORTED:
        raise ValueError(
            f"unsupported provider {provider!r}; supported: {', '.join(pv.SUPPORTED)}")

    app = FastAPI(title=f"swarph service ({provider})",
                  description="$0 subscription-LLM HTTP lane")

    def _auth(authorization: str) -> None:
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="bad or missing bearer token")

    @app.get("/health")
    def health():
        return {"ok": True, "provider": provider, "cost_model": "$0 subscription"}

    @app.post("/delegate")
    def delegate(req: DelegateRequest, authorization: str = Header(default="")):
        _auth(authorization)
        prompt = req.prompt if not req.system else f"System: {req.system}\n\n{req.prompt}"
        t0 = time.monotonic()
        try:
            text = pv.run_provider(provider, prompt, timeout=req.timeout or timeout)
        except RuntimeError as exc:
            # CLI failed — surface the (already-truncated, env-free) reason, never the env.
            raise HTTPException(status_code=502, detail=str(exc))
        return {"text": text, "provider": provider, "cost_usd": 0.0,
                "duration_s": round(time.monotonic() - t0, 3)}

    return app
