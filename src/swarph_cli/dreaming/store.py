"""Private, file-at-a-time memory writes with checked whole-corpus recovery."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import time
import uuid


class Refused(Exception):
    """A safe fixed diagnostic, not note text."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def private_dir(path):
    if path.is_symlink():
        raise Refused("symlinked state directory")
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for created in reversed(missing):
        created.mkdir(exist_ok=True, mode=0o700)
        if created.is_symlink():
            raise Refused("symlinked state directory")
        sync_dir(created.parent)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise Refused("state directory must be private (0700)")
    return path


def atomic_write(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(prefix=".dreaming-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), mode)
            else:
                os.chmod(temporary, mode)
        os.replace(temporary, path)  # A single file, NEVER the corpus directory.
        sync_dir(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def corpus_lock(corpus):
    # Shared by all cells pointing at this corpus, not keyed by HOME or cell.
    path = corpus.parent / (".dreaming-" + digest(os.fsencode(corpus))[:24] + ".lock")
    if path.is_symlink():
        raise Refused("symlinked corpus lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except (BlockingIOError, PermissionError):
        raise Refused("corpus is locked or inaccessible") from None
    finally:
        os.close(fd)


def tree(corpus):
    result = {}
    for path in sorted(corpus.rglob("*")):
        name = path.relative_to(corpus).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode) & 0o777
        if stat.S_ISLNK(info.st_mode):
            result[name] = {"kind": "symlink", "target": os.readlink(path), "mode": mode}
        elif stat.S_ISDIR(info.st_mode):
            result[name] = {"kind": "dir", "mode": mode}
        elif stat.S_ISREG(info.st_mode):
            result[name] = {"kind": "file", "sha256": digest(path.read_bytes()), "mode": mode}
        else:
            raise Refused("corpus contains an unsupported special file")
    return result


def idle(corpus, minutes):
    newest = max([corpus.stat().st_mtime] + [p.lstat().st_mtime for p in corpus.rglob("*")])
    if not math.isfinite(minutes) or minutes < 0 or time.time() - newest < minutes * 60:
        raise Refused("memory was written inside the idle window")


def read_backup(path, corpus):
    with tarfile.open(path, "r:gz") as archive:
        if archive.pax_headers.get("dreaming.corpus") != str(corpus):
            raise Refused("backup belongs to another corpus")
        expected = json.loads(archive.pax_headers["dreaming.tree"])
        members = archive.getmembers()
        if len(members) != len(expected) or len({m.name for m in members}) != len(members):
            raise Refused("backup file count mismatch")
        contents = {}
        for member in members:
            name = member.name
            if name not in expected or Path(name).is_absolute() or ".." in Path(name).parts:
                raise Refused("unsafe backup path")
            entry = expected[name]
            if member.isfile() or member.islnk():
                data = archive.extractfile(member).read()
                if entry["kind"] != "file" or digest(data) != entry["sha256"]:
                    raise Refused("backup content mismatch")
                contents[name] = data
            elif member.isdir() and entry["kind"] == "dir":
                continue
            elif member.issym() and entry["kind"] == "symlink" and member.linkname == entry["target"]:
                continue
            else:
                raise Refused("unsupported backup entry")
        return expected, contents


def backup(corpus, state, keep):
    if keep < 1:
        raise Refused("keep-backups must be positive")
    directory = private_dir(state / "backups")
    before = tree(corpus)
    path = directory / (str(time.time_ns()) + "-" + uuid.uuid4().hex + ".tar.gz")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT,
                          pax_headers={"dreaming.corpus": str(corpus),
                                       "dreaming.tree": json.dumps(before)}) as archive:
            for name in before:
                archive.add(corpus / name, arcname=name, recursive=False)
        stream.flush()
        os.fsync(stream.fileno())
    sync_dir(directory)
    read_backup(path, corpus)  # Count AND hashes checked before any live write.
    if tree(corpus) != before:
        raise Refused("corpus changed during backup")
    for old in sorted(directory.glob("*.tar.gz"))[:-keep]:
        old.unlink()
    return path


def matches(corpus, manifest, name):
    path = corpus / name
    return (not path.is_symlink() and path.is_file()
            and digest(path.read_bytes()) == manifest["files"][name]["sha256"])


def is_index(name):
    name = Path(name).name.lower()
    return name.startswith(("memory", "index_")) or name in {"_index.md", "index.md"}


def note_types(name, text):
    kinds = {kind for kind in ("user", "feedback")
             if Path(name).name.lower().startswith(kind + "_")}
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        import yaml  # Existing CLI dependency; safe loader, not a hand-written YAML subset.
        try:
            end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
            header = yaml.safe_load("\n".join(lines[1:end])) or {}
            metadata = header.get("metadata", {})
            for kind in (header.get("type", ""),
                         metadata.get("type", "") if isinstance(metadata, dict) else ""):
                if not isinstance(kind, str):
                    kinds.add("ambiguous")
                elif kind.lower() in {"user", "feedback"}:
                    kinds.add(kind.lower())
        except (yaml.YAMLError, StopIteration, AttributeError):
            kinds.add("ambiguous")  # Ambiguous frontmatter needs the owner's explicit opt-in.
    return kinds


def protected(path):
    return bool(note_types(path.name, path.read_text(encoding="utf-8")))


def apply_edits(corpus, out, manifest, edits, state, minutes, keep, owner):
    idle(corpus, minutes)
    path = backup(corpus, state, keep)
    archive = private_dir(state / "archive" / uuid.uuid4().hex)
    result = {"backup": str(path), "applied": [], "skipped": [], "propose_only": [], "index_edits_held": 0}
    for edit in edits:
        files = edit["files"]
        if not all(matches(corpus, manifest, name) for name in files):
            result["skipped"].append({"op": edit["op"], "files": files})
            continue
        index_edit = any(is_index(name) for name in files)
        if not owner and (index_edit or any(protected(corpus / name) for name in files)):
            result["propose_only"].append({"op": edit["op"], "files": files})
            result["index_edits_held"] += int(index_edit)
            continue
        target = edit.get("target")
        changed = files if edit["op"] in {"merge", "retire"} else [target]
        # Every changed pre-image is durable outside the corpus before mutation.
        for name in changed:
            data = (corpus / name).read_bytes()
            if digest(data) != manifest["files"][name]["sha256"]:
                raise Refused("memory changed while archiving")
            atomic_write(archive / name, data)
        for name in changed:
            if not matches(corpus, manifest, name):
                result["skipped"].append({"op": edit["op"], "files": [name]})
                continue
            if name == target:
                atomic_write(corpus / name, edit["text"].encode("utf-8"))
            else:
                (corpus / name).unlink()  # Durable archive copy already exists.
            result["applied"].append({"op": edit["op"], "files": [name]})
    atomic_write(out / "apply-result.json", json.dumps(result).encode())
    return result


def restore(corpus, path, state, minutes, keep):
    expected, contents = read_backup(path, corpus)
    idle(corpus, minutes)
    recovery = backup(corpus, state, keep)
    for name in sorted(expected, key=lambda n: (len(Path(n).parts), n)):
        dest = corpus / name
        for parent in dest.parents:
            if parent == corpus:
                break
            if parent.is_symlink():
                raise Refused("symlinked restore parent")
        entry = expected[name]
        if entry["kind"] == "dir":
            if dest.is_symlink():
                raise Refused("symlinked restore directory")
            dest.mkdir(exist_ok=True, mode=entry["mode"])
        elif entry["kind"] == "file":
            atomic_write(dest, contents[name], entry["mode"])
        else:
            temporary = dest.parent / (".dreaming-" + uuid.uuid4().hex)
            try:
                os.symlink(entry["target"], temporary)
                os.replace(temporary, dest)
            finally:
                if temporary.is_symlink():
                    temporary.unlink()
    return {"restored": len(contents), "recovery_backup": str(recovery)}
