# `swarph timeline` — the OKF temporal on-ramp — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `swarph timeline range/around/since` — a deterministic, $0 temporal-lookup verb over the raw git-backed `~/swarph-timeline/TIMELINE.md`, plus a reusable OKF link-grammar parser and an MCP tool — the temporal on-ramp (sub-project A) of the OKF traversal brain.

**Architecture:** A new `timeline.py` command reads and parses `TIMELINE.md` (one entry per line), filters entries by their embedded ISO timestamp, and prints them (human or OKF-`--json`). Edges come from a new shared `okf_links.py` parser (reused later by `swarph memory` and the `swarph brain` walker). No network, no server — pure file read; complements the gbrain-backed `swarph memory`. Follows the #41 `swarph memory` command/MCP/dispatch patterns.

**Tech Stack:** Python 3, stdlib-only (`argparse`, `re`, `json`, `datetime`, `os`) — public PyPI constraint. Tests via `venv/bin/python -m pytest`.

## Global Constraints

- **Stdlib-only.** No new third-party dependency (swarph-cli is public on PyPI).
- **Deterministic + $0.** Pure file parse + date filter; NO LLM, NO network, NO server. Filter by each entry's **embedded `@ <ISO-timestamp>`** (the swarph-highlight line format), NOT the git commit date.
- **Fail-safe, read-only.** A missing/unreadable `TIMELINE.md` → a stderr note + non-zero exit at the CLI (never a traceback); the MCP tool NEVER raises (returns `[]`). Mirror `swarph memory`'s fail-safe shape.
- **OKF edge model (from the spec):** `--json` emits, per matched entry, `{"node": {"id", "hemisphere":"time", "ts"}, "edges":[{"type":"link","to","to_hemisphere":"knowledge","direction":"out"}...], "cell", "text"}`. The node's canonical id is its ISO timestamp.
- **Link grammar (droplet-pinned — the reusable parser MUST honor exactly):** `[[slug]]`→`slug`; `[[slug|alias]]`→`slug`; `[[slug#heading]]`→`slug`; `![[embed]]`→`embed` (transclusion = an edge); `[text](path.md)`→`path.md`. Order-preserving dedupe.
- **`TIMELINE.md` line format (verbatim):** `- <ISO-ts> · **<cell>** · <text>[ · → [[mem]]]` where `<ISO-ts>` is `%Y-%m-%dT%H:%MZ` (e.g. `2026-07-15T08:51Z`), separator is ` · ` (U+00B7 with spaces). The `· → [[mem]]` memory pointer is optional.
- **Inert-safe.** Additive verb only; NO hook or existing-command behavior changes. **Publish is commander-gated** — this plan ends at green + PR (NOT published). New public verb → **PR-not-merged**, left for review (merge-on-green is standing auth, but not for a fresh public surface).
- **Version:** bump `swarph_cli.__version__` 0.30.0 → **0.31.0** in `pyproject.toml` + `src/swarph_cli/__init__.py` + BOTH pin tests (`tests/test_brain_ask_command.py`, `tests/test_watchdog.py`).
- **Staging discipline:** stage only the files each task names; never `git add -A`; do not stage `.codegraph/` or stray docs.

---

## File Structure

- **Create** `src/swarph_cli/commands/okf_links.py` — the shared OKF link-grammar parser (reusable "leave a tool").
- **Create** `tests/test_okf_links.py` — grammar unit tests.
- **Create** `src/swarph_cli/commands/timeline.py` — the `swarph timeline` verb.
- **Create** `tests/test_timeline_command.py` — timeline unit tests.
- **Modify** `src/swarph_cli/main.py` — register `"timeline"` in `_VERB_HANDLERS`.
- **Modify** `src/swarph_cli/commands/mcp_server.py` — add `swarph_timeline_navigate` + `_timeline_navigate`.
- **Modify** `README.md` — `### swarph timeline` section.
- **Modify** `pyproject.toml`, `src/swarph_cli/__init__.py`, `tests/test_brain_ask_command.py`, `tests/test_watchdog.py` — version bump 0.31.0.

---

### Task 1: The shared OKF link-grammar parser

**Files:**
- Create: `src/swarph_cli/commands/okf_links.py`
- Test: `tests/test_okf_links.py`

**Interfaces:**
- Produces: `parse_okf_links(text: str) -> list[str]` — target slugs/paths in document order, deduped.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_okf_links.py
from swarph_cli.commands.okf_links import parse_okf_links


