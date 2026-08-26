"""#615: `swarph version` — per-module versions + install origin.

A single version string cannot depict a mixed install, and under #496's seam
every cell IS a mixed install: released wheels, snapshots from a working
tree, stale dependencies. Measured 2026-08-26: a cell pipx-installed from
merged main reported 'swarph-cli 0.49.1', identical to the release — while
its dist-info already recorded the truth unread:

    direct_url.json = {"url": "file:///home/ubuntu/swarph-cli"}   <- a SNAPSHOT

PEP 610 gives each distribution an origin; this verb reads it. The fleet's
verification convention this week has been "a verification carries a
coordinate — which install, which box" stated in prose; this makes the
coordinate machine-readable.

Exit codes for --check (the #401 contract, same codes a supervisor already
knows): 0 = every module is an index install at the latest published
version; 1 = a snapshot or a stale module (verified, actionable); 7 =
couldn't-verify (PyPI unreachable — NOT success).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from importlib import metadata

_MODULES = ("swarph-cli", "swarph-mesh", "swarph-shared")
_PYPI_URL = "https://pypi.org/pypi/{}/json"


def _origin(dist: metadata.Distribution) -> str:
    """The PEP 610 origin of one installed distribution.

    vcs_info  -> git:<commit>          (the most honest coordinate there is)
    dir_info  -> dir:<path>            (a working-tree snapshot; +',editable')
    archive   -> archive:<url>         (direct URL wheel/sdist)
    ABSENT    -> 'pypi'                (resolved from an index as a
                                        dependency — the released-wheel path;
                                        pip records no direct_url for those)
    """
    try:
        raw = dist.read_text("direct_url.json")
    except (OSError, TypeError):
        raw = None
    if not raw:
        return "pypi"
    try:
        du = json.loads(raw)
    except json.JSONDecodeError:
        return "unknown"
    if "vcs_info" in du:
        commit = (du["vcs_info"].get("commit_id") or "?")[:12]
        return f"git:{commit}"
    if "dir_info" in du:
        suffix = ",editable" if du["dir_info"].get("editable") else ""
        return f"dir:{du.get('url', '').removeprefix('file://')}{suffix}"
    if "archive_info" in du:
        return f"archive:{du.get('url', '?')}"
    return "unknown"


def _parse_version(v: str) -> tuple[int, ...] | None:
    m = re.match(r"^(\d+(?:\.\d+)*)", v.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _latest_published(name: str, timeout: float = 5.0) -> str | None:
    """None = couldn't-verify (offline, 404, garbage) — never a guess."""
    try:
        with urllib.request.urlopen(_PYPI_URL.format(name),
                                    timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["info"]["version"]
    except Exception:
        return None


def _collect() -> list[dict]:
    rows = []
    for name in _MODULES:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            rows.append({"name": name, "version": None, "origin": "absent"})
            continue
        rows.append({"name": name, "version": dist.version,
                     "origin": _origin(dist)})
    return rows


def _format(rows: list[dict], check: bool) -> str:
    width = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        ver = r["version"] or "NOT INSTALLED"
        line = f"  {r['name'].ljust(width)}  {ver:<12} {r['origin']}"
        if check and r.get("latest") is not None:
            stale = (_parse_version(r["version"] or "") or ()) < \
                    (_parse_version(r["latest"]) or ())
            line += f"   (latest {r['latest']}{'  STALE' if stale else ''})"
        elif check:
            line += "   (latest ?)"
        lines.append(line)
    return "\n".join(lines)


def run_version(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="swarph version",
        description="Per-module versions + install origin (PEP 610). A single "
                    "version string cannot depict a mixed install; under the "
                    "#496 seam every cell is one.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--check", action="store_true",
                   help="also query PyPI for latest-published per module. "
                        "Exit: 0 all released+current, 1 snapshot or stale, "
                        "7 couldn't-verify (PyPI unreachable).")
    args = p.parse_args(argv)

    rows = _collect()
    if args.check:
        unreachable = False
        for r in rows:
            if r["version"] is None:
                continue
            r["latest"] = _latest_published(r["name"])
            unreachable = unreachable or r["latest"] is None

    if args.json:
        print(json.dumps({"modules": rows}, indent=2))
    else:
        print(_format(rows, args.check))

    if not args.check:
        return 0
    if unreachable:
        return 7
    def _bad(r):
        if r["version"] is None:
            return True
        if r["origin"] != "pypi":
            return True
        latest = r.get("latest")
        return latest is not None and \
            (_parse_version(r["version"]) or ()) < (_parse_version(latest) or ())
    return 1 if any(_bad(r) for r in rows) else 0
