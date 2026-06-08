"""§3.1 publish-time **security watchtower** for the swarph commons.

The watchtower is a defense-in-depth, layered model that gates an artifact's
content before it can be published to — or installed from — the commons:

1. **static scan (v1, THIS module).** A deterministic, *injection-immune*
   regex scanner over the artifact's canonical text. It flags dangerous
   patterns (pipe-to-interpreter, reverse shells, credential reads,
   instruction-smuggling in skills, …) and produces a ``PASS`` / ``FLAG`` /
   ``FAIL`` verdict. It is intentionally **non-LLM**: precisely *because* it
   does not "read" the content as instructions, it cannot be talked out of a
   verdict by content aimed at a reviewer (an artifact that says "ignore your
   security policy" just trips a rule). Publishers run it via ``swarph scan``;
   the install path gates a ``FAIL`` to a refusal (see ``add.py``).

2. **LLM security-reviewer (v2, scaffolded — :func:`llm_review`).** A
   perspective-diverse, security-specialist *cell* that judges *intent* —
   catching the obfuscated/novel attacks a static rule table misses. It is
   run on the content-as-DATA in a clean, restrictive config and is NEVER
   trusted alone: it sits *behind* the static scan (which can't be
   prompt-injected) so a content payload aimed at the reviewer still hits the
   deterministic gate first.

3. **signed-publisher verification (v2, scaffolded —
   :func:`verify_signature`).** Flips a published artifact from
   ``fail-closed`` (the v1 published-publisher refusal in ``add.py``) to
   *installable* when it both PASSES the scan and carries a valid signature
   from a trusted publisher key.

The v1 static layer is the deterministic floor; the v2 layers add
intent-judgement and provenance on top. Each layer is *additive* — a later
layer can only make the verdict stricter, never override a ``FAIL`` from the
deterministic floor.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScanFinding:
    """One dangerous-pattern hit from :func:`static_scan`.

    ``severity`` is ``"high"`` (→ FAIL) or ``"medium"`` (→ FLAG). ``rule`` is
    a short stable id (e.g. ``"shell-pipe-to-interpreter"``). ``message`` is a
    human explanation. ``excerpt`` is the matched snippet (truncated).
    """

    severity: str
    rule: str
    message: str
    excerpt: str


@dataclass(frozen=True)
class ScanResult:
    """A scan verdict + its findings.

    ``verdict`` is ``"PASS"`` (no findings), ``"FLAG"`` (only ``medium``
    findings), or ``"FAIL"`` (at least one ``high`` finding). ``findings`` is
    a ``tuple[ScanFinding, ...]``.
    """

    verdict: str
    findings: tuple


# --------------------------------------------------------------------------- #
# Rule tables (module-level dicts so they stay extensible + testable)
# --------------------------------------------------------------------------- #
#
# Each rule is a dict: rule-id -> (severity, compiled-regex, message). Tables
# are keyed by artifact class. ``static_scan`` walks the table for the class
# (falling back to a minimal generic set for an unknown class) and emits a
# finding per matching rule. Regexes are case-insensitive + multiline.

_FLAGS = re.IGNORECASE | re.MULTILINE

#: Outbound to a hardcoded external host/IP — excludes 127.0.0.1 / localhost.
_EXTERNAL_URL = re.compile(
    r"https?://(?!127\.0\.0\.1|localhost)[A-Za-z0-9._-]+", _FLAGS
)

#: Credential / secret read patterns (shared HIGH rule across shell + JSON).
_CRED_PATTERN = re.compile(
    r"(id_rsa|\.ssh/|\.aws/|\.env\b|credentials|MESH_GATEWAY_TOKEN|"
    r"ANTHROPIC_API_KEY|\.credentials\.json)",
    _FLAGS,
)

#: Pipe a network fetch into a shell interpreter.
_PIPE_TO_INTERP = re.compile(
    r"(curl|wget)\b[^\n|]*\|\s*(sh|bash)", _FLAGS
)


def _rule(severity, pattern, message):
    return (severity, re.compile(pattern, _FLAGS), message)


# ---- hook (shell) -------------------------------------------------------- #
_HOOK_RULES = {
    "shell-pipe-to-interpreter": (
        "high",
        _PIPE_TO_INTERP,
        "pipes a network fetch (curl/wget) straight into a shell interpreter",
    ),
    "reverse-shell": _rule(
        "high",
        r"(/dev/tcp/|nc\s+-e|bash\s+-i)",
        "reverse-shell pattern (/dev/tcp, nc -e, or bash -i)",
    ),
    "credential-read": (
        "high",
        _CRED_PATTERN,
        "reads a credential/secret path or env var",
    ),
    "rm-rf-root": _rule(
        "high",
        r"rm\s+-rf\s+/",
        "recursive force-delete rooted at /",
    ),
    "base64-pipe-to-interpreter": _rule(
        "high",
        r"base64\s+-d[^\n]*\|\s*(sh|bash)",
        "decodes base64 and pipes it into a shell (obfuscated execution)",
    ),
    "eval-network-fetch": _rule(
        "high",
        r'eval\s+"?\$\((curl|wget)',
        "evals the output of a network fetch",
    ),
    "outbound-external": (
        "medium",
        _EXTERNAL_URL,
        "contacts a hardcoded external host/IP",
    ),
    "world-writable-chmod": _rule(
        "medium",
        r"chmod\s+0?777",
        "makes a path world-writable (chmod 777)",
    ),
}


# ---- mcp / tool (server/lane spec JSON, scanned as text) ----------------- #
_MCP_RULES = {
    "command-shell-url": _rule(
        "high",
        r"\b(bash|sh)\b[^\n]{0,40}?-c\b[^\n]*https?://",
        "a command invokes bash -c / sh -c with a URL",
    ),
    "credential-env": (
        "high",
        _CRED_PATTERN,
        "embeds a credential/secret path or env var",
    ),
    "outbound-external": (
        "medium",
        _EXTERNAL_URL,
        "references a non-local external URL",
    ),
    "npx-unscoped": _rule(
        "medium",
        r"npx\s+-y\s+(?!@)[A-Za-z0-9._-]+",
        "npx -y of an unscoped (non-@-namespaced) package",
    ),
}


# ---- skill (SKILL.md text) ----------------------------------------------- #
_SKILL_RULES = {
    "instruction-smuggling-ignore": _rule(
        "high",
        r"ignore (all )?previous instructions",
        "instruction-smuggling: tries to override prior instructions",
    ),
    "exfiltration": _rule(
        "high",
        r"exfiltrat",
        "references exfiltration of data",
    ),
    "conceal-from-user": _rule(
        "high",
        r"do not (tell|inform|mention to) the user",
        "instructs the agent to conceal an action from the user",
    ),
    "send-to-external": _rule(
        "high",
        r"send\b[^\n]*\b(to|->)\s*https?://",
        "instructs sending data to an external URL",
    ),
    "external-post-target": (
        "medium",
        _EXTERNAL_URL,
        "names a bare external http(s) target",
    ),
}


# ---- generic (unknown class) — minimal so nothing silently passes -------- #
_GENERIC_RULES = {
    "credential-read": (
        "high",
        _CRED_PATTERN,
        "reads a credential/secret path or env var",
    ),
    "shell-pipe-to-interpreter": (
        "high",
        _PIPE_TO_INTERP,
        "pipes a network fetch into a shell interpreter",
    ),
}


#: class -> rule table. ``tool`` reuses the mcp HIGH/MEDIUM set per spec.
_RULE_TABLES = {
    "hook": _HOOK_RULES,
    "mcp": _MCP_RULES,
    "tool": _MCP_RULES,
    "skill": _SKILL_RULES,
}


def _excerpt(match, width: int = 120) -> str:
    snippet = match.group(0)
    snippet = " ".join(snippet.split())
    if len(snippet) > width:
        snippet = snippet[: width - 1] + "…"
    return snippet


def static_scan(klass: str, content: str) -> ScanResult:
    """Deterministically scan ``content`` for an artifact ``klass``.

    Walks the per-class regex rule table (a minimal generic set for an
    unknown class so it never silently passes everything), emitting one
    :class:`ScanFinding` per matching rule. Verdict thresholds:

    * **FAIL** — at least one ``high`` finding.
    * **FLAG** — at least one ``medium`` finding and no ``high``.
    * **PASS** — no findings.

    This is the deterministic, injection-immune v1 layer: it matches patterns,
    it does not interpret the content as instructions, so content aimed at a
    reviewer can't talk it out of a verdict.
    """
    table = _RULE_TABLES.get(klass, _GENERIC_RULES)

    findings = []
    for rule_id, (severity, pattern, message) in table.items():
        m = pattern.search(content)
        if m is not None:
            findings.append(
                ScanFinding(
                    severity=severity,
                    rule=rule_id,
                    message=message,
                    excerpt=_excerpt(m),
                )
            )

    has_high = any(f.severity == "high" for f in findings)
    has_medium = any(f.severity == "medium" for f in findings)
    if has_high:
        verdict = "FAIL"
    elif has_medium:
        verdict = "FLAG"
    else:
        verdict = "PASS"

    return ScanResult(verdict=verdict, findings=tuple(findings))


# --------------------------------------------------------------------------- #
# v2 watchtower layers — documented scaffolds (NOT built)
# --------------------------------------------------------------------------- #


def llm_review(klass: str, content: str) -> ScanResult:  # pragma: no cover - stub
    """**v2 (§3.1)** — perspective-diverse LLM security-reviewer.

    A security-specialist *cell* that judges an artifact's *intent* — the
    obfuscated/novel attacks the deterministic :func:`static_scan` rule table
    can miss. It MUST be run on the content-as-DATA inside a clean, restrictive
    config (no inherited permissive settings, no CLAUDE.md, creds-only) and is
    NEVER trusted alone: it sits *behind* the injection-immune static scan so a
    payload aimed at the reviewer hits the deterministic gate first. Additive
    only — it can tighten a verdict, never override a ``FAIL`` from the
    deterministic floor.
    """
    raise NotImplementedError(
        "LLM security-reviewer is the v2 watchtower layer (§3.1) — a "
        "security-specialist cell that judges intent; the static scan is the "
        "v1 deterministic layer"
    )


def verify_signature(
    content: bytes, signature: str, publisher: str
) -> bool:  # pragma: no cover - stub
    """**v2 (§3.1)** — signed-publisher verification.

    Returns True iff ``signature`` is a valid signature over ``content`` from a
    trusted ``publisher`` key. Flips a *published* artifact from the v1
    fail-closed refusal (see ``add.py``) to installable when it both PASSES the
    static scan and carries a valid publisher signature.
    """
    raise NotImplementedError(
        "signed-publisher verification is v2 (§3.1) — flips a published "
        "artifact from fail-closed to installable when PASS+signed"
    )


# --------------------------------------------------------------------------- #
# ``swarph scan`` — the watchtower as a CLI
# --------------------------------------------------------------------------- #


_VALID_CLASSES = ("hook", "mcp", "skill", "tool")

#: Verdict → process exit code. PASS=0, FLAG=1, FAIL=2.
_VERDICT_EXIT = {"PASS": 0, "FLAG": 1, "FAIL": 2}


def _print_result(path: str, klass: str, result: ScanResult, out) -> None:
    out(f"swarph scan: {path}  (class={klass})")
    out(f"  verdict: {result.verdict}")
    if not result.findings:
        out("  no dangerous patterns matched")
        return
    for f in result.findings:
        out(f"  [{f.severity.upper():6}] {f.rule}: {f.message}")
        out(f"           ↳ {f.excerpt}")


def run_scan(argv) -> int:
    """``swarph scan <path> --class {hook,mcp,skill,tool}``.

    Reads ``<path>`` (utf-8), runs :func:`static_scan`, prints the verdict +
    each finding. Exit code: **0** PASS, **1** FLAG, **2** FAIL. A missing
    file or a missing/invalid ``--class`` → a clear error + exit **2**.
    """
    parser = argparse.ArgumentParser(
        prog="swarph scan",
        description=(
            "Statically scan an artifact's content for dangerous patterns "
            "(the §3.1 watchtower v1 deterministic layer)."
        ),
    )
    parser.add_argument("path", help="path to the artifact's content file")
    parser.add_argument(
        "--class",
        dest="klass",
        choices=_VALID_CLASSES,
        required=True,
        help="artifact class: hook | mcp | skill | tool",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse exits 2 on a missing/invalid --class or path; normalise to
        # our refusal code 2.
        return 2 if (exc.code or 0) != 0 else 0

    p = Path(args.path).expanduser()
    try:
        content = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"swarph scan: file not found: {args.path}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"swarph scan: cannot read {args.path}: {exc}", file=sys.stderr)
        return 2

    result = static_scan(args.klass, content)
    _print_result(args.path, args.klass, result, print)
    return _VERDICT_EXIT[result.verdict]