def test_grammar_table():
    assert parse_okf_links("[[a]]") == ["a"]
    assert parse_okf_links("[[a|Alias A]]") == ["a"]               # alias dropped
    assert parse_okf_links("[[a#Heading]]") == ["a"]               # heading dropped
    assert parse_okf_links("![[embed]]") == ["embed"]             # transclusion is an edge
    assert parse_okf_links("see [txt](notes/b.md)") == ["notes/b.md"]  # md link
    # combined, order-preserving dedupe, markdown non-.md links ignored
    assert parse_okf_links("[[a]] x [[a|z]] y [[c#h]] z [q](http://x)") == ["a", "c"]


def test_empty_and_none():
    assert parse_okf_links("") == []
    assert parse_okf_links(None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_okf_links.py -v`
Expected: FAIL — `ModuleNotFoundError: swarph_cli.commands.okf_links`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/swarph_cli/commands/okf_links.py
"""Shared OKF link-grammar parser — the one true `[[link]]`/markdown-link extractor.

Reused across the OKF traversal brain: `swarph timeline` (this sub-project),
and later `swarph memory` + the `swarph brain` walker. The grammar is PINNED
(design review, droplet 2026-07-15) so the same edge is read identically
everywhere — a traversal brain is only as trustworthy as its least-deterministic
edge, so link parsing must be exact, not naive.

Grammar (target in brackets):
  [[slug]]          -> slug
  [[slug|alias]]    -> slug          (alias is display text, not the target)
  [[slug#heading]]  -> slug          (heading is an intra-page anchor)
  ![[embed]]        -> embed         (transclusion is a reference = an edge)
  [text](path.md)   -> path.md       (standard-markdown link to a .md file)
Order-preserving de-dupe; non-.md markdown links (http, images) are ignored.
"""
from __future__ import annotations

import re

# ![[x]] or [[x]] — capture the target up to the first '#', '|', or ']'.
_WIKI = re.compile(r"!?\[\[\s*([^\]|#]+?)\s*(?:[#|][^\]]*)?\]\]")
# [text](path.md) — capture a .md path target (ignore http/images/other extensions).
_MDLINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+\.md)\s*\)")


def parse_okf_links(text: str) -> list:
    """Extract OKF edge targets from `text`, in document order, de-duped."""
    if not text:
        return []
    hits = []
    for m in re.finditer(r"!?\[\[\s*[^\]|#]+?\s*(?:[#|][^\]]*)?\]\]|\[[^\]]*\]\(\s*[^)\s]+\.md\s*\)", text):
        frag = m.group(0)
        w = _WIKI.match(frag)
        if w:
            hits.append(w.group(1).strip())
            continue
        d = _MDLINK.match(frag)
        if d:
            hits.append(d.group(1).strip())
    return list(dict.fromkeys(hits))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_okf_links.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/okf_links.py tests/test_okf_links.py
git commit -m "feat(okf): shared OKF link-grammar parser (pinned grammar, reusable)"
```

---

### Task 2: `swarph timeline` — parse, date-filter, verbs, human output

**Files:**
- Create: `src/swarph_cli/commands/timeline.py`
- Test: `tests/test_timeline_command.py`

**Interfaces:**
- Consumes: `parse_okf_links` (Task 1).
- Produces: `Entry` (namedtuple `ts: datetime, cell: str, text: str, links: list[str]`); `load_entries(path: str) -> list[Entry]`; `_parse_arg_date(s: str) -> datetime`; `run_timeline(argv: list) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_timeline_command.py
import datetime as dt
from swarph_cli.commands import timeline

SAMPLE = (
    "# swarph timeline\n"
    "- 2026-07-10T21:02Z · **lab-ovh** · built tunnel-watch · → [[feedback_x]]\n"
    "- 2026-07-13T04:24Z · **lab-ovh** · credential isolation note [[reference_swairm_repo]]\n"
    "- 2026-07-15T08:51Z · **gridiron** · reaper operational · → [[feedback_y]]\n"
)


def _write(tmp_path):
    p = tmp_path / "TIMELINE.md"
    p.write_text(SAMPLE, encoding="utf-8")
    return str(p)


def test_load_entries_parses_ts_cell_links(tmp_path):
    entries = timeline.load_entries(_write(tmp_path))
    assert len(entries) == 3
    e = entries[0]
    assert e.ts == dt.datetime(2026, 7, 10, 21, 2, tzinfo=dt.timezone.utc)
    assert e.cell == "lab-ovh"
    assert e.links == ["feedback_x"]
    # inline [[link]] (not just the → pointer) is captured
    assert entries[1].links == ["reference_swairm_repo"]


def test_range_since_around(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    assert timeline.run_timeline(["range", "2026-07-12", "2026-07-14"]) == 0
    out = capsys.readouterr().out
    assert "2026-07-13T04:24Z" in out and "2026-07-10" not in out and "2026-07-15" not in out
    assert timeline.run_timeline(["since", "2026-07-14"]) == 0
    assert "2026-07-15T08:51Z" in capsys.readouterr().out
    assert timeline.run_timeline(["around", "2026-07-13", "--window", "1d"]) == 0
    around = capsys.readouterr().out
    assert "2026-07-13T04:24Z" in around and "2026-07-15" not in around


def test_missing_file_is_fail_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SWARPH_TIMELINE", str(tmp_path / "nope.md"))
    rc = timeline.run_timeline(["since", "2026-07-01"])
    assert rc == 1                       # non-zero, not a traceback
    assert "timeline" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py -v`
Expected: FAIL — `ModuleNotFoundError: ...timeline` / `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/swarph_cli/commands/timeline.py
"""``swarph timeline`` — DETERMINISTIC temporal lookup over the git-backed timeline.

The TEMPORAL on-ramp of the OKF traversal brain (sub-project A). Reads the raw
``~/swarph-timeline/TIMELINE.md`` (the append-only, git-merged shared log) and
answers date-scoped questions — ``range``/``around``/``since`` — with NO model,
NO network, NO server ($0, deterministic). Each entry is an OKF *temporal node*:
its canonical id is its ISO timestamp; its edges are the ``[[links]]`` it names
(into the knowledge hemisphere). Complements the semantic ``brain-ask`` and the
structural ``codegraph``/``memory``.

Filters by each entry's EMBEDDED ``@ <ISO-timestamp>`` (the swarph-highlight line
format), never the git commit date — the entry's own date is canonical. Read-only;
stdlib-only; fail-safe (a missing/unreadable file → stderr note + non-zero, never
a traceback).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import namedtuple

from swarph_cli.commands.okf_links import parse_okf_links

Entry = namedtuple("Entry", "ts cell text links")

_DEFAULT_TIMELINE = os.path.expanduser("~/swarph-timeline/TIMELINE.md")
# - <ISO-ts> · **<cell>** · <rest>
_LINE = re.compile(r"^- (?P<ts>\S+)\s+·\s+\*\*(?P<cell>[^*]+)\*\*\s+·\s+(?P<rest>.*)$")


def _timeline_path() -> str:
    return os.environ.get("SWARPH_TIMELINE", _DEFAULT_TIMELINE)


def _parse_entry_ts(s: str) -> dt.datetime | None:
    """Parse the entry timestamp ``2026-07-15T08:51Z`` (minute precision, UTC)."""
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def load_entries(path: str) -> list:
    """Parse TIMELINE.md into Entry tuples. Raises OSError if unreadable (caller
    is fail-safe). Malformed lines (no match / bad ts) are skipped, not fatal."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = _LINE.match(line.rstrip("\n"))
            if not m:
                continue
            ts = _parse_entry_ts(m.group("ts"))
            if ts is None:
                continue
            rest = m.group("rest")
            entries.append(Entry(ts=ts, cell=m.group("cell").strip(),
                                 text=rest, links=parse_okf_links(rest)))
    return entries


def _parse_arg_date(s: str) -> dt.datetime:
    """Parse a CLI date arg. Accepts ``YYYY-MM-DD`` or a full ISO timestamp."""
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable date {s!r} (use YYYY-MM-DD)")


def _fmt_human(e: Entry) -> str:
    ts = e.ts.strftime("%Y-%m-%dT%H:%MZ")
    links = ("  " + " ".join(f"[[{l}]]" for l in e.links)) if e.links else ""
    return f"{ts} · {e.cell} · {e.text}{links}"


def run_timeline(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="swarph timeline",
        description="Deterministic temporal lookup over the git-backed swarph timeline "
                    "(range/around/since). $0, no model, no network.")
    sub = p.add_subparsers(dest="subcommand")
    pr = sub.add_parser("range", help="entries between two dates (inclusive)")
    pr.add_argument("start"); pr.add_argument("end")
    pa = sub.add_parser("around", help="entries within a window of a date")
    pa.add_argument("date"); pa.add_argument("--window", default="3d", help="e.g. 3d, 12h")
    ps = sub.add_parser("since", help="entries on/after a date")
    ps.add_argument("date")
    for sp in (pr, pa, ps):
        sp.add_argument("--json", action="store_true", help="OKF node/edge JSON")
    args = p.parse_args(argv)
    if not args.subcommand:
        p.print_help(); return 0

    try:
        entries = load_entries(_timeline_path())
    except OSError as e:
        print(f"swarph timeline: cannot read {_timeline_path()} ({e})", file=sys.stderr)
        return 1

    try:
        lo, hi = _bounds(args)
    except ValueError as e:
        print(f"swarph timeline: {e}", file=sys.stderr)
        return 1
    hits = [e for e in entries if (lo is None or e.ts >= lo) and (hi is None or e.ts <= hi)]

    if getattr(args, "json", False):
        print(json.dumps([_as_okf(e) for e in hits], indent=2))
    else:
        for e in hits:
            print(_fmt_human(e))
    return 0


def _parse_window(w: str) -> dt.timedelta:
    m = re.fullmatch(r"(\d+)([dh])", w.strip())
    if not m:
        raise ValueError(f"bad --window {w!r} (use e.g. 3d or 12h)")
    n = int(m.group(1))
    return dt.timedelta(days=n) if m.group(2) == "d" else dt.timedelta(hours=n)


def _bounds(args):
    """(lo, hi) datetime bounds for the chosen subcommand; end-of-day for bare dates."""
    eod = dt.timedelta(hours=23, minutes=59)
    if args.subcommand == "range":
        return _parse_arg_date(args.start), _parse_arg_date(args.end) + eod
    if args.subcommand == "since":
        return _parse_arg_date(args.date), None
    if args.subcommand == "around":
        c = _parse_arg_date(args.date); w = _parse_window(args.window)
        return c - w, c + w + eod
    return None, None


def _as_okf(e: Entry) -> dict:
    ts = e.ts.strftime("%Y-%m-%dT%H:%MZ")
    return {
        "node": {"id": ts, "hemisphere": "time", "ts": ts},
        "edges": [{"type": "link", "to": l, "to_hemisphere": "knowledge",
                   "direction": "out"} for l in e.links],
        "cell": e.cell, "text": e.text,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py -v`
Expected: PASS (4 tests). (The `_as_okf`/`--json` path is exercised in Task 3's test; it's included here so the module is complete.)

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/timeline.py tests/test_timeline_command.py
git commit -m "feat(timeline): swarph timeline range/around/since over the git timeline"
```

---

### Task 3: `--json` OKF node/edge output

**Files:**
- Modify: `src/swarph_cli/commands/timeline.py` (already contains `_as_okf` + `--json` from Task 2 — this task adds its dedicated test)
- Test: `tests/test_timeline_command.py`

**Interfaces:**
- Consumes: `run_timeline`, `_as_okf` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
def test_json_emits_okf_node_edges(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    assert timeline.run_timeline(["since", "2026-07-14", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    rec = payload[0]
    assert rec["node"] == {"id": "2026-07-15T08:51Z", "hemisphere": "time",
                           "ts": "2026-07-15T08:51Z"}
    assert rec["edges"] == [{"type": "link", "to": "feedback_y",
                             "to_hemisphere": "knowledge", "direction": "out"}]
    assert rec["cell"] == "gridiron"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py::test_json_emits_okf_node_edges -v`
Expected: PASS if Task 2's `_as_okf` is correct. If it FAILS, fix `_as_okf`/`--json` in `timeline.py` until the OKF schema matches exactly. (This task's value is locking the schema with an assertion; write it first and confirm it drives the shape.)

