"""Tokenless, cell-user wake from a different user's append-only inbox."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

ROOT_REFUSAL = 77
DIRECTORY_OWNER_REFUSAL = 78
FILE_OWNER_REFUSAL = 79
ACCOUNT_REFUSAL = 80


class Refusal(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def add_arguments(parser):
    parser.add_argument("--cell", required=True)
    parser.add_argument("--inbox", required=True, type=Path)
    parser.add_argument("--pane", required=True)
    parser.add_argument("--once", action="store_true", help="poll once instead of looping")


def _check_reader(inbox, cell):
    uid = os.geteuid()
    if uid == 0:
        raise Refusal(ROOT_REFUSAL, "refusing effective uid 0")
    ready = False
    try:
        directory = inbox.parent.lstat()
        if directory.st_uid == uid:
            raise Refusal(DIRECTORY_OWNER_REFUSAL, "refusing inbox directory owner")
        if stat.S_ISDIR(directory.st_mode):
            entry = inbox.lstat()
            if entry.st_uid == uid:
                raise Refusal(FILE_OWNER_REFUSAL, "refusing inbox file owner")
            ready = stat.S_ISREG(entry.st_mode)
    except OSError:
        pass
    import pwd

    try:
        account = pwd.getpwuid(uid).pw_name
    except KeyError:
        account = None
    if account != f"cell-{cell}":
        raise Refusal(ACCOUNT_REFUSAL, "refusing account other than the nominated cell user")
    return ready


def _empty_cursor():
    return {"device": None, "inode": None, "offset": 0,
            "sha256": hashlib.sha256().hexdigest(), "seen_ids": []}


def _load_cursor(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return _empty_cursor()
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("cursor is not a regular file")
        data = json.load(source)
    if (not isinstance(data, dict) or set(data) != set(_empty_cursor())
            or type(data.get("offset")) is not int
            or data["offset"] < 0 or not isinstance(data.get("seen_ids"), list)
            or any(type(i) is not int or i <= 0 for i in data["seen_ids"])
            or not isinstance(data.get("sha256"), str)
            or len(data["sha256"]) != 64
            or any(data.get(k) is not None and type(data.get(k)) is not int
                   for k in ("device", "inode"))):
        raise ValueError("invalid wake cursor")
    return data


def _atomic_save(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), mode)
            json.dump(data, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_batch(inbox, cursor):
    try:
        fd = os.open(inbox, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if info.st_uid == os.geteuid():
            raise Refusal(FILE_OWNER_REFUSAL, "refusing inbox file owner")
        if not stat.S_ISREG(info.st_mode):
            return None
        rotated = cursor["inode"] is not None and (
            (info.st_dev, info.st_ino) != (cursor["device"], cursor["inode"])
            or info.st_size < cursor["offset"])
        digest = hashlib.sha256()
        if not rotated:
            # shortcut: rehash history; use a spool checkpoint if logs outgrow polling.
            remaining = cursor["offset"]
            while remaining:
                chunk = source.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            # Detect copy-truncate/regrow between polls, not just a smaller size.
            rotated = bool(remaining or digest.hexdigest() != cursor["sha256"])
        if rotated:
            source.seek(0)
            digest = hashlib.sha256()
        offset = source.tell()
        seen = set(cursor["seen_ids"])
        pending = rotated
        while source.tell() < info.st_size:
            line = source.readline(info.st_size - source.tell())
            if not line or not line.endswith(b"\n"):
                break
            digest.update(line)
            offset = source.tell()
            try:
                dm = json.loads(line)
                identifier = dm.get("id") if isinstance(dm, dict) else None
                if type(identifier) is not int or identifier <= 0:
                    raise ValueError("invalid DM id")
            except (ValueError, UnicodeError):
                # A corrupt complete record still warrants a fixed pull wake.
                pending = True
                continue
            if identifier not in seen:
                seen.add(identifier)
                pending = True
        return {"device": info.st_dev, "inode": info.st_ino, "offset": offset,
                "sha256": digest.hexdigest(), "seen_ids": sorted(seen)}, pending


def _wake(pane):
    from .commands import mesh

    screen = mesh._capture_pane_lines(pane)
    if (not screen or not mesh._is_opencode_pane(screen)
            or mesh._opencode_running(screen) or mesh._pane_in_mode(pane)):
        return False
    if mesh._composer_state(pane) not in ("clear", "wake"):
        return False
    return mesh._tmux_wake(pane, opencode_only=True) is True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="swarph monitor wake-from-inbox", allow_abbrev=False)
    add_arguments(parser)
    args = parser.parse_args(argv)
    if not hasattr(os, "geteuid"):
        parser.error("requires POSIX users and tmux")
    try:
        _check_reader(args.inbox, args.cell)
        state_value = os.environ.get("STATE_DIRECTORY", "")
        state = Path(state_value)
        if (not state.is_absolute() or ":" in state_value
                or not args.inbox.is_absolute() or not args.pane.strip()):
            parser.error("requires absolute STATE_DIRECTORY and --inbox, and a nonempty --pane")
        state.mkdir(exist_ok=True, mode=0o700)
        if not stat.S_ISDIR(state.lstat().st_mode):
            raise ValueError("STATE_DIRECTORY must be a real directory")
        cursor_path = state / "cursor.json"
        cursor = _load_cursor(cursor_path)
        while True:
            ready = _check_reader(args.inbox, args.cell)
            _atomic_save(state / "heartbeat.json", {"at": time.time()}, mode=0o644)
            batch = _read_batch(args.inbox, cursor) if ready else None
            if batch is not None:
                candidate, pending = batch
                # Recheck owners after reading, before the first tmux operation.
                if not pending or (_check_reader(args.inbox, args.cell) and _wake(args.pane)):
                    if candidate != cursor:
                        _atomic_save(cursor_path, candidate)
                        cursor = candidate
            if args.once:
                return 0
            time.sleep(1)
    except Refusal as exc:
        print(f"cell wake: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f"cell wake: state unavailable or invalid ({type(exc).__name__})", file=sys.stderr)
        return 2
