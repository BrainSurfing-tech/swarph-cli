"""LLM lane registry + control. server.py delegates here. A lane is config:
{name, provider, model, n}; workers (llm-lane@<name>) pull from the task queue
tagged category='lane:<name>'. Mirrors services_control's allowlist discipline:
the PROVIDERS + NAME_RE allowlists are the security boundary — an unknown
provider/name/n fails closed (LaneError) BEFORE any DB write or fleetctl call.
"""
from __future__ import annotations
import json, logging, os, re, shutil, sqlite3, subprocess, sys, time

from . import services_control as _svc   # reuse _wrapper_trusted + FLEETCTL

_log = logging.getLogger("mesh-gateway")


class LaneError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status; self.detail = detail; super().__init__(detail)


PROVIDERS = ("claude", "gpt", "gemini", "grok")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}\Z")  # \Z not $: $ matches before a trailing \n
MAX_N = int(os.environ.get("LANE_MAX_N", "8"))
DB_PATH = os.environ.get(
    "LANES_DB_PATH",
    os.path.expanduser("~/.swarph/gateway/lanes.db"),
)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
AUDIT_PATH = os.environ.get(
    "LANE_AUDIT_PATH",
    os.path.expanduser("~/.swarph/gateway/logs/lane_audit.jsonl"),
)
# The lane WORKER writes its verdict to a file under this dir (task-<pk>.txt) and
# reports the path via /tasks/{pk}/complete (the live contract is output_path,
# not an inline result). MUST match lane_worker.RESULTS_DIR — the worker + the
# gateway share the box, so get_job reads the content back. The traversal guard
# in get_job only reads an output_path that resolves UNDER this dir.
RESULTS_DIR = os.environ.get(
    "LANE_RESULTS_DIR", os.path.expanduser("~/.swarph/lane-results"))
MAX_RESULT_BYTES = 256 * 1024   # bound the file read (output_path is semi-trusted)
# The curated per-lane context dir. The DEFAULT + the env key MUST match the lane
# WORKER's lane_worker.LANE_CONTEXT_ROOT — the gateway creates/populates this dir and
# the worker reads files from it at assemble time. Keep the two in sync.
LANE_CONTEXT_ROOT = os.environ.get(
    "LANE_CONTEXT_ROOT", os.path.expanduser("~/.swarph/lane-context"))
