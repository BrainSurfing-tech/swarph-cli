"""``swarph board cards dev-extract`` — a small pinned "dev extract" for spec/plan
cards (card #1410, #559 phase 2; re-spec of #1396).

The extract JOINS two halves, each with its own as-of pin:

* Codegraph half (READ-ONLY index.db): symbols with file:line, caller
  counts, routes + params; per repo the ``indexed_origin_main`` sha
  (#1393) plus ``repos.indexed_at``. No covering-tests section.
* Federation half (READ-ONLY federation-report.txt + structurizr workspace):
  the report header stamp plus ``Layer N:`` lines only, and deploy units
  for visible repos — no rels/depends-on/blast-radius/arrow/import lines.
  STALE when older than the last repo.merged for a repo.

Posting uses the board path when ``--card`` is given WITHOUT
``--print-only``; otherwise prints and posts nothing. Nothing is deployed.

Zeros print as FLOORs, unreadable sources as ``unavailable: <reason>``.

Privacy (#1410): public repos are always visible; the card project's
``card-allowlist.json`` repos join them ONLY when ``entitled`` is True —
the runner's OWN direct ``execute`` grant (``GET /peers/{peer}/grants``,
exact-grantee direct row). Default False is public only (fail closed);
print mode names withheld repos, post-safe mode is count-only — posted
bodies carry public rows only and name no withheld or missing repo.

Known limit, STATED not solved (#20/#1369): same-box cells share a uid
and can open index.db directly, so this gate binds the CLI's composed view
(print vs post-safe), not the file. Every row-returning SQL statement
carries an explicit LIMIT (pinned by ``test_every_row_query_is_bounded``);
caller counts are ``COUNT(*)`` aggregates; the renderer enforces
``--max-lines`` and states what was cut.
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

# Junk-target stoplist (#788: `?`/`none` can never be symbols).
_JUNK_WORDS = frozenset({"none", "null", "nil", "n/a", "na", "undefined"})
_ROUTE_TOKEN_RE = re.compile(r"(?:POST|GET|PUT|PATCH|DELETE)\s+/\S+")
_SHELL_META_RE = re.compile(r"[=|><;$&*`(){}\[\]\"']")


def is_lookup_token(tok: str) -> bool:
    """True when `tok` is worth an index lookup; else junk (#792/#788)."""
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
    """Call-site / def-site spelling → bare symbol name (pure, #1410)."""
    t = (tok or "").strip()
    m = re.match(r"(?i)^\s*def\s+(.+?)\s*$", t)
    if m:
        t = m.group(1).strip()
    m2 = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(.*\)$", t)
    if m2:
        t = m2.group(1)
    return t


def extract_card_tokens(card: dict, cap: int = MAX_AUTO_TOKENS) -> list:
    """Candidate symbol tokens from a card's title+body (bounded, pure)."""
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
    """Health probe: None when the index is usable, else a reason."""
    try:
        have = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 50"
        ).fetchall()}
    except sqlite3.Error as exc:
        return f"unreadable index ({exc})"
    missing = [t for t in REQUIRED_INDEX_TABLES if t not in have]
    if missing:
        return f"index missing required tables: {', '.join(missing)}"
    # Every column the row queries use (a dropped one would swallow into []
    # and print an honest-looking floor for a broken source).
    try:
        con.execute("SELECT name, visibility, indexed_at, path FROM repos LIMIT 1").fetchall()
        con.execute("SELECT id, repo, name, kind, file_path, start_line,"
                    " qualified_name, signature FROM symbols LIMIT 1").fetchall()
        con.execute("SELECT src_symbol, dst_symbol, edge_type FROM edges LIMIT 1").fetchall()
    except sqlite3.Error as exc:
        return f"unreadable index ({exc})"
    # Tables present but zero rows is not a pinnable index.
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
        paths = dict(con.execute("SELECT name, path FROM repos LIMIT 100").fetchall())
    except sqlite3.Error:
        paths = {}
    for name, p in paths.items():
        if name in out:
            out[name]["path"] = p
    return out