- [ ] **Step 3: (only if the test failed) align `_as_okf` to the asserted schema**

Ensure `_as_okf` returns exactly `{"node": {"id","hemisphere":"time","ts"}, "edges":[{"type":"link","to","to_hemisphere":"knowledge","direction":"out"}], "cell", "text"}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/timeline.py tests/test_timeline_command.py
git commit -m "test(timeline): lock the OKF node/edge --json schema"
```

---

### Task 4: Register `timeline` in the CLI dispatch

**Files:**
- Modify: `src/swarph_cli/main.py`
- Test: `tests/test_timeline_command.py`

**Interfaces:**
- Consumes: `run_timeline` (Task 2).

- [ ] **Step 1: Write the failing test**

```python
def test_timeline_registered_in_dispatch():
    from swarph_cli import main as m
    assert m._VERB_HANDLERS["timeline"] == "swarph_cli.commands.timeline.run_timeline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py::test_timeline_registered_in_dispatch -v`
Expected: FAIL — `KeyError: 'timeline'`.

- [ ] **Step 3: Add the dispatch entry**

In `src/swarph_cli/main.py`, add to the `_VERB_HANDLERS` dict (next to `"memory"`):

```python
    "timeline": "swarph_cli.commands.timeline.run_timeline",
```

- [ ] **Step 4: Run test + smoke**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py::test_timeline_registered_in_dispatch -v` → PASS.
Run: `venv/bin/python -m swarph_cli timeline --help` → prints range/around/since help, exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/main.py tests/test_timeline_command.py
git commit -m "feat(timeline): register `swarph timeline` verb in CLI dispatch"
```

