"""``swarph board cards dev-extract`` — a small pinned "dev extract" for spec/plan
cards (card #1410, #559 phase 2; re-spec of #1396).

The extract JOINS two halves, each with its own as-of pin:

* Codegraph half (READ-ONLY ``~/.swarph/codegraph/index.db``): target symbols
  with file:line, caller counts, matching routes + params. Per repo: commit
  sha from that repo's ``.codegraph/indexed_origin_main``
  file (card #1393) plus the index time from ``repos.indexed_at``. No
  covering-tests section (name-match is not coverage — removed #1410).
* Federation half (READ-ONLY ``~/.swarph/codegraph/federation-report.txt`` +
  ``structurizr/workspace.json``): the report's header stamp (build time)
  and its ``Layer N:`` header lines only, plus the deploy units running the
  visible target repos. NO rels, depends-on, blast-radius, arrow, or
  import-site lines of any kind are printed (kept by LINE SHAPE, not words).
  Pinned with the federation build time from the stamp (never the file
  mtime); labelled STALE when older than the last repo.merged for a repo.

Posting is via the existing board path: the built body is posted with the
same ``/messages`` + ``_card_say_payload`` machinery as
``swarph board cards say`` (see ``board._run_cards_say``) whenever ``--card``
is given WITHOUT ``--print-only``. ``--print-only`` (or no ``--card``)
prints the extract and posts nothing; nothing is ever deployed.

Honesty rules (graded r4): every zero is printed as a FLOOR
(``0 callers (floor: HTTP callers not linked yet)``), never as a fact; a
missing or unreadable source prints ``unavailable: <reason>``, never a zero.

Privacy rule (#1410): a card's readers must not receive symbols from a
repo they cannot read today. The composition mirrors
``swarph_cli/commands/codegraph.py:145`` (``_visible_repos``: public repos
are always visible; a private repo is visible when the caller is in
``allowlist[repo]``) with the caller replaced by the card's board project:
public repos are always visible, and the repos listed for the card's project
slug in ``~/.swarph/codegraph/card-allowlist.json`` (commander-approved,
``{project_slug: [repo, ...]}``) are visible too. The card's project is
resolved the way the board resolves it for a card artifact — the card row
carries ``project_id`` (``GET /board/cards/{id}`` has no slug field) and the
slug comes from the ``GET /board/projects`` rows matched by id (same
int-or-str equality as ``board._project_owner_orchestrator``). Malformed or
absent file, or a project with no entry, means public only (fail closed).
Private (or unknown-visibility) repos outside that set are WITHHELD and the
extract NAMES the withheld repo + why instead of leaking symbol details.
``~/.swarph/codegraph/allowlist.json`` is NEVER read here (different
meaning: per-cell read authority). Fail closed (#111), never open.
Withheld-name filtering scans path components too, and no rels of any kind
are printed.

Bounded queries: EVERY row-returning SQL statement in this module carries an
explicit ``LIMIT`` (see ``test_1396_dev_extract.py::test_every_row_query_is_bounded``,
which fails if a new unbounded row query is added). Caller counts are
``COUNT(*)`` aggregates — no row materialization — over the ~162k-row edge
table (measured <0.5s on the live index). Federation file reads are capped
by line/relationship caps; the renderer enforces ``--max-lines`` and states
what was cut.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess

DEFAULT_INDEX = os.path.expanduser("~/.swarph/codegraph/index.db")
DEFAULT_FED_REPORT = os.path.expanduser("~/.swarph/codegraph/federation-report.txt")
DEFAULT_STRUCTURIZR_WS = os.path.expanduser(
    "~/.swarph/codegraph/structurizr/workspace.json"
)
DEFAULT_FED_DOCS_DB = os.path.expanduser("~/.swarph/federation/outputs/fed-docs.db")
DEFAULT_CARD_ALLOWLIST = os.path.expanduser(
    "~/.swarph/codegraph/card-allowlist.json"
)

MAX_AUTO_TOKENS = 8
ROW_LIMIT = 8
FED_REPORT_LINE_CAP = 300
DEFAULT_MAX_LINES = 60

FLOOR_CALLERS = "0 callers (floor: HTTP callers not linked yet)"
FLOOR_ROUTES = "0 routes (floor: route registry not linked for this symbol)"

_TOKEN_RE = re.compile(r"`([^`]+)`")
_SNAKE_RE = re.compile(r"(?<![A-Za-z0-9_])_?[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}")
_ROUTE_RE = re.compile(r"(?:POST|GET|PUT|PATCH|DELETE)\s+/\S+|/\S*chat\S*|/\S*sync\S*")

# Junk-target stoplist (card #788 mines backticked `?` and `none`; neither
# can ever be a symbol, and `none` even resolves to a same-named `variable`
# row in a private repo, printing a bogus also-defined line).
_JUNK_WORDS = frozenset({"none", "null", "nil", "n/a", "na", "undefined"})
_ROUTE_TOKEN_RE = re.compile(r"(?:POST|GET|PUT|PATCH|DELETE)\s+/\S+")
_SHELL_META_RE = re.compile(r"[=|><;$&*`(){}\[\]\"']")


def is_lookup_token(tok: str) -> bool:
    """True when `tok` is worth an index lookup (card #792/#788 junk filter).

    Drops, before any lookup: single-char spans (`?`); bare junk words
    (`none`, ...); multi-word spans that are NOT a route (`pip install -U`,
    `swarph version`, `import json, sys`, `cards graph`, backticked shell
    commands); spans carrying shell/code metacharacters (`delivery =
    sd.get("delivery`, `json.loads(row["menu_json`). A `METHOD /path` span
    is a route target and is kept. Anything kept either exact-matches a
    symbol or resolves to nothing — never to 76 unrelated routes.
    """
    t = (tok or "").strip()
    if len(t) < 2:
        return False
    if t.lower() in _JUNK_WORDS:
        return False
    if _ROUTE_TOKEN_RE.fullmatch(t):
        return True
    if re.search(r"\s", t):
        return False
    if _SHELL_META_RE.search(t):
        return False
    return True


def _normalize_token(tok: str) -> str:
    """Call-site / def-site spelling → bare symbol name (pure).

    `_format_graph(rows)` (call with args) and `def _format_graph` (def-site)
    must both resolve `_format_graph` (#1410). Only a bare identifier with a
    trailing ``(...)`` arg list is stripped — a dotted or spaced span is never
    a call, so junk like ``json.loads(row["menu_json`` keeps its brackets and
    still fails the lookup filter.
    """
    t = (tok or "").strip()
    m = re.match(r"(?i)^\s*def\s+(.+?)\s*$", t)
    if m:
        t = m.group(1).strip()
    m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(.*\)$", t)
    if m2:
        t = m2.group(1)
    return t


def extract_card_tokens(card: dict, cap: int = MAX_AUTO_TOKENS) -> list:
    """Candidate symbol tokens from a card's title+body (bounded to `cap`).

    Backtick spans first (explicit author intent), then bare snake_case
    identifiers (leading underscore allowed: `_format_graph`), then
    route-like paths. Tokens are normalized (call/def spellings → bare name)
    before the lookup filter. Pure; the caller still resolves each
    token against the index (unknown tokens resolve to nothing, not to text).
    """
    text = f"{(card or {}).get('title') or ''}\n{(card or {}).get('body') or ''}"
    seen: list = []

    def _add(tok: str):
        tok = _normalize_token(tok).strip().strip("\"'.,;:()[]")
        if tok and tok not in seen and len(tok) <= 80 and is_lookup_token(tok):
            seen.append(tok)

    for m in _TOKEN_RE.finditer(text):
        _add(m.group(1))
    for m in _SNAKE_RE.finditer(text):
        _add(m.group(0))
    for m in _ROUTE_RE.finditer(text):
        _add(m.group(0))
    return seen[:cap]


def open_ro_index(index_path: str, timeout_ms: int = 2000):
    """Open the codegraph index STRICTLY read-only (mode=ro never writes)."""
    con = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True, timeout=1.0)
    con.execute(f"PRAGMA busy_timeout={int(timeout_ms)}")
    return con


REQUIRED_INDEX_TABLES = ("repos", "symbols")


def probe_index(con) -> str | None:
    """One-shot health probe: None when the index is usable, else a reason.

    `open_ro_index` succeeds on garbage (connect itself never reads), and
    every row query swallows `sqlite3.Error` into `[]` while `repo_registry`
    swallows it into `{}` — so without this probe a 0-byte file, garbage
    bytes, or a schema-less db all render as status ok with '0 symbols'.
    The floor rule forbids that: a zero must never be a fact about a broken
    source. Callers treat any non-None return as
    `index.status = 'unavailable: <reason>'`.
    """
    try:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 50"
        ).fetchall()}
    except sqlite3.Error as exc:
        return f"unreadable index ({exc})"
    missing = [t for t in REQUIRED_INDEX_TABLES if t not in have]
    if missing:
        return f"index missing required tables: {', '.join(missing)}"
    # Every column the row queries actually use (not only repos(name) /
    # symbols(name)): a dropped column (e.g. symbols.signature) makes that
    # query swallow sqlite3.Error into [] and the extract would print an
    # honest-looking floor for a broken source. edges feeds caller_count the
    # same way, so its columns are probed too.
    try:
        con.execute(
            "SELECT name, visibility, indexed_at, path FROM repos LIMIT 1"
        ).fetchall()
        con.execute(
            "SELECT id, repo, name, kind, file_path, start_line,"
            " qualified_name, signature FROM symbols LIMIT 1"
        ).fetchall()
        con.execute(
            "SELECT src_symbol, dst_symbol, edge_type FROM edges LIMIT 1"
        ).fetchall()
    except sqlite3.Error as exc:
        return f"unreadable index ({exc})"
    # Tables present but zero rows is not a pinnable index: without this,
    # an empty index renders status ok with '0 symbols' as if measured.
    try:
        n_repos = con.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
        n_syms = con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    except sqlite3.Error as exc:
        return f"unreadable index ({exc})"
    if not n_repos or not n_syms:
        return (f"empty index: repos has {n_repos} rows, symbols has"
                f" {n_syms} rows (nothing pinned yet)")
    return None


def repo_registry(con) -> dict:
    """{repo: {visibility, indexed_at, path}} — one bounded row query."""
    out = {}
    try:
        rows = con.execute(
            "SELECT name, visibility, indexed_at FROM repos LIMIT 100"
        ).fetchall()
    except sqlite3.Error:
        return {}
    for name, vis, indexed_at in rows:
        out[name] = {"visibility": vis, "indexed_at": indexed_at}
    try:
        paths = {
            n: p
            for n, p in con.execute("SELECT name, path FROM repos LIMIT 100").fetchall()
        }
    except sqlite3.Error:
        paths = {}
    for name, p in paths.items():
        if name in out:
            out[name]["path"] = p
    return out


def load_card_allowlist(path: str | None = DEFAULT_CARD_ALLOWLIST) -> dict:
    """Load the commander-approved card allowlist (`{project_slug: [repo]}`).

    Fail closed: an absent file, unreadable bytes, non-JSON, a non-dict, or
    non-list values all yield `{}` (public only) — never raise, never open.
    """
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, val in data.items():
        if isinstance(key, str) and key.strip() and isinstance(val, list):
            out[key.strip()] = sorted(
                {str(r).strip() for r in val if isinstance(r, str) and str(r).strip()})
    return out


def card_project_slug(card: dict | None, projects: list | None = None) -> str | None:
    """The card's board project slug (pure; None when unresolvable).

    Mirrors the board's real rule for a card artifact: a card row carries
    ``project_id`` (``GET /board/cards/{id}`` has no slug field) and the slug
    is read off the ``GET /board/projects`` rows matched by id — same
    int-or-str equality as ``board._project_owner_orchestrator``. A card dict
    that already carries a ``project``/``project_slug`` string (tests, offline
    use) returns it directly. None means the caller runs public-only.
    """
    if not isinstance(card, dict):
        return None
    for key in ("project", "project_slug", "slug"):
        val = card.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    pid = card.get("project_id")
    if pid is None or not projects:
        return None
    for proj in projects:
        if not isinstance(proj, dict):
            continue
        if proj.get("id") == pid or str(proj.get("id")) == str(pid):
            slug = proj.get("slug")
            if isinstance(slug, str) and slug.strip():
                return slug.strip()
    return None


# Fleet-shaped repo names in card prose: every `swarph-...` whole token in a
# card's title/body is a repo reference (the fleet names repos `swarph-*`),
# never a concept. Non-`swarph-` hyphenated prose (`supply-chain`,
# `first-time`) and peer names (`lab-ovh`, `gemini-researcher`) are never
# repo claims and are ignored — only what the card actually names is
# reported, nothing is invented.
_SWARPH_REPO_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(swarph-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)"
    r"(?![A-Za-z0-9_.-])", re.IGNORECASE)


def card_named_missing_repos(card: dict | None, registry: dict) -> list:
    """Fleet-shaped repo names the card names that the index lacks (pure).

    Scans the card's title+body the way `extract_card_tokens` scans it for
    targets — a pure function of the card dict — for whole `swarph-...`
    tokens absent from `registry` (card #1393 names `swarph-codegraph-index`,
    which has no repos row). Returns the sorted distinct missing names.
    """
    if not isinstance(card, dict) or not registry:
        return []
    text = f"{card.get('title') or ''}\n{card.get('body') or ''}"
    lowered = {str(r).lower() for r in registry}
    seen = {m.group(1) for m in _SWARPH_REPO_RE.finditer(text)}
    return sorted(n for n in seen if n.lower() not in lowered)


def split_visible(registry: dict, project_slug: str | None = None,
                  card_allowlist: dict | None = None) -> tuple:
    """Public repos + the card project's allowlisted repos are visible.

    Composition mirrors codegraph._visible_repos (public always; private needs
    an allowlist entry) with the card's board project slug in the caller's
    seat: ``card_allowlist[project_slug]`` plays the role of
    ``allowlist[repo]`` membership. Anything else — including
    unknown-visibility repos — is WITHHELD (fail closed). Malformed/absent
    file (``{}``), or a project with no entry, means public only.
    """
    allowed = set()
    if isinstance(card_allowlist, dict) and project_slug:
        for repo in card_allowlist.get(project_slug) or []:
            if isinstance(repo, str) and repo:
                allowed.add(repo)
    visible = sorted(n for n, m in registry.items()
                     if m.get("visibility") == "public" or n in allowed)
    withheld = sorted(n for n in registry if n not in visible)
    return visible, withheld


def find_symbols(con, token: str, visible: list, limit: int = ROW_LIMIT) -> list:
    """Exact-name symbol rows in visible repos (bounded LIMIT).

    `kind = 'import'` rows are never definitions: a token like `swarph_cli`
    otherwise resolves to dozens of `from swarph_cli import ...` sites
    instead of the symbol itself.
    """
    if not visible:
        return []
    qmarks = ",".join("?" * len(visible))
    try:
        rows = con.execute(
            "SELECT id, repo, name, kind, file_path, start_line, qualified_name,"
            " signature FROM symbols WHERE name = ? AND kind != 'import'"
            f" AND repo IN ({qmarks}) LIMIT ?",
            [token, *visible, limit],
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "id": sid, "repo": repo, "name": name, "kind": kind,
            "file_path": fp, "start_line": line,
            "qualified_name": qual or "", "signature": sig or "",
        }
        for sid, repo, name, kind, fp, line, qual, sig in rows
    ]


def _is_test_path(file_path: str) -> bool:
    """True when `file_path` lives under a tests/ dir (test code, not deployed).

    A same-named row under tests/ is a test helper: the renderer must word
    that line as test code instead of implying a deployed copy that differs.
    """
    fp = (file_path or "").replace("\\", "/")
    return fp == "tests" or fp.startswith("tests/") or "/tests/" in fp


def withheld_symbol_repos(con, token: str, withheld: list,
                          limit: int = ROW_LIMIT) -> dict:
    """Exact-name symbol match in WITHHELD repos — repo names only, no detail.

    Fail-closed companion to `find_symbols`: when a target token matches a
    symbol in a withheld (private/unknown-visibility) repo, the extract names
    ONLY the repo (never file, line, signature, or count). Repo names are not
    a new leak — the WITHHELD list already prints them with the reason.

    Returns {repo: test_only}: `test_only` is True when EVERY matched row
    for that repo lives under tests/ (a test helper, not deployed code), so
    the renderer words the line as test code. The query reads file_path only
    to make that distinction — it is never printed.
    """
    if not withheld or not token:
        return {}
    qmarks = ",".join("?" * len(withheld))
    try:
        rows = con.execute(
            "SELECT DISTINCT repo, file_path FROM symbols WHERE name = ?"
            " AND kind != 'import'"
            f" AND repo IN ({qmarks}) LIMIT ?",
            [token, *withheld, limit],
        ).fetchall()
    except sqlite3.Error:
        return {}
    by_repo: dict = {}
    for repo, fp in rows:
        by_repo.setdefault(repo, []).append(fp or "")
    return {repo: (bool(fps) and all(_is_test_path(f) for f in fps))
            for repo, fps in sorted(by_repo.items())}


def _escape_like(s: str) -> str:
    """Escape LIKE metacharacters (`%`, `_`, the escape char) in a literal.

    `--path "%"` must match a literal `%`, not every row: the LIKE pattern
    below would otherwise read a caller-supplied `%`/`_` as a wildcard (and
    a trailing backslash would eat the closing escape). Both queries declare
    an ESCAPE clause, so prefixing those three characters keeps the lookup
    literal.
    """
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def find_symbols_by_path(con, path_arg: str, visible: list,
                         limit: int = ROW_LIMIT) -> list:
    """Symbols whose file_path matches `path` or `path:line` (bounded LIMIT).

    `--path` is a file selector, not a second spelling of `--symbol`: the
    lookup is against `file_path` (exact or `/`-suffixed), never the symbol
    name. A trailing `:line` (digits) narrows to that start line. LIKE
    metacharacters in the argument are escaped, so `--path %` matches a
    literal `%` (no arbitrary rows) while a real path still resolves.
    """
    if not visible or not path_arg:
        return []
    path, _, line = (path_arg or "").rpartition(":")
    if not path or not line.isdigit():
        path, line = path_arg, None
    qmarks = ",".join("?" * len(visible))
    like = "%/" + _escape_like(path.lstrip("/"))
    try:
        if line is not None:
            rows = con.execute(
                "SELECT id, repo, name, kind, file_path, start_line, qualified_name,"
                " signature FROM symbols WHERE (file_path = ? OR file_path LIKE ? ESCAPE '\\')"
                " AND kind != 'import'"
                f" AND repo IN ({qmarks}) AND start_line = ? LIMIT ?",
                [path, like, *visible, int(line), limit],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT id, repo, name, kind, file_path, start_line, qualified_name,"
                " signature FROM symbols WHERE (file_path = ? OR file_path LIKE ? ESCAPE '\\')"
                " AND kind != 'import'"
                f" AND repo IN ({qmarks}) LIMIT ?",
                [path, like, *visible, limit],
            ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "id": sid, "repo": repo, "name": name, "kind": kind,
            "file_path": fp, "start_line": ln,
            "qualified_name": qual or "", "signature": sig or "",
        }
        for sid, repo, name, kind, fp, ln, qual, sig in rows
    ]


def _path_withholds(file_path: str | None, withheld) -> bool:
    """True when a WITHHELD repo name appears as a PATH COMPONENT (#1410).

    Separator-insensitive equality: `mesh-gateway`, `mesh_gateway`, and
    `mesh.gateway` are the same component. Prefixes do not count — a
    component `gateway` is not `mesh-gateway`.
    """
    if not file_path or not withheld:
        return False
    comps = [p for p in re.split(r"[\\/]+", file_path) if p]
    for repo in withheld or []:
        want = _name_parts(repo)
        if not want:
            continue
        for comp in comps:
            if _name_parts(comp) == want:
                return True
    return False


def find_routes(con, token: str, visible: list, limit: int = ROW_LIMIT) -> list:
    """Route-kind rows matching the ROUTE itself (bounded LIMIT).

    Matches `token` against the route's own `name` (e.g. `POST
    /api/chat-sync`) — never `qualified_name`, which embeds the file path
    (`src/swarph_cli/gateway/server.py::POST:...`), so a token like
    `swarph_cli` matched all 76 route rows on card #792. A trailing `/`
    from mined card text (`POST /api/chat-sync/`) is stripped; a token
    that is then too short to be a route (`/`) matches nothing.
    """
    if not visible:
        return []
    tok = (token or "").rstrip("/")
    if len(tok) < 2:
        return []
    qmarks = ",".join("?" * len(visible))
    like = f"%{tok}%"
    try:
        rows = con.execute(
            "SELECT repo, name, file_path, start_line, signature FROM symbols"
            " WHERE kind = 'route' AND name LIKE ?"
            f" AND repo IN ({qmarks}) LIMIT ?",
            [like, *visible, limit],
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"repo": r, "name": n, "file_path": fp, "start_line": ln, "signature": s or ""}
        for r, n, fp, ln, s in rows
    ]


def caller_count(con, symbol_id: int, visible: list) -> int | None:
    """COUNT(*) of 'calls' edges from visible repos (aggregate, no rows)."""
    if not visible:
        return 0
    qmarks = ",".join("?" * len(visible))
    try:
        return con.execute(
            "SELECT COUNT(*) FROM edges e JOIN symbols src ON src.id = e.src_symbol"
            f" WHERE e.dst_symbol = ? AND e.edge_type = 'calls'"
            f" AND src.repo IN ({qmarks})",
            (symbol_id, *visible),
        ).fetchone()[0]
    except sqlite3.Error:
        return None


def _name_parts(s: str) -> list:
    """A repo/module/path name split on every separator layer.

    Repo, module, and dotted-path spellings of one name share parts:
    `mesh-gateway`, `mesh_gateway`, and the head of
    `mesh_gateway.private_helper` are all `[mesh, gateway]`.

    Still feeds `_path_withholds` (the symbol path-component guard). The
    fed-report word machinery that also used it (`_names_whole_token`,
    `_line_withholds`, `_drop_withheld_lines`) is gone: the federation
    half keeps lines by SHAPE now, so there is nothing left to filter.
    """
    return [p for p in re.split(r"[-_./]+", (s or "").lower()) if p]


# Federation report lines are kept by LINE SHAPE, not by words (card #1410):
# a line is kept iff it is a Layer header (`Layer N:`). Dependency arrows
# (`──depends on──▶`), blast-radius lines, import-site lines (`↳ imports
# ... e.g. ...`), and every other report line never reach any output — the
# old word grep passed those through whenever they named a hit repo.
_LAYER_RE = re.compile(r"^\s*Layer \d+:\s*$")


def read_fed_report(path: str,
                    line_cap: int = FED_REPORT_LINE_CAP) -> dict:
    """Shape-only read of the nightly federation report (capped).

    Returns the header stamp (`# generated ...`, the build time freshness
    compares — never the file mtime) plus the `Layer N:` header lines. A
    missing/unreadable file, or a file with no header stamp (0-byte,
    garbage), reads `unavailable`. No repo names are matched and none are
    printed, so there is nothing to filter here.
    """
    lines: list = []
    try:
        # encoding pinned: the report carries box-drawing/arrow glyphs and the
        # Windows runner's locale codec (cp1252) cannot round-trip them.
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(line_cap + 1):
                try:
                    lines.append(next(fh))
                except StopIteration:
                    break
    except OSError as exc:
        return {"status": f"unavailable: {exc.strerror or exc}"}
    cut = len(lines) > line_cap
    lines = lines[:line_cap]
    generated = next(
        (ln for ln in lines[:5] if ln.startswith("# generated")), ""
    ).strip("# \n")
    if not generated:
        return {"status": "unavailable: no header stamp"}
    layers = [ln.strip() for ln in lines if _LAYER_RE.match(ln)]
    return {
        "status": "ok", "generated": generated,
        "layers": layers, "cut": cut, "scanned": min(len(lines), line_cap),
    }


def _stamp_time(generated: str) -> str | None:
    """The build instant: first timestamp token of the header stamp.

    `read_fed_report` strips `# ` off `# generated 2026-11-03T04:45:01Z by
    ...`, so the stored stamp starts with the word `generated`; skip it and
    take the first timestamp token. Unparseable stamps yield None
    (freshness renders unavailable, never STALE-by-default). The file mtime
    is never used (card #1410).
    """
    text = (generated or "").strip()
    if text[:9].lower() == "generated":
        text = text[9:].strip()
    tok = (text.split() or [""])[0]
    return tok if _parse_instant(tok) is not None else None


def read_structurizr(path: str, repos: list) -> dict:
    """Deploy units for `repos` (minimal bounded JSON scan).

    #1410: relationships ("rels") are never read and never printed — no rels
    of any kind appear in the output — so there is nothing to filter here.
    What remains is the smallest part that still prints deploy units: the
    one-system/many-containers scan matching `repos` by container name and
    reading each match's `group` (the deploy host). The full-container-name
    list is gone — it only fed the removed fed-report unknown-name rule.
    `matched`/`deploy_units` name only visible repos / host groups, so they
    need no filtering.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            ws = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"status": f"unavailable: {exc}"}
    model = ws.get("model", {}) if isinstance(ws, dict) else {}
    systems = model.get("softwareSystems", []) or []
    # Repos live at CONTAINER level (one system, e.g. "Swarph Federation",
    # holding a container per repo with `group` = deploy host).
    by_name = {}
    for s in systems:
        if not isinstance(s, dict):
            continue
        for c in s.get("containers", []) or []:
            if not isinstance(c, dict):
                continue
            by_name[str(c.get("name", "")).lower()] = c
    matched, deploy = [], []
    for repo in repos:
        c = by_name.get(repo.lower())
        if not isinstance(c, dict):
            continue
        matched.append(repo)
        group = c.get("group")
        if group and str(group) not in deploy:
            deploy.append(str(group))
    return {
        "status": "ok",
        "matched": matched[:ROW_LIMIT],
        "deploy_units": deploy[:ROW_LIMIT],
    }


def last_merged_at(repo_path: str | None) -> str | None:
    """Last repo.merged proxy: origin/main HEAD commit time (bounded git call).

    The true repo.merged event log is not readable from here, so the merge
    time is proxied by the merge commit itself and LABELLED as a proxy in the
    rendered extract. Any git failure -> None (rendered as unavailable).
    """
    if not repo_path:
        return None
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "origin/main"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        ts = (out.stdout or "").strip()
        return ts or None
    except Exception:
        return None


def indexed_sha(repo_path: str | None) -> str | None:
    """Pinned sha from the repo's .codegraph/indexed_origin_main (card #1393)."""
    if not repo_path:
        return None
    try:
        with open(os.path.join(repo_path, ".codegraph", "indexed_origin_main"),
                  encoding="utf-8") as fh:
            sha = fh.read().strip().split()[0]
        return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None
    except (OSError, IndexError):
        return None


WITHHOLD_REASON = ("not public and not listed for this card's project in "
                   "card-allowlist.json; card artifacts show public repos "
                   "plus the card project allowlist only")


def redact_for_json(ex: dict) -> dict:
    """A `--json`-safe copy of the extract: WITHHELD registry entries keep
    only `{name, visibility, reason}` — `path` and `indexed_at` are dropped.

    The withholding rule exists to keep private-repo facts off card
    artifacts; dumping the raw registry (absolute paths + index times) into
    machine-readable `--json` re-opens exactly that leak on the local box.
    Visible repos keep everything. The input dict is never mutated, so the
    text renderer keeps using the full extract.
    """
    out = dict(ex)
    withheld = set(ex.get("withheld") or [])
    out["registry"] = {
        name: ({"name": name,
                "visibility": (meta or {}).get("visibility"),
                "reason": WITHHOLD_REASON}
               if name in withheld else meta)
        for name, meta in (ex.get("registry") or {}).items()
    }
    return out


def fed_docs_state(fed_docs_db: str) -> str:
    """The TRUE fed-docs.db state, independent of every other source.

    The text renderer prints this verbatim, so an unavailable index must
    still carry it: the old early returns dropped the key and the renderer
    printed an empty value with the 'present on this box' wording.
    """
    if not os.path.exists(fed_docs_db or ""):
        return "unavailable: fed-docs.db not present on this box"
    return (f"present ({fed_docs_db}) — citation lookup not linked yet")


def build_extract(*, tokens: list, index_path: str = DEFAULT_INDEX,
                  fed_report: str = DEFAULT_FED_REPORT,
                  structurizr_ws: str = DEFAULT_STRUCTURIZR_WS,
                  fed_docs_db: str = DEFAULT_FED_DOCS_DB,
                  card: dict | None = None,
                  paths: list | None = None,
                  merged_lookup=None,
                  project_slug: str | None = None,
                  card_allowlist: dict | None = None,
                  card_allowlist_path: str | None = DEFAULT_CARD_ALLOWLIST) -> dict:
    """Assemble the extract dict. Never raises on bad sources (unavailable).

    Visibility: the card's board project slug (explicit `project_slug`, else
    derived from `card` via `card_project_slug`) is looked up in the card
    allowlist (explicit `card_allowlist`, else loaded from
    `card_allowlist_path`, fail closed); those repos join the public set.
    """
    merged_lookup = merged_lookup or last_merged_at
    tokens = [_normalize_token(t) for t in (tokens or [])]
    tokens = [t for t in tokens if t and is_lookup_token(t)]
    fed_docs = fed_docs_state(fed_docs_db)
    if project_slug is None and card:
        project_slug = card_project_slug(card)
    if card_allowlist is None:
        card_allowlist = load_card_allowlist(card_allowlist_path)
    try:
        con = open_ro_index(index_path)
    except sqlite3.Error as exc:
        return {"index": {"status": f"unavailable: {exc}"}, "tokens": tokens,
                "fed_docs": fed_docs}
    try:
        bad = probe_index(con)
        if bad is not None:
            return {"index": {"status": f"unavailable: {bad}"},
                    "tokens": tokens, "fed_docs": fed_docs}
        registry = repo_registry(con)
        visible, withheld = split_visible(registry, project_slug, card_allowlist)
        # Allowlist-driven (a card-project repo with no indexed copy) PLUS
        # card-named (a fleet-shaped `swarph-...` name the card's own text
        # names that the registry lacks — card #1393's swarph-codegraph-index
        # has no repos row). Only names the card actually contains.
        missing_repos = sorted(
            {r for r in ((card_allowlist or {}).get(project_slug) or [])
             if isinstance(r, str) and r and r not in registry}
            | set(card_named_missing_repos(card, registry)))
        syms, routes = [], []
        withheld_hits: dict = {}

        def _add_sym(s: dict):
            if _path_withholds(s.get("file_path"), withheld):
                return
            if s["id"] not in {x["id"] for x in syms}:
                s["callers"] = caller_count(con, s["id"], visible)
                syms.append(s)

        for tok in tokens[:MAX_AUTO_TOKENS]:
            for s in find_symbols(con, tok, visible):
                _add_sym(s)
            for repo, test_only in withheld_symbol_repos(
                    con, tok, withheld).items():
                withheld_hits.setdefault(tok, {})[repo] = test_only
            routes.extend(r for r in find_routes(con, tok, visible)
                          if r["name"] not in {x["name"] for x in routes}
                          and not _path_withholds(r.get("file_path"), withheld))
        for parg in (paths or [])[:MAX_AUTO_TOKENS]:
            for s in find_symbols_by_path(con, parg, visible):
                _add_sym(s)
        repos_hit = sorted({s["repo"] for s in syms})
    finally:
        try:
            con.close()
        except Exception:
            pass
    # Shape-only federation half: the report contributes its header stamp
    # (the build time) plus the `Layer N:` lines — never per-repo hit lines,
    # so neither the hit repos nor the withheld set are inputs anymore.
    struct = read_structurizr(structurizr_ws, repos_hit)
    fed = read_fed_report(fed_report)
    pins = {}
    for repo in repos_hit:
        meta = registry.get(repo, {})
        pins[repo] = {
            "sha": indexed_sha(meta.get("path")),
            "indexed_at": meta.get("indexed_at"),
            "merged_at": merged_lookup(meta.get("path")),
        }
    fed_build = (_stamp_time(fed.get("generated"))
                 if fed.get("status") == "ok" else None)
    return {
        "index": {"status": "ok", "path": index_path},
        "tokens": tokens, "paths": list(paths or []),
        "symbols": syms, "routes": routes,
        "repos_hit": repos_hit, "withheld_hits": withheld_hits,
        "visible": visible, "withheld": withheld, "registry": registry,
        "project_slug": project_slug, "missing_repos": missing_repos,
        "federation_report": fed, "structurizr": struct,
        "fed_build": fed_build,
        # (d) absence is claimed ONLY when the file truly is absent; a
        # present-but-empty (or otherwise unreadable) db says so explicitly.
        "fed_docs": fed_docs,
        "pins": pins,
        "card": ({"id": card.get("id"), "title": card.get("title")}
                 if card else None),
    }


def _parse_instant(s) -> "datetime.datetime | None":
    """Parse a timestamp to an aware UTC instant; None when unusable.

    Federation build times are UTC (`...Z` / `+00:00`); repo.merged proxies
    come from `git %cI` in the fleet checkout's local offset (e.g.
    `...+01:00`). Comparing the raw strings mis-orders across offsets, so
    both sides are parsed and compared as instants. Naive inputs are read as
    UTC; garbage returns None (rendered unavailable, never STALE-by-default).
    """
    import datetime as _dt
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if t[-1:] in ("Z", "z"):
        t = t[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def is_stale_fed_build(fed_build, merged_at) -> bool | None:
    """True when the federation build predates the repo merge (instants).

    None when either side is missing/unparseable — the caller renders
    freshness unavailable rather than guessing.
    """
    fb = _parse_instant(fed_build)
    mg = _parse_instant(merged_at)
    if fb is None or mg is None:
        return None
    return fb < mg


def render(ex: dict, max_lines: int = DEFAULT_MAX_LINES) -> str:
    """Render the extract text with floor/unavailable wording + size cap.

    The cap reserves the tail (WITHHELD line, fed-docs line, freshness
    section, cut line): only the middle (symbols/routes/pins plus
    the federation build/hits) is truncated, so the 60-line budget
    can never cut freshness away again (card #792 showed 0 STALE lines).
    Three plain outcome lines (#1410), each distinct: `index unavailable`
    (the index cannot be read/probed), `repo X is not in the index` (an
    allowlisted or card-named repo with no indexed copy), `no symbol
    resolved` (lookups found nothing). The federation half prints only the
    report header stamp (build line) and the `Layer N:` lines — or the
    report's own unavailable line. No covering-tests section and no rels,
    depends-on, blast-radius, arrow, or import lines of any kind.
    """
    cut_notes: list = []
    head = ["# dev extract (card #1410, #559 phase 2) — pinned, small, dry-run-safe"]
    if ex.get("card"):
        head.append(f"subject: card #{ex['card'].get('id')}: "
                    f"{(ex['card'].get('title') or '')[:100]}")
    _shown_targets = list(ex.get("tokens", [])) + list(ex.get("paths", []))
    head.append(f"targets: {', '.join(_shown_targets) or '(none resolved)'}")
    head.append("")
    idx = ex.get("index", {})
    if idx.get("status") != "ok":
        _detail = (idx.get("status") or "unavailable").partition(
            "unavailable:")[2].strip() or idx.get("status", "unavailable")
        head.append(f"index unavailable: {_detail}")
    else:
        head.append("## codegraph half (index.db, READ-ONLY)")
        # Card #1404: the index can miss WHOLE files (mesh-gateway server.py,
        # 1.12 MiB, exceeds the 1 MiB file limit, so the deployed chat_sync
        # has 0 rows) — a visible match is the indexed copy, not proof the
        # deployed code is here. One line always (index ok); repo names and
        # the card number only, never withheld file paths or symbol names.
        _cov_names = ", ".join(ex.get("withheld", []) or [])
        if _cov_names:
            head.append(
                f"index coverage is partial: withheld {_cov_names} can have "
                "0 rows for a whole main module (card #1404); a visible match "
                "is the indexed copy, not evidence the deployed code is here")
        else:
            head.append(
                "index coverage is partial: a withheld repo's main module can "
                "have 0 rows (card #1404); a visible match is the indexed "
                "copy, not evidence the deployed code is here")
        for repo in ex.get("missing_repos", []):
            head.append(f"repo {repo} is not in the index")
        for s in ex.get("symbols", [])[:ROW_LIMIT]:
            loc = f"{s['file_path']}:{s['start_line']}"
            n = s.get("callers")
            callers = (FLOOR_CALLERS if n == 0 else
                       f"{n} callers" if isinstance(n, int) else
                       "callers unavailable: count query failed")
            head.append(f"- {s['name']} [{s['kind']}] {s['repo']} {loc} — {callers}")
            if s.get("signature"):
                head.append(f"  sig: {s['signature'][:120]}")
            # Card #1404: swarph-cli also carries a gateway copy — say so with
            # the hit, since the deployed gateway is a withheld repo. Only
            # for this path shape (swarph-cli rows under swarph_cli/gateway/),
            # and only while mesh-gateway is actually withheld: on a card
            # whose project allowlists it, the deployed copy prints in full
            # beside it and the warning would be false (#1410).
            if (s.get("repo") == "swarph-cli"
                    and "swarph_cli/gateway/" in (s.get("file_path") or "")
                    and "mesh-gateway" in (ex.get("withheld") or [])):
                head.append("  swarph-cli gateway copy; "
                             "the deployed gateway is a withheld repo")
        if not ex.get("symbols"):
            head.append("no symbol resolved")
        if len(ex.get("symbols", [])) > ROW_LIMIT:
            cut_notes.append(f"symbols cut after {ROW_LIMIT}")
        for tok in ex.get("tokens", []):
            _hits = (ex.get("withheld_hits") or {}).get(tok, {})
            for repo, test_only in sorted(_hits.items()):
                if test_only:
                    head.append(
                        f"- {tok}: also defined in withheld {repo} "
                        "(test code); the deployed copy may differ")
                else:
                    head.append(f"- {tok}: also defined in withheld {repo}; "
                                 "the deployed copy may differ")
        head.append("")
        routes = ex.get("routes", [])
        if routes:
            head.append("routes + params:")
            for r in routes[:ROW_LIMIT]:
                head.append(f"- {r['name']} {r['repo']} "
                             f"{r['file_path']}:{r['start_line']}"
                             + (f" params: {r['signature'][:100]}" if r.get("signature") else ""))
        else:
            head.append(f"routes: {FLOOR_ROUTES}")
        head.append("")
        # #1410: no covering-tests section — name-match is not coverage, and
        # no real data backs it. Nothing is printed here at all.
        head.append("pins (per repo: indexed sha + index time):")
        for repo, p in ex.get("pins", {}).items():
            sha = p.get("sha") or "sha unavailable: .codegraph/indexed_origin_main missing"
            head.append(f"- {repo}: {sha} @ {p.get('indexed_at') or 'index time unavailable'}")
    head.append("")
    head.append("## federation half (READ-ONLY)")
    fed = ex.get("federation_report", {})
    st = ex.get("structurizr", {})
    # No synthetic per-outcome line here: the outcomes already have their
    # own plain lines (`repo X is not in the index`, `no symbol resolved`,
    # `index unavailable`). The federation half prints the report stamp
    # plus the `Layer N:` lines, or the report's own unavailable line.
    if fed.get("status") != "ok":
        head.append(f"federation-report: {fed.get('status', 'unavailable')}")
    else:
        head.append(f"build: {ex.get('fed_build') or 'unavailable: no header stamp'} "
                    "(federation-report.txt header stamp = nightly 04:45 build)")
        for layer in fed.get("layers", []):
            head.append(layer)
        if not fed.get("layers"):
            head.append("federation layers: unavailable "
                        "(floor: report has no Layer lines)")
        if fed.get("cut"):
            cut_notes.append(f"fed-report scanned first {FED_REPORT_LINE_CAP} lines")
    if st.get("status") != "ok":
        head.append(f"structurizr: {st.get('status', 'unavailable')}")
    else:
        # #1410: no rels of any kind in output — downstream is never
        # printed (neither entries nor floor). Deploy units stay.
        if st.get("deploy_units"):
            head.append(f"deploy units: {', '.join(st['deploy_units'])}")
        else:
            head.append("deploy units: unavailable (floor: no group-tagged container matched)")
    # Reserved tail: WITHHELD (ONE names-list line), the fed-docs state,
    # and the freshness section. The cap truncates `head` only, so these
    # are never cut away.
    tail = [""]
    withheld = ex.get("withheld", [])
    if withheld:
        tail.append(f"WITHHELD: {', '.join(withheld)} ({WITHHOLD_REASON} — "
                    "same public+allowlist composition as "
                    "codegraph._visible_repos, caller replaced by the card's "
                    "project slug; full list in --json)")
    _fed_docs = ex.get("fed_docs") or \
        "unavailable: fed-docs state not recorded"
    if isinstance(_fed_docs, str) and _fed_docs.startswith("unavailable"):
        tail.append(f"fed-docs.db: {_fed_docs} "
                    "(cards/docs/memories citation index — absent on this box, "
                    "so citations beyond the subject card are not linked yet)")
    else:
        tail.append(f"fed-docs.db: {_fed_docs} "
                    "(cards/docs/memories citation index — present on this box, "
                    "but citations beyond the subject card are not linked yet)")
    tail.append("")
    tail.append("freshness (STALE when fed build < last repo.merged):")
    for repo, p in ex.get("pins", {}).items():
        merged = p.get("merged_at")
        fb = ex.get("fed_build")
        if not fb:
            tail.append(f"- {repo}: freshness unavailable (no federation build time)")
        elif not merged:
            tail.append(f"- {repo}: freshness unavailable "
                        "(last repo.merged unknown for this repo)")
        else:
            stale = is_stale_fed_build(fb, merged)
            if stale is None:
                tail.append(f"- {repo}: freshness unavailable "
                            "(unparseable federation-build or repo.merged time)")
            elif stale:
                tail.append(f"- {repo}: STALE (federation build {fb} older than "
                            f"last repo.merged {merged}; merge-time is an "
                            "origin/main-HEAD proxy, see module docstring)")
            else:
                tail.append(f"- {repo}: fresh (federation build {fb} >= "
                            f"last repo.merged {merged})")
    if len(head) + len(tail) + (1 if cut_notes else 0) > max_lines:
        cut_notes.append(f"output capped at {max_lines} lines "
                         "(render budget; freshness + withheld kept)")
        room = max(0, max_lines - len(tail) - 1)
        lines = head[:room] + [f"cut: {'; '.join(cut_notes)}"] + tail
    else:
        lines = head + tail
        if cut_notes:
            lines += ["", f"cut: {'; '.join(cut_notes)}"]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="swarph board cards dev-extract",
        description="Build a pinned dev extract joining the codegraph and "
                    "federation halves for a card. Prints only with "
                    "--print-only (or when no --card is given); with --card "
                    "and without --print-only it posts the extract onto the "
                    "card via `cards say`. Nothing is ever deployed.")
    ap.add_argument("--card", type=int, default=None,
                    help="card id: token source + post target (without "
                         "--print-only the extract is POSTED to this card)")
    ap.add_argument("--symbol", action="append", default=[],
                    help="explicit target symbol name (repeatable)")
    ap.add_argument("--path", action="append", default=[],
                    help="explicit target file path, optionally with :line "
                         "(repeatable; matches symbol file_path, not names)")
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--fed-report", default=DEFAULT_FED_REPORT)
    ap.add_argument("--structurizr-ws", default=DEFAULT_STRUCTURIZR_WS)
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    ap.add_argument("--print-only", action="store_true",
                    help="dry-run: print the extract, post nothing")
    ap.add_argument("--json", action="store_true")
    return ap