def load_card_allowlist(path: str | None = DEFAULT_CARD_ALLOWLIST) -> dict:
    """Card allowlist (`{project_slug: [repo]}`); fail closed to `{}`."""
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key.strip(): sorted(
                {str(r).strip() for r in val
                 if isinstance(r, str) and str(r).strip()})
            for key, val in data.items()
            if isinstance(key, str) and key.strip() and isinstance(val, list)}


def card_project_slug(card: dict | None, projects: list | None = None) -> str | None:
    """The card's board project slug via ``project_id`` → projects rows."""
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
        if (isinstance(proj, dict)
                and (proj.get("id") == pid or str(proj.get("id")) == str(pid))):
            slug = proj.get("slug")
            if isinstance(slug, str) and slug.strip():
                return slug.strip()
    return None


# Whole `swarph-...` tokens in card prose are repo references; anything
# else hyphenated (prose, peer names) is never a repo claim.
_SWARPH_REPO_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(swarph-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)"
    r"(?![A-Za-z0-9_.-])", re.IGNORECASE)


def card_named_missing_repos(card: dict | None, registry: dict) -> list:
    """Card-named `swarph-...` repos absent from the registry (pure)."""
    if not isinstance(card, dict) or not registry:
        return []
    text = f"{card.get('title') or ''}\n{card.get('body') or ''}"
    lowered = {str(r).lower() for r in registry}
    seen = {m.group(1) for m in _SWARPH_REPO_RE.finditer(text)}
    return sorted(n for n in seen if n.lower() not in lowered)


def split_visible(registry: dict, project_slug: str | None = None,
                  card_allowlist: dict | None = None,
                  entitled: bool = False) -> tuple:
    """Public repos + the card project's allowlisted repos are visible."""
    if entitled and isinstance(card_allowlist, dict) and project_slug:
        allowed = {r for r in card_allowlist.get(project_slug) or ()
                   if isinstance(r, str) and r}
    else:
        allowed = set()
    visible = sorted(n for n, m in registry.items()
                     if m.get("visibility") == "public" or n in allowed)
    withheld = sorted(n for n in registry if n not in visible)
    return visible, withheld


def find_symbols(con, token: str, visible: list, limit: int = ROW_LIMIT) -> list:
    """Exact-name symbol rows in visible repos (`import` rows excluded)."""
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
    return [{"id": sid, "repo": repo, "name": name, "kind": kind, "file_path": fp,
             "start_line": ln, "qualified_name": qual or "", "signature": sig or ""}
            for sid, repo, name, kind, fp, ln, qual, sig in rows]


def _is_test_path(file_path: str) -> bool:
    """True when `file_path` lives under a tests/ dir."""
    fp = (file_path or "").replace("\\", "/")
    return fp == "tests" or fp.startswith("tests/") or "/tests/" in fp


def withheld_symbol_repos(con, token: str, withheld: list,
                          limit: int = ROW_LIMIT) -> dict:
    """Exact-name symbol match in WITHHELD repos — repo names only."""
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
    """Escape LIKE metacharacters so `--path %` matches a literal `%`."""
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_"))


def find_symbols_by_path(con, path_arg: str, visible: list,
                         limit: int = ROW_LIMIT) -> list:
    """Symbols whose file_path matches `path` or `path:line`."""
    if not visible or not path_arg:
        return []
    path, _, line = (path_arg or "").rpartition(":")
    if not path or not line.isdigit():
        path, line = path_arg, None
    qmarks = ",".join("?" * len(visible))
    like = "%/" + _escape_like(path.lstrip("/"))
    extra = " AND start_line = ?" if line is not None else ""
    args = ([path, like, *visible]
            + ([int(line)] if line is not None else []) + [limit])
    try:
        rows = con.execute(
            "SELECT id, repo, name, kind, file_path, start_line, qualified_name,"
            " signature FROM symbols WHERE (file_path = ? OR file_path LIKE ? ESCAPE '\\')"
            " AND kind != 'import'"
            f" AND repo IN ({qmarks}){extra} LIMIT ?",
            args,
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{"id": sid, "repo": repo, "name": name, "kind": kind, "file_path": fp,
             "start_line": ln, "qualified_name": qual or "", "signature": sig or ""}
            for sid, repo, name, kind, fp, ln, qual, sig in rows]


