"""Opt-in compression marker parser. Pure Python, fail-safe: anything not a
well-formed marker -> None (leave the file untouched)."""
from __future__ import annotations
import re
from dataclasses import dataclass

_MARKER_RE = re.compile(r"<!--\s*swarph:compress\s+(?P<body>.*?)\s*-->")
_KV_RE = re.compile(r'(\w+)=(?:"([^"]*)"|(\S+))')
_VALID_LEVERS = {"shorthand", "archival"}


@dataclass(frozen=True)
class Marker:
    lever: str
    pointer: str | None = None
    floor: float | None = None
    boundary: str | None = None


def parse_marker(text: str) -> Marker | None:
    m = _MARKER_RE.search(text)
    if not m:
        return None
    kv = {k: (q or u) for k, q, u in _KV_RE.findall(m.group("body"))}
    lever = kv.get("lever")
    if lever not in _VALID_LEVERS:
        return None  # fail safe
    floor = None
    if "floor" in kv:
        try:
            floor = float(kv["floor"])
        except ValueError:
            return None
    if lever == "shorthand" and not kv.get("pointer"):
        return None  # shorthand REQUIRES a pointer (index-over-source invariant)
    if lever == "archival" and not kv.get("boundary"):
        return None  # archival REQUIRES a boundary signal
    return Marker(lever=lever, pointer=kv.get("pointer"), floor=floor,
                  boundary=kv.get("boundary"))
