"""Card #1445 — opencode Go usage reader, writing ``opencode.json``.

The cell-opencode timer (meta-muse #6552) writes
``/var/lib/swarph-usage/opencode-go.json`` hourly. That file's body is
``{provider, source, read_at, response}`` (DM 104684). ``response`` is
the Go usage API body. The live USAGE-JSON shape (DM 104655, post
98479) is ``{read_at, response.usage.{rolling,weekly,monthly}.{status,
percent,resetsAt}}``, so the windows live at ``response.usage`` when
``response`` is the API body, and at ``response.response.usage`` when
``response`` is that USAGE-JSON wrapper. An override path comes from
``--opencode-file`` or ``SWARPH_OPENCODE_USAGE_FILE``.

``read_at`` in ``opencode.json`` is the file body's own ``read_at``.
It is not the file mtime, not a nested ``response.read_at``, and not
the sync clock. A window whose ``percent`` is missing, non-numeric,
NaN, or Infinity is omitted, never stored as a fresh zero. Nothing is
written when ``read_at`` is missing or no window maps. The write is
atomic. Only percent and resetsAt are
copied: other keys (status text, credentials, source strings) are not
written.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROVIDER = "opencode"
SOURCE = (
    "opencode go usage file response.usage "
    "(read_at is the file body's read_at, not the file mtime)"
)
FILENAME = "opencode.json"
DEFAULT_USAGE_FILE = "/var/lib/swarph-usage/opencode-go.json"
ENV_USAGE_FILE = "SWARPH_OPENCODE_USAGE_FILE"

#: Window names in the Go usage body, in table order before the alpha sort.
WINDOW_NAMES = ("rolling", "weekly", "monthly")


def _iso_z(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def opencode_usage_path(explicit: "str | Path | None" = None) -> Path:
    """Where the timer file lives. Flag, then env, then the #6552 default.

    Tests pass an explicit tmp path. The default is not under the usage
    dir, so the raw timer file is never itself a pool row.
    """
    if explicit is not None:
        return Path(explicit)
    raw = os.environ.get(ENV_USAGE_FILE)
    if raw:
        return Path(raw).expanduser()
    return Path(DEFAULT_USAGE_FILE)


def _usage_object(document: dict) -> "dict | None":
    """The ``{rolling, weekly, monthly}`` object, or None.

    Two live nestings: ``response.usage`` (the GET body stored as
    ``response``) and ``response.response.usage`` (the USAGE-JSON body
    stored as ``response``). The first one that names a known window
    wins. A usage object with no known window is not a reading.
    """
    response = document.get("response")
    if not isinstance(response, dict):
        return None
    candidates = []
    direct = response.get("usage")
    if isinstance(direct, dict):
        candidates.append(direct)
    inner = response.get("response")
    if isinstance(inner, dict):
        nested = inner.get("usage")
        if isinstance(nested, dict):
            candidates.append(nested)
    for usage in candidates:
        if any(name in usage for name in WINDOW_NAMES):
            return usage
    return None


def _percent(value):
    """A real finite used-percent, or None.

    Bool, missing, non-numeric, NaN, and Infinity are not a number and
    not a zero. The window that carried them is omitted.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _resets_to_iso(value):
    """Epoch or ISO -> UTC ISO Z. Unparseable is None, never 'now'."""
    from swarph_cli.usage import parse_time

    parsed = parse_time(value)
    if parsed is None:
        return None
    return _iso_z(parsed)


def windows_from_usage(usage: dict) -> dict:
    """``{name: {used_percentage, resets_at}}`` for windows that have percent.

    A missing, non-numeric, NaN, or Infinity ``percent`` drops that
    window. It is not written as 0. A missing ``resetsAt`` keeps the
    window with ``resets_at`` null. Unknown keys are ignored.
    """
    windows = {}
    for name in WINDOW_NAMES:
        entry = usage.get(name)
        if not isinstance(entry, dict):
            continue
        used = _percent(entry.get("percent"))
        if used is None:
            continue
        windows[name] = {
            "used_percentage": used,
            "resets_at": _resets_to_iso(entry.get("resetsAt")),
        }
    return windows


def build_document(usage: dict, *, read_at: datetime) -> dict:
    """The opencode.json body. Empty windows dict when nothing mappable.

    ``read_at`` is the timer file body's ``read_at``. It is not the
    sync clock and not the file mtime.
    """
    return {
        "provider": PROVIDER,
        "source": SOURCE,
        "read_at": _iso_z(read_at),
        "windows": windows_from_usage(usage),
    }


def _body_read_at(document: dict):
    """The file body's ``read_at`` as a datetime, or None.

    A nested ``response.read_at`` does not count: that clock must not
    make the pool row look fresher than the file body.
    """
    from swarph_cli.usage import parse_time

    if not isinstance(document, dict):
        return None
    return parse_time(document.get("read_at"))


def write_opencode_json(*, usage_file=None, usage_dir=None) -> "dict | None":
    """Read the timer file and atomically write ``usage/opencode.json``.

    Returns {"path", "windows", "usage_file"} on a write, None when
    nothing was written (missing file, unreadable JSON, no body
    ``read_at``, or no window with a finite numeric percent). A missing file
    is not an error: sync still refreshes openai.json. Never raises
    on I/O. Never copies keys other than percent and resetsAt.
    """
    from swarph_cli.usage import default_usage_dir

    source_path = opencode_usage_path(usage_file)
    try:
        with open(source_path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    moment = _body_read_at(document)
    usage = _usage_object(document)
    if moment is None or usage is None:
        return None
    drafted = build_document(usage, read_at=moment)
    if not drafted["windows"]:
        return None
    target_dir = Path(usage_dir) if usage_dir is not None else default_usage_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / FILENAME
        fd, tmp_name = tempfile.mkstemp(dir=str(target_dir),
                                        prefix=FILENAME + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(drafted, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError:
        return None
    return {"path": str(target), "windows": drafted["windows"],
            "usage_file": str(source_path)}