---

### Task 5: `swarph_timeline_navigate` MCP tool

**Files:**
- Modify: `src/swarph_cli/commands/mcp_server.py`
- Test: `tests/test_timeline_command.py`

**Interfaces:**
- Consumes: `timeline.load_entries`, `timeline._timeline_path`, `timeline._as_okf`, `timeline._bounds` (Task 2).
- Produces: `_timeline_navigate(op, start=None, end=None, date=None, window="3d") -> list`; registered `swarph_timeline_navigate` MCP tool.

- [ ] **Step 1: Write the failing test**

```python
def test_timeline_navigate_failsafe(tmp_path, monkeypatch):
    from swarph_cli.commands import mcp_server
    monkeypatch.setenv("SWARPH_TIMELINE", _write(tmp_path))
    got = mcp_server._timeline_navigate("since", date="2026-07-14")
    assert got and got[0]["node"]["id"] == "2026-07-15T08:51Z"
    # fail-safe: unknown op / bad input NEVER raises
    assert mcp_server._timeline_navigate("bogus") == []
    monkeypatch.setenv("SWARPH_TIMELINE", str(tmp_path / "nope.md"))
    assert mcp_server._timeline_navigate("since", date="2026-07-01") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py::test_timeline_navigate_failsafe -v`
Expected: FAIL — `AttributeError: module 'mcp_server' has no attribute '_timeline_navigate'`.

