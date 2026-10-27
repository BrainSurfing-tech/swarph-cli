from __future__ import annotations

import contextvars
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Stated false-positive budget constant: 0.0% false positives permitted on the reference corpus
FALSE_POSITIVE_BUDGET: float = 0.0

# Audit / Opt-out event tracking
OPT_OUT_EVENTS: list[dict] = []

# ContextVar for propagating opt-out through call chains into HTTP choke points
_OPT_OUT_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar("_OPT_OUT_CTX", default=False)


class redact_opt_out_context:
    """Context manager to propagate opt-out status through the call chain to HTTP choke points."""

    def __init__(self, opt_out: bool = True):
        self.opt_out = opt_out
        self.token = None

    def __enter__(self):
        if self.opt_out:
            self.token = _OPT_OUT_CTX.set(True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.token is not None:
            _OPT_OUT_CTX.reset(self.token)


def get_opt_out_events() -> list[dict]:
    """Return a copy of all recorded opt-out audit events."""
    return list(OPT_OUT_EVENTS)


def clear_opt_out_events() -> None:
    """Clear the in-memory opt-out audit log."""
    OPT_OUT_EVENTS.clear()


def shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string in bits per character."""
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


# ── Character classification and entropy helpers ──────────────────────────────

def _char_class(c: str) -> int:
    if "a" <= c <= "z":
        return 1
    if "A" <= c <= "Z":
        return 2
    if "0" <= c <= "9":
        return 3
    return 4  # symbol (+, /, =, -, _, etc.)


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

_SAFE_PLACEHOLDERS = {
    "dummy", "placeholder", "example", "test", "change_me", "your_token_here",
    "redacted", "none", "null", "undefined", "true", "false",
}


def _is_standalone_secret(tok: str) -> bool:
    if len(tok) < 24:
        return False
    core = tok.rstrip("=")
    # Exclude pure hex (git commits 40 chars, sha256 64 chars, md5 32 chars) in any case
    if _HEX_RE.fullmatch(core):
        return False
    # Exclude UUIDs
    if len(core) == 36 and _UUID_RE.fullmatch(core):
        return False
    # Exclude tokens with a prefix followed by a hex digest of length >= 16 (e.g. branch-40hex)
    for sep in ("-", "_"):
        if sep in core:
            suffix = core.rsplit(sep, 1)[-1]
            if len(suffix) >= 16 and _HEX_RE.fullmatch(suffix):
                return False
    # Exclude tokens containing __ or more than 2 underscores/hyphens
    if "__" in tok or (tok.count("_") + tok.count("-")) > 2:
        return False
    # Require upper, lower, and digits all present, with at least 4 digits
    has_upper = any("A" <= c <= "Z" for c in tok)
    has_lower = any("a" <= c <= "z" for c in tok)
    digit_count = sum(1 for c in tok if "0" <= c <= "9")
    if not (has_upper and has_lower and digit_count >= 4):
        return False
    # Shannon entropy >= 4.25
    if shannon_entropy(tok) < 4.25:
        return False
    # High character-class transitions (at least 40% of adjacent character pairs change class)
    transitions = sum(
        1 for i in range(len(tok) - 1) if _char_class(tok[i]) != _char_class(tok[i + 1])
    )
    if (transitions / (len(tok) - 1)) < 0.40:
        return False
    return True


# ── Patterns ──────────────────────────────────────────────────────────────────

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Known secret patterns
_GITHUB_RE = re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_ANTHROPIC_RE = re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{8,}\b")
_OPENAI_RE = re.compile(r"\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9\-_]{16,}\b")
_XAI_RE = re.compile(r"\bxai-[A-Za-z0-9\-_]{16,}\b")
_GCP_RE = re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")
_SLACK_RE = re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}\b")
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_AWS_SECRET_RE = re.compile(
    r"(?i)\b(aws_secret_access_key|aws_session_token)\b(\s*[:=]\s*)(['\"]?)(?!\[REDACTED)[A-Za-z0-9/+=]{20,}\3"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.eyJ[A-Za-z0-9_\-]{4,}(?:\.[A-Za-z0-9_\-]{4,})?\b")
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)(?!\[REDACTED)[A-Za-z0-9\-._~+/]{12,}=*")

# URI & DSN credentials
_URI_SCHEME_RE = re.compile(
    r"\b([a-zA-Z][a-zA-Z0-9+.\-]*://[^/\s:@]+:)(?!\[REDACTED)([^@\s/]+)(@[^\s/]+)"
)
_URI_SCHEMELESS_RE = re.compile(
    r"(?<!\[)(?<![a-zA-Z0-9/._\-:])(?!(?:REDACTED|CUSTOM):)([a-zA-Z0-9._\-]+:)(?!\[REDACTED)([^@\s:/]{2,})(@(?:localhost|(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}|\d{1,3}(?:\.\d{1,3}){3})(?::\d+)?)\b"
)
_DSN_KEY_VALUE_RE = re.compile(
    r"(?i)(?<![a-zA-Z0-9_])(?<!--)(password|passwd|pwd|api[_-]?key|auth[_-]?token|access[_-]?token|mesh_gateway_token|pgpassword|rediscli_auth|client_secret|secret)\b(\s*[:=]\s*)(?!\[REDACTED)(['\"]?)([^\s'\"&;,)]+)\3"
)

# CLI passwords
_MYSQL_ATTACHED_RE = re.compile(r"\b(mysql(?:dump|admin)?(?:\s+[^\n;|]+?)?\s+-p)(?!\[REDACTED)(\S+)")
_MYSQL_LONG_RE = re.compile(r"\b(mysql(?:dump|admin)?(?:\s+[^\n;|]+?)?\s+--password(?:=|\s+))(?!\[REDACTED)(\S+)")
_DOCKER_LOGIN_RE = re.compile(
    r"\b(docker\s+login(?:\s+[^\n;|]+?)?\s+(?:-p|--password)(?:=|\s+))(?!\[REDACTED)(?!-|--password-stdin\b)(\S+)"
)
_SSHPASS_RE = re.compile(r"\b(sshpass\s+-p\s*)(?!\[REDACTED)(\S+)")
_CURL_RE = re.compile(r"\b(curl(?:\s+[^\n;|]+?)?\s+(?:-u|--user)\s+[^:\s]+:)(?!\[REDACTED)(\S+)")

# Shannon entropy secret-assignment
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b([a-z0-9_]*(?:token|secret|key|pass|cred|auth|private)[a-z0-9_]*)\b(\s*[:=]\s*)(['\"]?)(?!\[REDACTED)([A-Za-z0-9/+=\-_]{16,})\3"
)

# Standalone high-entropy secret token candidates: '=' only at trailing padding
_STANDALONE_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9/+\-_/.])([A-Za-z0-9/+\-_]{24,}=*)(?![A-Za-z0-9/+\-_/.])"
)


def _normalize_custom_rules(custom_rules) -> list[tuple[str, re.Pattern]]:
    rules: list[tuple[str, re.Pattern]] = []
    if custom_rules:
        if not isinstance(custom_rules, (list, tuple)):
            raise ValueError(f"custom_rules must be a list or tuple, got {type(custom_rules).__name__}")
        for item in custom_rules:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                name, pat = item[0], item[1]
            elif isinstance(item, dict) and "name" in item and "pattern" in item:
                name, pat = item["name"], item["pattern"]
            else:
                raise ValueError(f"Invalid custom redaction rule: {item!r}")
            if not str(name).strip():
                raise ValueError(f"Custom rule name cannot be empty: {item!r}")
            safe_name = re.sub(r"[^A-Za-z0-9]", "_", str(name)).upper()
            if isinstance(pat, str):
                pat = re.compile(pat)
            rules.append((safe_name, pat))

    env_rules = os.environ.get("SWARPH_REDACT_CUSTOM_RULES")
    if env_rules:
        parsed = json.loads(env_rules)
        if not isinstance(parsed, list):
            raise ValueError("SWARPH_REDACT_CUSTOM_RULES must be a JSON list")
        for item in parsed:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                name, pat = item[0], item[1]
            elif isinstance(item, dict) and "name" in item and "pattern" in item:
                name, pat = item["name"], item["pattern"]
            else:
                raise ValueError(f"Invalid custom redaction rule in SWARPH_REDACT_CUSTOM_RULES: {item!r}")
            if not str(name).strip():
                raise ValueError(f"Custom rule name cannot be empty: {item!r}")
            safe_name = re.sub(r"[^A-Za-z0-9]", "_", str(name)).upper()
            if isinstance(pat, str):
                pat = re.compile(pat)
            rules.append((safe_name, pat))
    return rules


def _replace_secret_assignment(m: re.Match) -> str:
    key = m.group(1)
    sep = m.group(2)
    quote = m.group(3)
    val = m.group(4)
    val_lower = val.lower()
    if any(p in val_lower for p in _SAFE_PLACEHOLDERS):
        return m.group(0)
    # Exempt ANY pure hex value of length >= 16 (in lower, upper, or mixed case) and UUIDs
    if len(val) >= 16 and _HEX_RE.fullmatch(val):
        return m.group(0)
    if len(val) == 36 and _UUID_RE.fullmatch(val):
        return m.group(0)
    if shannon_entropy(val) >= 3.6:
        return f"{key}{sep}{quote}[REDACTED:HIGH_ENTROPY]{quote}"
    return m.group(0)


def _replace_standalone_tokens(text: str) -> str:
    matches = list(_STANDALONE_CANDIDATE_RE.finditer(text))
    for m in reversed(matches):
        tok = m.group(1)
        start, end = m.start(1), m.end(1)
        # Verify not already inside [REDACTED:...]
        preceding = text[:start]
        last_bracket = preceding.rfind("[REDACTED:")
        if last_bracket != -1:
            closing = text.find("]", last_bracket)
            if closing == -1 or closing >= start:
                continue
        if _is_standalone_secret(tok):
            text = text[:start] + "[REDACTED:HIGH_ENTROPY]" + text[end:]
    return text


def _handle_opt_out(text: str, caller: str, opt_out_reason: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    reason = opt_out_reason or "env_or_flag"
    event = {
        "ts": ts,
        "caller": caller,
        "reason": reason,
        "chars": len(text),
    }
    OPT_OUT_EVENTS.append(event)
    print(
        f"[swarph-redact] OPT-OUT caller={caller} reason={reason} ts={ts} chars={len(text)}",
        file=sys.stderr,
    )
    audit_path = os.environ.get("SWARPH_REDACT_AUDIT_LOG")
    if audit_path:
        try:
            Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
            with open(audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass


def redact_text(
    text: str | None,
    *,
    custom_rules=None,
    opt_out: bool = False,
    caller: str = "unknown",
    opt_out_reason: str = "",
) -> str | None:
    """Redact secret shapes from text, replacing them with typed markers.

    Returns the redacted string, or None if text is None.
    If opt_out is True or SWARPH_REDACT_OPTOUT is set, records an audit event and returns text unchanged.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        return text

    is_ctx_optout = _OPT_OUT_CTX.get()
    is_env_optout = os.environ.get("SWARPH_REDACT_OPTOUT", "").strip().lower() in ("1", "true", "yes")
    if opt_out or is_env_optout or is_ctx_optout:
        if not is_ctx_optout or opt_out:
            _handle_opt_out(text, caller, opt_out_reason)
        return text

    out = text

    # Layer 1: Custom Rules
    rules = _normalize_custom_rules(custom_rules)
    for safe_name, pat in rules:
        out = pat.sub(f"[REDACTED:CUSTOM:{safe_name}]", out)

    # Layer 2: PEM Private Keys
    out = _PEM_RE.sub("[REDACTED:PEM_PRIVATE_KEY]", out)

    # Layer 3: Known Secret Patterns
    out = _GITHUB_RE.sub("[REDACTED:GITHUB_TOKEN]", out)
    out = _ANTHROPIC_RE.sub("[REDACTED:ANTHROPIC_KEY]", out)
    out = _OPENAI_RE.sub("[REDACTED:API_KEY]", out)
    out = _XAI_RE.sub("[REDACTED:API_KEY]", out)
    out = _GCP_RE.sub("[REDACTED:GCP_API_KEY]", out)
    out = _SLACK_RE.sub("[REDACTED:SLACK_TOKEN]", out)
    out = _AWS_KEY_RE.sub("[REDACTED:AWS_KEY]", out)
    out = _AWS_SECRET_RE.sub(r"\1\2[REDACTED:AWS_SECRET]", out)
    out = _JWT_RE.sub("[REDACTED:JWT]", out)
    out = _BEARER_RE.sub(r"\1[REDACTED:BEARER_TOKEN]", out)

    # Layer 4: URI & DSN Credentials
    out = _URI_SCHEME_RE.sub(r"\1[REDACTED:URI_CREDENTIAL]\3", out)
    out = _URI_SCHEMELESS_RE.sub(r"\1[REDACTED:URI_CREDENTIAL]\3", out)
    out = _DSN_KEY_VALUE_RE.sub(r"\1\2[REDACTED:DSN_CREDENTIAL]", out)

    # Layer 5: Low-Entropy CLI Passwords
    out = _MYSQL_ATTACHED_RE.sub(r"\1[REDACTED:CLI_PASSWORD]", out)
    out = _MYSQL_LONG_RE.sub(r"\1[REDACTED:CLI_PASSWORD]", out)
    out = _DOCKER_LOGIN_RE.sub(r"\1[REDACTED:CLI_PASSWORD]", out)
    out = _SSHPASS_RE.sub(r"\1[REDACTED:CLI_PASSWORD]", out)
    out = _CURL_RE.sub(r"\1[REDACTED:CLI_PASSWORD]", out)

    # Layer 6: Shannon Entropy Scoring
    out = _SECRET_ASSIGN_RE.sub(_replace_secret_assignment, out)
    out = _replace_standalone_tokens(out)

    return out


def redact_value(
    value: Any,
    *,
    custom_rules=None,
    opt_out: bool = False,
    caller: str = "unknown",
    opt_out_reason: str = "",
) -> Any:
    """Recursively redact values in strings, dicts, lists, and tuples."""
    if isinstance(value, str):
        return redact_text(
            value,
            custom_rules=custom_rules,
            opt_out=opt_out,
            caller=caller,
            opt_out_reason=opt_out_reason,
        )
    if isinstance(value, dict):
        return {
            k: redact_value(
                v,
                custom_rules=custom_rules,
                opt_out=opt_out,
                caller=caller,
                opt_out_reason=opt_out_reason,
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            redact_value(
                item,
                custom_rules=custom_rules,
                opt_out=opt_out,
                caller=caller,
                opt_out_reason=opt_out_reason,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_value(
                item,
                custom_rules=custom_rules,
                opt_out=opt_out,
                caller=caller,
                opt_out_reason=opt_out_reason,
            )
            for item in value
        )
    return value


def encode_outbound_json(
    body: Any,
    *,
    custom_rules=None,
    opt_out: bool = False,
    caller: str = "http",
    opt_out_reason: str = "",
    already_logged_opt_out: bool = False,
) -> bytes:
    """Single outbound JSON choke point: redacts ``body`` via :func:`redact_value`
    and returns UTF-8 JSON bytes ready for HTTP transport."""
    is_ctx_optout = _OPT_OUT_CTX.get()
    is_env_optout = os.environ.get("SWARPH_REDACT_OPTOUT", "").strip().lower() in ("1", "true", "yes")
    if opt_out or is_env_optout or is_ctx_optout:
        raw_str = json.dumps(body)
        if not already_logged_opt_out and not is_ctx_optout:
            _handle_opt_out(raw_str, caller, opt_out_reason)
        return raw_str.encode("utf-8")
    redacted = redact_value(
        body,
        custom_rules=custom_rules,
        opt_out=False,
        caller=caller,
        opt_out_reason=opt_out_reason,
    )
    return json.dumps(redacted).encode("utf-8")
