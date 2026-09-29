"""claude -p transport for the bench `subscription` backend (#999). Subscription-only,
isolated, billing-guarded. Measured 2026-09-29: without the isolation flags one call loaded
286 tools and 213k cache-write tokens; with them, 0 tools and ~421 input tokens."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional

_STRIP_EXACT = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_RATE = ("usage limit", "rate limit", "429", "overloaded")


@dataclass
class CliResult:
    text: str
    tokens_in: int
    tokens_out: int
    api_key_source: Optional[str]
    cost_equiv: Optional[float]
    error: Optional[str] = None


def build_argv(model_id: str, system: str) -> list:
    return ["claude", "-p", "--model", model_id, "--system-prompt", system, "--max-turns", "1",
            "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--tools", "", "--setting-sources", ""]


def clean_env(env: dict) -> dict:
    """Drop billing keys and every SWARPH_CHANNEL* name, including ones not listed yet."""
    return {
        k: v for k, v in env.items()
        if k not in _STRIP_EXACT and not k.startswith("SWARPH_CHANNEL")
    }


def parse_stream(lines) -> CliResult:
    key, res = None, None
    for ln in lines:
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("type") == "system" and e.get("subtype") == "init":
            key = e.get("apiKeySource")
        elif e.get("type") == "result":
            res = e
    if res is None:
        return CliResult("", 0, 0, key, None, error="no_result")
    u = res.get("usage") or {}
    tin = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out = CliResult(res.get("result") or "", tin, int(u.get("output_tokens") or 0), key, res.get("total_cost_usd"))
    if key != "none":
        return CliResult("", tin, out.tokens_out, key, out.cost_equiv, error=f"billing:{key}")
    if res.get("is_error"):
        msg = (res.get("result") or "").lower()
        out.text = ""
        out.error = "rate_limit" if any(t in msg for t in _RATE) else f"cli_error:{res.get('subtype')}"
    return out


def call(model_id: str, prompt: str, system: str, *, runner=subprocess.run, timeout: int = 300) -> CliResult:
    import os
    p = runner(build_argv(model_id, system), input=prompt, capture_output=True, text=True,
               env=clean_env(dict(os.environ)), timeout=timeout)
    return parse_stream((p.stdout or "").splitlines())