def _path_withholds(file_path: str | None, withheld) -> bool:
    """True when a WITHHELD repo name appears as a path component."""
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
    """Route-kind rows matching the route's own `name` (never qualified)."""
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
    """A name split on every separator layer (feeds `_path_withholds`)."""
    return [p for p in re.split(r"[-_./]+", (s or "").lower()) if p]


# Federation report lines are kept by LINE SHAPE: only `Layer N:` headers.
_LAYER_RE = re.compile(r"^\s*Layer \d+:\s*$")


def read_fed_report(path: str,
                    line_cap: int = FED_REPORT_LINE_CAP) -> dict:
    """Header stamp + `Layer N:` lines of the federation report (capped)."""
    lines: list = []
    try:
        # encoding pinned: the report carries glyphs cp1252 cannot round-trip.
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
    """First timestamp token of the header stamp (None when unparseable)."""
    text = (generated or "").strip()
    if text[:9].lower() == "generated":
        text = text[9:].strip()
    tok = (text.split() or [""])[0]
    return tok if _parse_instant(tok) is not None else None


def read_structurizr(path: str, repos: list) -> dict:
    """Deploy units for `repos` (container `group` per matched repo)."""
    try:
        with open(path, encoding="utf-8") as fh:
            ws = json.load(fh)
    except (OSError, ValueError) as exc:
        return {"status": f"unavailable: {exc}"}
    model = ws.get("model", {}) if isinstance(ws, dict) else {}
    by_name = {}
    for s in model.get("softwareSystems", []) or []:
        for c in (s.get("containers", []) or []) if isinstance(s, dict) else []:
            if isinstance(c, dict):
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
    """origin/main HEAD commit time as the repo.merged proxy (None on failure)."""
    if not repo_path:
        return None
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "origin/main"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace")
        return (out.stdout or "").strip() or None
    except Exception:
        return None


def indexed_sha(repo_path: str | None) -> str | None:
    """Pinned sha from the repo's .codegraph/indexed_origin_main (#1393)."""
    if not repo_path:
        return None
    try:
        with open(os.path.join(repo_path, ".codegraph", "indexed_origin_main"),
                  encoding="utf-8") as fh:
            sha = fh.read().strip().split()[0]
        return sha if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else None
    except (OSError, IndexError):
        return None


WITHHOLD_REASON = ("not public and not listed for this card's project in card-allowlist.json; "
                   "card artifacts show public repos plus the card project allowlist only")


POST_SAFE_WITHHOLD_TAIL = "names not shown in posted extracts"


def redact_for_json(ex: dict, post_safe: bool = False) -> dict:
    """A `--json`-safe copy of the extract (never mutates the input)."""
    out = dict(ex)
    withheld = set(ex.get("withheld") or [])
    if post_safe:
        # Post-safe strips every withheld/missing name; missing cannot even
        # keep a count (card-text dependent), unlike the withheld set.
        out["registry"] = {n: m for n, m in (ex.get("registry") or {}).items()
                           if n not in withheld}
        out["withheld"] = []
        out["withheld_count"] = len(withheld)
        out["withheld_hits"] = {}
        out["missing_repos"] = []
        return out
    out["registry"] = {
        n: ({"name": n, "visibility": (m or {}).get("visibility"),
             "reason": WITHHOLD_REASON} if n in withheld else m)
        for n, m in (ex.get("registry") or {}).items()
    }
    return out