- [ ] **Step 3: Add the wrapper + register the tool**

In `src/swarph_cli/commands/mcp_server.py`, extend the existing import line to add `timeline`:

```python
from swarph_cli.commands import brain_ask, memory, timeline
```

Add the wrapper (near `_memory_navigate`):

```python
def _timeline_navigate(op: str, start: str = "", end: str = "", date: str = "",
                       window: str = "3d"):
    """Deterministic temporal lookup over the git timeline. op: 'range'|'around'|'since'.
    Returns OKF node/edge records (list). Fail-safe: any bad input/op/read → [] (never raises)."""
    import argparse as _a
    try:
        ns = _a.Namespace(subcommand=op, start=start, end=end, date=date, window=window)
        lo, hi = timeline._bounds(ns)
        entries = timeline.load_entries(timeline._timeline_path())
        hits = [e for e in entries if (lo is None or e.ts >= lo) and (hi is None or e.ts <= hi)]
        return [timeline._as_okf(e) for e in hits]
    except Exception:
        return []
```

Register the tool inside the existing FastMCP builder (beside `swarph_memory_navigate`):

```python
    @mcp.tool()
    def swarph_timeline_navigate(op: str, start: str = "", end: str = "",
                                 date: str = "", window: str = "3d"):
        """Deterministic temporal lookup over the swarph timeline: op='range'|'around'|'since'.
        The temporal hemisphere of the OKF traversal brain — entries are dated OKF nodes with
        [[link]] edges into knowledge. Complements semantic recall; $0, no model."""
        return _timeline_navigate(op, start=start, end=end, date=date, window=window)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/python -m pytest tests/test_timeline_command.py::test_timeline_navigate_failsafe -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/swarph_cli/commands/mcp_server.py tests/test_timeline_command.py
git commit -m "feat(timeline): swarph_timeline_navigate MCP tool (deterministic temporal nav)"
```

---

### Task 6: README §timeline + version bump to 0.31.0

