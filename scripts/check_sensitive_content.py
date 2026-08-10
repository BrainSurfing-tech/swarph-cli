#!/usr/bin/env python3
"""Reject sensitive organization and customer identifiers before publication."""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path


# Encoded so the guard itself does not reintroduce the identifiers it prevents.
_FORBIDDEN_TERMS = tuple(
    base64.b64decode(value).decode("utf-8").casefold()
    for value in (
        "THlyw6ljbw==",
        "THlyZWNv",
        "UHVibGljaXM=",
        "U2FwaWVudA==",
        "U29uZXBhcg==",
        "Q2FybHNiZXJn",
        "U3RlbGxhbnRpcw==",
        "UmVuYXVsdA==",
        "Tmlzc2Fu",
    )
)


def _tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(name) for name in result.stdout.decode().split("\0") if name]


def _contains_forbidden_term(path: Path) -> bool:
    try:
        contents = path.read_bytes()
    except OSError as error:
        print(f"sensitive-content: unable to read {path}: {error}", file=sys.stderr)
        return True
    if b"\0" in contents:
        return False
    return any(term in contents.decode("utf-8", errors="ignore").casefold()
               for term in _FORBIDDEN_TERMS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="scan every tracked file instead of hook arguments")
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args()

    files = _tracked_files() if args.all else args.files
    matches = [path for path in files if _contains_forbidden_term(path)]
    if not matches:
        return 0

    print(
        "sensitive-content: blocked sensitive organization or customer "
        "identifier in:",
        file=sys.stderr,
    )
    print(*(f"  {path}" for path in matches), sep="\n", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