def fed_docs_state(fed_docs_db: str) -> str:
    """fed-docs.db presence wording, independent of every other source."""
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
                  card_allowlist_path: str | None = DEFAULT_CARD_ALLOWLIST,
                  entitled: bool = False) -> dict:
    """Assemble the extract dict. Never raises on bad sources (unavailable)."""
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
        visible, withheld = split_visible(
            registry, project_slug, card_allowlist, entitled=entitled)
        # Allowlist entries (entitlement-gated: no oracle) plus card-named
        # `swarph-...` names the registry lacks.
        _allow_entries = (((card_allowlist or {}).get(project_slug) or [])
                          if entitled else [])
        missing_repos = sorted(
            {r for r in _allow_entries
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
    # Shape-only federation half: header stamp + `Layer N:` lines only.
    struct = read_structurizr(structurizr_ws, repos_hit)
    fed = read_fed_report(fed_report)
    pins = {repo: {"sha": indexed_sha((registry.get(repo) or {}).get("path")),
                  "indexed_at": (registry.get(repo) or {}).get("indexed_at"),
                  "merged_at": merged_lookup((registry.get(repo) or {}).get("path"))}
            for repo in repos_hit}
    fed_build = (_stamp_time(fed.get("generated"))
                 if fed.get("status") == "ok" else None)
    return {
        "index": {"status": "ok", "path": index_path},
        "tokens": tokens, "paths": list(paths or []),
        "symbols": syms, "routes": routes,
        "repos_hit": repos_hit, "withheld_hits": withheld_hits,
        "visible": visible, "withheld": withheld, "registry": registry,
        "project_slug": project_slug, "missing_repos": missing_repos,
        "entitled": bool(entitled),
        "federation_report": fed, "structurizr": struct,
        "fed_build": fed_build,
        "fed_docs": fed_docs,
        "pins": pins,
        "card": ({"id": card.get("id"), "title": card.get("title")}
                 if card else None),
    }


def _parse_instant(s) -> "datetime.datetime | None":
    """A timestamp as an aware UTC instant; None when unusable."""
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
    """True when the federation build predates the repo merge (None if unknown)."""
    fb, mg = _parse_instant(fed_build), _parse_instant(merged_at)
    return None if fb is None or mg is None else fb < mg


def render(ex: dict, max_lines: int = DEFAULT_MAX_LINES,
           post_safe: bool = False) -> str:
    """Render the extract text; post-safe mode names no withheld/missing repo."""
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
        # One coverage line always (index ok); post-safe uses the generic
        # wording (no withheld names in posts).
        _cov_names = (", ".join(ex.get("withheld", []) or [])
                      if not post_safe else "")
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
        # Post-safe: no missing-repo lines at all — a posted body naming a
        # nonexistent repo must read the same as one naming a real private
        # repo (no existence oracle).
        for repo in ([] if post_safe else ex.get("missing_repos", [])):
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
            # Gateway-copy note (#1404/#1410): only for this path shape and
            # only while mesh-gateway is withheld (else the warning is false).
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
            # Post-safe: dropped entirely (each names a withheld repo).
            _hits = {} if post_safe else (ex.get("withheld_hits") or {}).get(tok, {})
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
        head.append("pins (per repo: indexed sha + index time):")
        for repo, p in ex.get("pins", {}).items():
            sha = p.get("sha") or "sha unavailable: .codegraph/indexed_origin_main missing"
            head.append(f"- {repo}: {sha} @ {p.get('indexed_at') or 'index time unavailable'}")
    head.append("")
    head.append("## federation half (READ-ONLY)")
    fed = ex.get("federation_report", {})
    st = ex.get("structurizr", {})
    # The federation half prints the report stamp + `Layer N:` lines, or
    # the report's own unavailable line.
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
        if st.get("deploy_units"):
            head.append(f"deploy units: {', '.join(st['deploy_units'])}")
        else:
            head.append("deploy units: unavailable (floor: no group-tagged container matched)")
    # Reserved tail (never cut away by the cap): WITHHELD, fed-docs, freshness.
    tail = [""]
    withheld = ex.get("withheld", [])
    if withheld:
        _tail_detail = (
            f"{len(withheld)} repos ({POST_SAFE_WITHHOLD_TAIL})"
            if post_safe else
            f"{', '.join(withheld)} ({WITHHOLD_REASON} — "
            "same public+allowlist composition as "
            "codegraph._visible_repos, caller replaced by the card's "
            "project slug; full list in --json)")
        tail.append(f"WITHHELD: {_tail_detail}")
    _fed_docs = ex.get("fed_docs") or \
        "unavailable: fed-docs state not recorded"
    if isinstance(_fed_docs, str) and _fed_docs.startswith("unavailable"):
        _link = ("absent on this box, "
                 "so citations beyond the subject card are not linked yet")
    else:
        _link = ("present on this box, "
                 "but citations beyond the subject card are not linked yet")
    tail.append(f"fed-docs.db: {_fed_docs} "
                f"(cards/docs/memories citation index — {_link})")
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
                            "origin/main-HEAD proxy)")
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
