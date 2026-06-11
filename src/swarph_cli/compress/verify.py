"""Verification gates. Pure-Python gates are the mechanical floor; verify_expand
(model, below) is the adversarial quality check on shorthand output."""
from __future__ import annotations
import gzip, re
from pathlib import Path

_LINK_RE = re.compile(r"\]\(([^)]+)\)")


def _links(text: str) -> set[str]:
    return set(_LINK_RE.findall(text))


def links_preserved(source: str, output: str) -> bool:
    """Every link in source survives in output (superset)."""
    return _links(source).issubset(_links(output))


def entries_point_to_source(output: str, pointer: str, base: Path) -> bool:
    """Every non-blank, non-comment line contains the pointer substring AND at
    least one of its links resolves on disk. Guards the index-over-source invariant."""
    for line in output.splitlines():
        s = line.strip()
        if not s or s.startswith("<!--") or s.startswith("#"):
            continue
        if pointer not in line:
            return False
        targets = _LINK_RE.findall(line)
        if not any((base / t).exists() for t in targets):
            return False
    return True


def redundancy_ratio(text: str) -> float:
    """1 - gzip(text)/len(text). High = compressible prose; low = already dense."""
    raw = text.encode("utf-8")
    if not raw:
        return 0.0
    return 1.0 - (len(gzip.compress(raw, 9)) / len(raw))


def above_floor(text: str, floor: float) -> bool:
    """True if there's redundancy worth removing (ratio >= floor)."""
    return redundancy_ratio(text) >= floor


def idempotent(first_pass: str, second_pass: str, tol: float = 0.05) -> bool:
    """compress(compress(x)) ~= noop: second pass must not shrink >tol of first."""
    if not first_pass:
        return True
    shrink = (len(first_pass) - len(second_pass)) / len(first_pass)
    return shrink <= tol
