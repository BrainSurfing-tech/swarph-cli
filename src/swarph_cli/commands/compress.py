"""`swarph compress <file>` — compress a machine-read surface. Dry-run by default;
marker opt-in (unmarked -> refuse). See docs spec 2026-06-11-swarph-context-compressor."""
from __future__ import annotations
import argparse, asyncio, shutil, sys, tempfile, os
from pathlib import Path
from ..compress.marker import parse_marker
from ..compress.levers import archival_split, shorthand
from ..compress import verify


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph compress", description="Compress a machine-read context surface.")
    p.add_argument("file", help="path to the surface to compress")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    p.add_argument("--verify-idempotent", action="store_true", help="assert compress(compress(x)) ~= noop")
    p.add_argument("--force", action="store_true", help="override refusals (NOT for source-of-truth)")
    return p


def _atomic_write(path: Path, content: str) -> None:
    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(content)
    os.replace(tmp, path)


def run_compress(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    path = Path(args.file)
    if not path.exists():
        print(f"[compress] no such file: {path}", file=sys.stderr)
        return 2
    source = path.read_text()
    marker = parse_marker(source)
    if marker is None:
        print(f"[compress] REFUSE: {path} carries no swarph:compress marker — leaving untouched", file=sys.stderr)
        return 3

    if marker.lever == "archival":
        archive_path = path.with_suffix(".archive" + path.suffix)
        res = archival_split(source, boundary=marker.boundary, archive_name=archive_path.name)
        if res is None:
            print(f"[compress] REFUSE: no boundary line matching {marker.boundary!r}", file=sys.stderr)
            return 4
        saved = len(source) - len(res.live)
        print(f"[compress] archival: {path.name} {len(source)}B → {len(res.live)}B live "
              f"(+{len(res.archive)}B → {archive_path.name}); saved {saved}B always-loaded")
        if args.apply:
            archive_path.write_text(res.archive)
            _atomic_write(path, res.live)
            print(f"[compress] applied (.bak left, archive {archive_path.name})")
        return 0

    # shorthand
    floor = marker.floor if marker.floor is not None else 0.45
    if not verify.above_floor(source, floor):
        print(f"[compress] REFUSE: already dense (below redundancy floor {floor}) — nothing free to remove", file=sys.stderr)
        return 5
    out = asyncio.run(shorthand(source))
    if not verify.links_preserved(source, out):
        print("[compress] REFUSE: shorthand dropped a link (links not superset)", file=sys.stderr)
        return 6
    if not verify.entries_point_to_source(out, pointer=marker.pointer, base=path.parent):
        print("[compress] REFUSE: an entry lost its pointer-to-source (index-over-source violated)", file=sys.stderr)
        return 6
    if not asyncio.run(verify.verify_expand(source, out)):
        print("[compress] REFUSE: adversarial verify-expand found a dropped fact", file=sys.stderr)
        return 7
    if args.verify_idempotent:
        out2 = asyncio.run(shorthand(out))
        if not verify.idempotent(out, out2):
            print("[compress] REFUSE: not idempotent — second pass kept cutting (signal-eating alarm)", file=sys.stderr)
            return 8
    saved = len(source) - len(out)
    print(f"[compress] shorthand: {path.name} {len(source)}B → {len(out)}B; saved {saved}B ({saved*100//max(len(source),1)}%)")
    if args.apply:
        _atomic_write(path, out)
        print("[compress] applied (.bak left)")
    return 0
