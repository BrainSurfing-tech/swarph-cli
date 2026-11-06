#!/usr/bin/env python3
"""Shared outbound scrub (card #1073, lab decision msg 65982).

ONE source for secret-shaped TEXT redaction before any model input:
``scrub_text(str) -> str | None``. The triage router imports it (no
copy); hippocampus (lab-orchestrator) imports it from the installed
package and fails closed on None. Rules and classifiers never see raw
payload — only this module's output.

Returns None for non-str input: an unscrubbable payload must never
reach a model, so callers split/skip instead of calling.
"""
from __future__ import annotations

import re

_SECRET_PATTERNS = [
    # Anthropic keys. Kept above the OpenAI rule so sk-ant- is not also
    # eaten as a generic sk- key.
    (re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{8,}\b"), "[REDACTED-API-KEY]"),
    # OpenAI: classic sk-, plus hyphenated project and service-account
    # keys. The old class was [A-Za-z0-9] and stopped at the first hyphen,
    # so sk-proj- and sk-svcacct- reached the model (card #1472).
    # swarph_cli.redact._OPENAI_RE already allowed those shapes; this is
    # the shared model-input scrubber. triage.router imports scrub_text
    # and does not keep a copy.
    (re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9\-_]{16,}\b"),
     "[REDACTED-API-KEY]"),
    # xAI keys (msg 65982).
    (re.compile(r"\bxai-[A-Za-z0-9\-_]{16,}\b"), "[REDACTED-API-KEY]"),
    # Google AI Studio keys: AIza + 35 chars (msg 65982). Exact length:
    # a shorter made-up tail is not this shape. A closing \b misses a tail
    # whose 35th character is '-' when the next character is end, whitespace,
    # a quote, or other punctuation (both non-word). A following '-' is a
    # boundary too, so key-x and key-- match and a key that ends in '-' stays
    # inside the class. The lookahead still refuses a longer alphanumeric or
    # underscore tail (card #1472).
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{35}(?![0-9A-Za-z_])"),
     "[REDACTED-API-KEY]"),
    # GitHub + Slack tokens.
    (re.compile(r"\bgh[opusr]_[A-Za-z0-9_]{20,}\b"), "[REDACTED-TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}\b"), "[REDACTED-TOKEN]"),
    # Bearer credentials.
    (re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
     "Bearer [REDACTED]"),
    # JWTs: two compact segments minimum (msg 65982).
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+"
                r"(?:\.[A-Za-z0-9_\-]+)?"), "[REDACTED-JWT]"),
    # AWS access key ids (msg 65982).
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWS-KEY]"),
    # PEM blocks (private keys and certificates alike — content, not shape).
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED-PRIVATE-KEY]"),
    # password/token/secret assignments.
    (re.compile(r"(?i)\b(password|passwd|pwd|api[_-]?key|auth[_-]?token|secret)\b\s*[:=]\s*\S+"),
     r"\1=[REDACTED]"),
]


def scrub_text(payload) -> str | None:
    """Scrubbed text, or None when the payload is not scrubbable text
    (then the model is not called for it). Never raises on str input:
    an input that cannot be scrubbed must read as unscrubbable, not
    crash the caller — return None rather than propagate."""
    if not isinstance(payload, str):
        return None
    try:
        out = payload
        for pattern, replacement in _SECRET_PATTERNS:
            out = pattern.sub(replacement, out)
    except Exception:
        return None
    return out