**Files:**
- Modify: `README.md`, `pyproject.toml`, `src/swarph_cli/__init__.py`, `tests/test_brain_ask_command.py`, `tests/test_watchdog.py`

**Interfaces:** none (docs + version constants).

- [ ] **Step 1: Bump the pin tests first (write the failing assertion)**

Grep the current pins: `grep -rn "0.30.0" tests/`. In `tests/test_brain_ask_command.py` and `tests/test_watchdog.py`, change the asserted version `"0.30.0"` → `"0.31.0"` AND rename the test functions `test_version_is_0_30_0` → `test_version_is_0_31_0`.

- [ ] **Step 2: Run to verify they fail**

Run: `venv/bin/python -m pytest tests/test_brain_ask_command.py tests/test_watchdog.py -k version -v`
Expected: FAIL (code still reports 0.30.0).

- [ ] **Step 3: Bump the version constants**

`pyproject.toml`: `version = "0.31.0"`. `src/swarph_cli/__init__.py`: `__version__ = "0.31.0"`.

- [ ] **Step 4: Run to verify they pass**

Run: `venv/bin/python -m pytest tests/test_brain_ask_command.py tests/test_watchdog.py -k version -v`
Expected: PASS.

- [ ] **Step 5: Add the README section**

In `README.md`, after the `### swarph memory` section, add:

````markdown
### `swarph timeline` (v0.31.0)

**Deterministic, $0** temporal lookup over the git-backed swarph timeline — the temporal hemisphere of the OKF traversal brain (counterpart to `swarph codegraph` for code and `swarph memory` for knowledge). No model, no network — it parses the timeline file.

```
swarph timeline range <start> <end>          # entries between two dates (inclusive)
swarph timeline around <date> [--window 3d]  # entries within a window of a date
swarph timeline since <date>                 # entries on/after a date
```

Dates are `YYYY-MM-DD` (or a full ISO timestamp). Source is `~/swarph-timeline/TIMELINE.md` (override with `SWARPH_TIMELINE`). Add `--json` for the OKF node/edge payload (each entry is a dated node with `[[link]]` edges into knowledge). Also exposed to any MCP host as `swarph_timeline_navigate`.
````

- [ ] **Step 6: Run the FULL suite (no regressions)**

Run: `venv/bin/python -m pytest -q`
Expected: all pass (new okf_links + timeline tests + bumped pins; no regressions).

- [ ] **Step 7: Commit**

```bash
git add README.md pyproject.toml src/swarph_cli/__init__.py tests/test_brain_ask_command.py tests/test_watchdog.py
git commit -m "docs(timeline): README §timeline; bump 0.31.0"
```

---

## Self-Review

**1. Spec coverage** (sub-project A of the traversal-brain spec):
- Shared OKF link-grammar parser, pinned grammar → Task 1 ✓ (all five forms + dedupe).
- `swarph timeline range/around/since` over raw git TIMELINE.md, embedded-ts filter, fail-safe → Task 2 ✓.
- OKF `--json` node/edge schema (node=ts/hemisphere=time; edges=[[links]] out→knowledge; cell) → Task 3 ✓.
- `swarph_timeline_navigate` MCP tool, fail-safe → Task 5 ✓.
- CLI dispatch registration → Task 4 ✓.
- README + version bump → Task 6 ✓.
- Out of scope (correctly absent): #42 gbrain graph, the `swarph brain` walker, `--root` file-native walk, the graph-summary onboarding output (those belong to sub-projects B/C).

**2. Placeholder scan:** no TBD/TODO; every code step is complete. Task 3 is a schema-locking test over Task 2's `_as_okf` — the one place an implementer might find the assertion already green; the step says so explicitly and to fix `_as_okf` if not.

**3. Type consistency:** `parse_okf_links → list[str]` (Task 1) consumed by `load_entries` (Task 2). `Entry(ts: datetime, cell, text, links)` used in Tasks 2/3/5. `_bounds(args)`/`_as_okf(e)`/`_timeline_path()`/`load_entries(path)` signatures identical across Task 2 (def), Task 3 (test), Task 5 (MCP wrapper reuse). The MCP wrapper builds an `argparse.Namespace` with exactly the fields `_bounds` reads (`subcommand`, `start`, `end`, `date`, `window`). `_VERB_HANDLERS["timeline"]` (Task 4) matches the module path `swarph_cli.commands.timeline.run_timeline`.