# A context filename is a bare basename (no dir, no traversal). Same allowlist
# discipline as NAME_RE: validate BEFORE any fs touch.
CTX_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS lanes (
        name TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
        n INTEGER NOT NULL, created REAL NOT NULL,
        context_text TEXT NOT NULL DEFAULT '',
        context_files TEXT NOT NULL DEFAULT '[]')""")
    # Upgrade an existing DB created before context columns (ADD COLUMN is a no-op
    # to add if missing; sqlite has no IF NOT EXISTS for columns, so probe first).
    cols = {r[1] for r in con.execute("PRAGMA table_info(lanes)")}
    if "context_text" not in cols:
        con.execute("ALTER TABLE lanes ADD COLUMN context_text TEXT NOT NULL DEFAULT ''")
    if "context_files" not in cols:
        con.execute("ALTER TABLE lanes ADD COLUMN context_files TEXT NOT NULL DEFAULT '[]'")
    con.commit(); con.close()


def _validate(name: str, provider: str, n: int) -> None:
    if not NAME_RE.match(name or ""):
        raise LaneError(400, f"bad lane name {name!r}")
    if provider not in PROVIDERS:
        raise LaneError(400, f"unknown provider {provider!r}")
    if not (0 <= n <= MAX_N):
        raise LaneError(400, f"n must be 0..{MAX_N}")


def _lane_ctx_dir(name: str) -> str:
    """Absolute path of the lane's curated context dir. NAME_RE-validated names
    only (caller passes a real lane name), so no traversal via the lane name."""
    return os.path.join(LANE_CONTEXT_ROOT, name)


def create_lane(name: str, provider: str, model: str, n: int = 0) -> dict:
    # Always persist the lane at n=0 — workers are brought up separately by
    # scale_lane. A row is "created but has 0 workers" until scaled, so a scale
    # failure can never leave a lane recorded at an n it never actually reached
    # (I2). n is still validated here so a bad target fails closed before any write.
    _validate(name, provider, n)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("INSERT INTO lanes VALUES (?,?,?,?,?,?,?)",
                    (name, provider, model, 0, time.time(), "", "[]"))
        con.commit()
    except sqlite3.IntegrityError:
        raise LaneError(409, f"lane {name!r} exists")
    finally:
        con.close()
    os.makedirs(_lane_ctx_dir(name), exist_ok=True)
    return {"name": name, "provider": provider, "model": model, "n": 0}


def list_lanes() -> list[dict]:
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM lanes ORDER BY name")]
    finally:
        con.close()


def get_lane(name: str) -> dict:
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM lanes WHERE name=?", (name,)).fetchone()
    finally:
        con.close()
    if not r:
        raise LaneError(404, f"lane {name!r} not found")
    return dict(r)


def delete_lane(name: str) -> None:
    get_lane(name)  # 404 if absent
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("DELETE FROM lanes WHERE name=?", (name,))
        con.commit()
    finally:
        con.close()
    try:
        shutil.rmtree(_lane_ctx_dir(name), ignore_errors=True)
    except Exception:
        pass


def _validate_ctx_files(name: str, files: list) -> list:
    """Each entry must be a safe basename that resolves to an existing regular file
    UNDER the lane's context dir (traversal + symlink-escape guard, mirroring
    _read_result_file). Returns the cleaned list or raises LaneError(400)."""
    if not isinstance(files, list):
        raise LaneError(400, "context_files must be a list of filenames")
    base = os.path.realpath(_lane_ctx_dir(name))
    out = []
    for f in files:
        if not isinstance(f, str) or not CTX_FILE_RE.match(f):
            raise LaneError(400, f"bad context filename {f!r}")
        try:
            real = os.path.realpath(os.path.join(base, f))
        except (ValueError, OSError):
            raise LaneError(400, f"bad context filename {f!r}")
        if real != base and not real.startswith(base + os.sep):
            raise LaneError(400, f"context file {f!r} escapes the lane dir")
        if not os.path.isfile(real):
            raise LaneError(400, f"context file {f!r} not found in lane dir")
        out.append(f)
    return out


def set_context(name: str, context_text: str, context_files: list) -> dict:
    get_lane(name)  # 404 if absent
    if not isinstance(context_text, str):
        raise LaneError(400, "context_text must be a string")
    files = _validate_ctx_files(name, context_files)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE lanes SET context_text=?, context_files=? WHERE name=?",
                    (context_text, json.dumps(files), name))
        con.commit()
    finally:
        con.close()
    return {"context_text": context_text, "context_files": files}


def get_context(name: str) -> dict:
    lane = get_lane(name)
    return {"context_text": lane.get("context_text", "") or "",
            "context_files": json.loads(lane.get("context_files") or "[]")}


def list_context_files(name: str) -> list:
    """Filenames available to attach: regular files directly under the lane dir."""
    get_lane(name)
    d = _lane_ctx_dir(name)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d)
                  if CTX_FILE_RE.match(f) and os.path.isfile(os.path.join(d, f)))


# ── A2: scale via the fleetctl llm-lane@* path ──────────────────────────────

def _audit(actor, lane, action, unit, result):
    # An audit-write hiccup must never turn an already-succeeded (or already-failed)
    # privileged action into a different outcome. Log the failure; never raise.
    # Mirrors services_control._audit.
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps({"ts": time.time(), "actor": actor, "lane": lane,
                                "action": action, "unit": unit, "result": result}) + "\n")
    except Exception as e:
        msg = f"lane audit-write failed ({type(e).__name__}): {lane}/{action} result={result}"
        try:
            _log.warning(msg)
        except Exception:
            print(msg, file=sys.stderr)


def _fleet(action: str, unit: str, *, actor: str = "commander", lane: str = "") -> None:
    """Call the fleetctl sudo wrapper exactly as services_control.run_action does
    — ["sudo","-n", FLEETCTL, action, unit] — gated by the SAME wrapper-integrity
    check (fail closed with 503 if the wrapper isn't root-owned + non-writable).
    The unit is always llm-lane@<NAME_RE-validated-name>.<i>; the upstream NAME_RE
    allowlist is the gate, so the wrapper's @* glob can't smuggle a bad unit.

    Honors the subprocess result: a non-zero rc or a spawn/timeout failure raises
    LaneError (silent-failure fix C1) and is audited. Returns None on success."""
    if not _svc._wrapper_trusted():
        raise LaneError(503, "fleet control disabled: wrapper integrity check failed")
    try:
        p = subprocess.run(["sudo", "-n", _svc.FLEETCTL, action, unit],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        _audit(actor, lane, action, unit, "timeout")
        raise LaneError(504, "fleetctl timeout")
    except Exception as e:
        _audit(actor, lane, action, unit, f"spawn-error:{type(e).__name__}")
        raise LaneError(500, f"fleetctl spawn failed: {type(e).__name__}")
    if p.returncode != 0:
        _audit(actor, lane, action, unit, f"rc={p.returncode}")
        raise LaneError(500, f"fleetctl rc={p.returncode}: {(p.stderr or '').strip()[:200]}")
    _audit(actor, lane, action, unit, "ok")
    return None


def _set_n(name: str, n: int) -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE lanes SET n=? WHERE name=?", (n, name))
        con.commit()
    finally:
        con.close()


def scale_lane(name: str, n: int) -> dict:
    lane = get_lane(name)                  # 404 if absent
    _validate(name, lane["provider"], n)   # fail-closed on bad n BEFORE any fleetctl call
    cur = lane["n"]
    # Record-what-happened (C2): track the last count actually applied. If a _fleet
    # call raises mid-loop, persist that last-applied count BEFORE re-raising so the
    # DB never claims more (or fewer) workers than fleetctl actually brought up.
    # Idempotent on retry — we do NOT roll back.
    applied = cur
    try:
        if n > cur:
            for i in range(cur + 1, n + 1):
                _fleet("enable", f"llm-lane@{name}.{i}", lane=name)
                applied = i
        elif n < cur:
            for i in range(cur, n, -1):
                _fleet("disable", f"llm-lane@{name}.{i}", lane=name)
                applied = i - 1
    except LaneError:
        _set_n(name, applied)
        raise
    _set_n(name, n)
    return {**lane, "n": n}


# ── A3: enqueue as a lane-tagged task (reuse the gateway /tasks queue) ───────

def enqueue(name: str, prompt: str, *, job_context_text: str = "",
            job_context_files: list | None = None) -> int:
    """Enqueue work for a lane as a task tagged category='lane:<name>'. Optional
    per-job context (operator-only at the API layer) is validated against the lane
    dir and rides in the task spec; the worker appends it after the lane context."""
    lane = get_lane(name)   # 404 if absent
    files = _validate_ctx_files(name, job_context_files or [])
    spec = {"prompt": prompt, "provider": lane["provider"], "model": lane["model"],
            "lane_context_text": lane.get("context_text", "") or "",
            "lane_context_files": json.loads(lane.get("context_files") or "[]"),
            "job_context_text": job_context_text or "", "job_context_files": files}
    return _create_task(f"lane:{name}", spec)


def _create_task(category: str, spec: dict) -> int:
    """Delegate to the gateway's shared task-insert path — server._insert_task —
    so lane work lands in the SAME claude_tasks queue as everything else (no new
    queue). Imported lazily to avoid an import cycle (server imports lanes_control)."""
    import json
    import server
    return server._insert_task(category, json.dumps(spec))


# ── B-seam: get_job — read the lane verdict back (result delivery) ──────────
# The lane WORKER reports completion via /tasks/{pk}/complete with an output_path
# (a task-<pk>.txt file under RESULTS_DIR), NOT an inline result. The
# enqueuer (POST /lanes/<name>/enqueue → job_id) had no way to read the verdict.
# get_job closes that: status + (done→output_path content / failed→error / else
# None). The file read is traversal-guarded — output_path is semi-trusted (it
# arrives from the worker via /complete) so only a path that resolves UNDER
# RESULTS_DIR is read, and the read is bounded to MAX_RESULT_BYTES.

# DB status → public job status. The live queue marks a claimed-but-running task
# 'in_progress'; the lane job contract exposes that as 'claimed'.
_STATUS_MAP = {"in_progress": "claimed"}


def _read_task_row(pk: int) -> dict | None:
    """Read the claude_tasks row for `pk` via the gateway's shared connection —
    reuses server._conn so there is one DB client. Returns {id, category, status,
    output_path, last_error} or None. Lazy import avoids the import cycle."""
    import server
    with server._conn() as c:
        r = c.execute(
            "SELECT id, category, status, output_path, last_error "
            "FROM claude_tasks WHERE id=?", (pk,)
        ).fetchone()
    return dict(r) if r is not None else None


def _read_result_file(output_path: str) -> str | None:
    """Read output_path ONLY if it resolves under RESULTS_DIR (traversal guard);
    bound the read to MAX_RESULT_BYTES. Returns None on any refusal / read error —
    never reads an arbitrary path, never raises into the caller."""
    if not output_path:
        return None
    base = os.path.realpath(RESULTS_DIR)
    # A NUL byte in output_path makes os.path.realpath raise ValueError (and open()
    # raises ValueError too). Treat any such malformed path as a refusal — return
    # None ("no result"), never raise into the caller.
    try:
        real = os.path.realpath(output_path)
    except (ValueError, OSError):
        return None
    # Must be base itself's child: prefix check with a trailing sep so
    # '/x/lane-results-evil' can't pass as under '/x/lane-results'.
    if real != base and not real.startswith(base + os.sep):
        return None
    try:
        with open(real, "r") as f:
            return f.read(MAX_RESULT_BYTES)
    except (ValueError, OSError):
        return None


def get_job(name: str, job_id: int) -> dict:
    """Read a lane job back by its enqueue job_id. Returns
    {job_id, status, result}: result is the verdict file content if done, the
    task error if failed, else None. 404 (LaneError) if the task is absent or its
    category != lane:<name> (it isn't this lane's job). The route stays thin."""
    row = _read_task_row(job_id)
    if row is None or row["category"] != f"lane:{name}":
        raise LaneError(404, f"job {job_id} not found for lane {name!r}")
    status = _STATUS_MAP.get(row["status"], row["status"])
    if status == "done":
        result = _read_result_file(row["output_path"])
    elif status == "failed":
        result = row["last_error"]
    else:
        result = None
    return {"job_id": job_id, "status": status, "result": result}
