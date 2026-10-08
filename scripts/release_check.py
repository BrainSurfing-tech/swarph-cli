#!/usr/bin/env python3
"""Checks for release.sh (card #1065).

The version a running importer loaded is read from that process's own image
(the swarph_cli-X.Y.Z.dist-info path it mapped at import). Nothing here asks
the installer what it last wrote.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

DIST_INFO = re.compile(rb"swarph_cli-(\d+\.\d+\.\d+)\.dist-info")
VERSION_LINE = {
    "pyproject": re.compile(r'^version = "[^"]+"', re.M),
    "init": re.compile(r'^__version__ = "[^"]+"', re.M),
    "plugin": re.compile(r'"version": "[^"]+"'),
}


def versions_in_image(blob: bytes) -> set[str]:
    return {m.decode() for m in DIST_INFO.findall(blob)}


def start_epoch_from_stat(stat_text: str, btime: float, clk_tck: float) -> float:
    """Field 22 of /proc/pid/stat, after the comm field which may contain spaces."""
    rparen = stat_text.rfind(")")
    if rparen < 0:
        raise ValueError("stat has no comm field")
    fields = stat_text[rparen + 2:].split()
    ticks = int(fields[19])  # field 22; field 3 is fields[0]
    return btime + ticks / clk_tck


def approval_at_head(reviews: list[dict], head: str) -> bool:
    """True only when some reviewer's latest non-comment review is APPROVED on this head."""
    latest: dict[str, dict] = {}
    ordered = sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0))
    for review in ordered:
        if (review.get("state") or "").upper() == "COMMENTED":
            continue
        login = ((review.get("user") or {}).get("login")) or ""
        latest[login] = review
    return any(
        (review.get("state") or "").upper() == "APPROVED" and review.get("commit_id") == head
        for review in latest.values()
    )


def bump_text(kind: str, version: str, text: str) -> str:
    pattern = VERSION_LINE[kind]
    replacement = {
        "pyproject": f'version = "{version}"',
        "init": f'__version__ = "{version}"',
        "plugin": f'"version": "{version}"',
    }[kind]
    new, n = pattern.subn(replacement, text, count=1)
    if n != 1:
        raise SystemExit(f"release: {kind} has no version field to bump")
    return new


def verify_process(start: float, install_epoch: float, image: bytes, expected: str, pid: str) -> None:
    """Fail if the process started before the install, or its loaded version is not expected."""
    if start < install_epoch:
        raise SystemExit(
            f"FAIL pid {pid} started at {start:.3f} before the install at {install_epoch:.3f}"
        )
    found = versions_in_image(image)
    if found != {expected}:
        shown = ", ".join(sorted(found)) or "none"
        raise SystemExit(
            f"FAIL pid {pid} loaded __version__ {shown} from the process image, wanted {expected}"
        )


def script_imports_swarph_cli(argv: list[str], read_text) -> bool:
    """True when this process's entry script imports swarph_cli."""
    if "-m" in argv:
        module = argv[argv.index("-m") + 1] if argv.index("-m") + 1 < len(argv) else ""
        if module == "swarph_cli" or module.startswith("swarph_cli."):
            return True
    for arg in argv:
        if not (arg.endswith(".py") or arg.endswith("/swarph") or os.path.basename(arg) == "swarph"):
            continue
        try:
            text = read_text(arg)
        except OSError:
            continue
        if "import swarph_cli" in text or "from swarph_cli" in text:
            return True
    return False


def discover_units(list_running, main_pid, cmdline, read_text) -> list[str]:
    found = []
    for unit in list_running():
        pid = main_pid(unit)
        if not pid or pid == "0":
            continue
        argv = cmdline(pid)
        if script_imports_swarph_cli(argv, read_text):
            found.append(unit)
    return found


def _running_swarph_units() -> list[str]:
    out = subprocess.run(
        ["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "swarph*"],
        text=True, capture_output=True,
    )
    units = []
    for line in out.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.endswith(".service"):
            units.append(name)
    return units


def _main_pid(unit: str) -> str:
    out = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", unit],
        text=True, capture_output=True,
    )
    return out.stdout.strip()


def _cmdline(pid: str) -> list[str]:
    raw = open(f"/proc/{pid}/cmdline", "rb").read()
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _read_text(path: str) -> str:
    return open(path, encoding="utf-8", errors="replace").read()


def _cmd_discover() -> int:
    for unit in discover_units(_running_swarph_units, _main_pid, _cmdline, _read_text):
        print(unit)
    return 0


def _read_proc_image(pid: str) -> bytes:
    chunks = []
    maps = open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace").read().splitlines()
    mem = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    try:
        for line in maps:
            parts = line.split()
            if len(parts) < 2 or "r" not in parts[1]:
                continue
            start_s, end_s = parts[0].split("-")
            start, end = int(start_s, 16), int(end_s, 16)
            size = end - start
            if size <= 0 or size > 16 * 1024 * 1024:
                continue
            try:
                os.lseek(mem, start, os.SEEK_SET)
                chunks.append(os.read(mem, size))
            except OSError:
                continue
    finally:
        os.close(mem)
    return b"".join(chunks)


def _cmd_verify(args: argparse.Namespace) -> int:
    if os.environ.get("RELEASE_START_EPOCH"):
        start = float(os.environ["RELEASE_START_EPOCH"])
    else:
        stat = open(f"/proc/{args.pid}/stat", encoding="utf-8", errors="replace").read()
        btime = float(os.environ.get("RELEASE_BTIME", "0"))
        if not os.environ.get("RELEASE_BTIME"):
            for line in open("/proc/stat", encoding="utf-8"):
                if line.startswith("btime "):
                    btime = float(line.split()[1])
                    break
        clk = float(os.environ.get("RELEASE_CLK_TCK", str(os.sysconf("SC_CLK_TCK"))))
        start = start_epoch_from_stat(stat, btime, clk)
    if os.environ.get("RELEASE_IMAGE"):
        image = open(os.environ["RELEASE_IMAGE"], "rb").read()
    else:
        image = _read_proc_image(args.pid)
    verify_process(start, float(args.install_epoch), image, args.expected, str(args.pid))
    print(f"ok pid {args.pid} started after the install and loaded {args.expected}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="release_check")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("approval-at-head")
    a.add_argument("--head", required=True)

    b = sub.add_parser("bump")
    b.add_argument("--kind", required=True, choices=sorted(VERSION_LINE))
    b.add_argument("--version", required=True)
    b.add_argument("path")

    v = sub.add_parser("verify")
    v.add_argument("--pid", required=True)
    v.add_argument("--expected", required=True)
    v.add_argument("--install-epoch", required=True)

    sub.add_parser("discover")

    args = p.parse_args(argv)
    if args.cmd == "approval-at-head":
        reviews = json.load(sys.stdin)
        if approval_at_head(reviews, args.head):
            return 0
        print(f"no APPROVED review at head {args.head}", file=sys.stderr)
        return 2
    if args.cmd == "discover":
        return _cmd_discover()
    if args.cmd == "bump":
        path = args.path
        text = open(path, encoding="utf-8").read()
        open(path, "w", encoding="utf-8").write(bump_text(args.kind, args.version, text))
        return 0
    if args.cmd == "verify":
        return _cmd_verify(args)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
