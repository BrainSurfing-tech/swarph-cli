"""LLM fleet control — allowlisted read + privileged actions for the 4 provider
lanes. server.py delegates here; this module owns the allowlist (the security
boundary) and the only call into the fleetctl sudo wrapper."""
from __future__ import annotations
import concurrent.futures, json, logging, os, subprocess, sys, tempfile, time, urllib.request
from typing import Optional

_log = logging.getLogger("mesh-gateway")

class FleetError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status; self.detail = detail; super().__init__(detail)

LANES = ("claude-service", "gpt-service", "gemini-service", "grok-service")
ACTIONS = ("start", "stop", "restart", "set-model")
LANE_META = {  # lane -> (peer/node name, port, provider, env model key, env file)
    "claude-service": ("claude-node", 8787, "claude", "CLAUDE_MODEL", os.path.expanduser("~/claude-service/.env")),
    "gpt-service":    ("gpt-node",    8789, "gpt",    "GPT_MODEL",    os.path.expanduser("~/gpt-service/.env")),
    "gemini-service": ("gemini-node", 8790, "gemini", "GEMINI_MODEL", os.path.expanduser("~/gemini-service/.env")),
    "grok-service":   ("grok-node",   8791, "grok",   "GROK_MODEL",   os.path.expanduser("~/grok-service/.env")),
}
MODEL_ALLOWLIST = {
    "claude-service": ("claude",),  # claude -p uses the session default; pin not exposed v1
    "gpt-service":    ("gpt-5.x",),
    "gemini-service": ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite",
                       "gemini-3.1-flash-lite", "gemini-3-flash-preview"),
    "grok-service":   ("grok",),
}
# Hardcoded — NOT overridable from the environment. The wrapper path is part of
# the trust boundary (sudoers pins this exact path); an env override would let a
# poisoned PATH/env swap in an attacker binary. Tests may monkeypatch the attr.
FLEETCTL = "/usr/local/sbin/fleetctl"
AUDIT_PATH = os.environ.get(
    "FLEET_AUDIT_PATH",
    os.path.expanduser("~/.swarph/gateway/logs/fleet_audit.jsonl"),
)

def _lstat(path: str):
    """Thin indirection over os.lstat so the integrity check is monkeypatchable
    in unit tests without touching real root-owned files. lstat (NOT stat) so a
    symlink in the path chain is seen as a symlink, never followed."""
    return os.lstat(path)

def _realpath(path: str) -> str:
    """Indirection over os.path.realpath for the same monkeypatch reason."""
    return os.path.realpath(path)

def _wrapper_trusted() -> bool:
    """The fleetctl wrapper is privileged (runs via sudo as root). A root:root 0755
    file is STILL swappable if any directory in its path is writable by a non-root
    user (unlink+recreate, or symlink-swap the path), so we must verify the WHOLE
    path chain, not just the file. Trust the wrapper ONLY when ALL hold:

      1. realpath(FLEETCTL) == FLEETCTL — the resolved path is not a symlink
         redirect (reject any symlink in the chain).
      2. FLEETCTL exists, is root-owned (lstat.st_uid == 0), and is NOT
         group/other-writable (mode & 0o022 == 0).
      3. EVERY directory component from FLEETCTL's dir up to '/' is root-owned AND
         not group/other-writable. A writable parent dir is enough to swap the
         binary, so this is the load-bearing check.

    Any failure -> fail closed (False). Read-only listing does not call this."""
    try:
        # (1) reject symlink redirects anywhere in the path.
        if _realpath(FLEETCTL) != FLEETCTL:
            return False
        # (2) the wrapper file itself: root-owned, not group/other-writable.
        st = _lstat(FLEETCTL)
        if st.st_uid != 0 or (st.st_mode & 0o022) != 0:
            return False
        # (3) walk every parent dir up to '/': each root-owned + non-writable.
        d = os.path.dirname(FLEETCTL)
        while True:
            dst = _lstat(d)
            if dst.st_uid != 0 or (dst.st_mode & 0o022) != 0:
                return False
            if d == "/":
                break
            d = os.path.dirname(d)
    except (FileNotFoundError, OSError):
        return False
    return True

def _validate(lane: str, action: str) -> None:
    if lane not in LANES:
        raise FleetError(403, f"unknown lane {lane!r}")
    if action not in ACTIONS:
        raise FleetError(400, f"unknown action {action!r}")

def _unit_active(unit: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True,
                              text=True, timeout=5).stdout.strip() == "active"
    except Exception:
        return False

# The lanes bind a private/tailnet IP (a cell on 0.0.0.0 also answers there); the
# mesh peer-name may NOT be a resolvable host from the gateway, so probe a
# concrete IP. Env-overridable for tests/portability.
HEALTH_HOST = os.environ.get("FLEET_HEALTH_HOST", "127.0.0.1")

def _lane_health(lane: str) -> dict:
    node, port, provider, *_ = LANE_META[lane]
    try:
        with urllib.request.urlopen(f"http://{HEALTH_HOST}:{port}/health", timeout=3) as r:
            caps = json.loads(r.read().decode()).get("capabilities", {})
        return {"model": caps.get("default_model"), "authed": caps.get(f"{provider}_authed"),
                "sandbox": caps.get("sandbox")}
    except Exception:
        return {"model": None, "authed": None, "sandbox": None}

def _probe_lane(lane: str) -> dict:
    """Probe one lane's unit state + health. Swallows all exceptions so a dead
    lane yields its all-None row and never raises into the pool."""
    node, port, provider, *_ = LANE_META[lane]
    try:
        active = _unit_active(lane)
        h = _lane_health(lane) if active else {"model": None, "authed": None, "sandbox": None}
    except Exception:
        active = False
        h = {"model": None, "authed": None, "sandbox": None}
    return {"lane": lane, "node": node, "port": port, "provider": provider,
            "state": "active" if active else "inactive",
            "model": h["model"], "authed": h["authed"], "sandbox": h["sandbox"],
            "models_available": list(MODEL_ALLOWLIST[lane])}

def list_services() -> list[dict]:
    # Probe the 4 lanes concurrently so wall-time is ~max(5,3) not 4×8.
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        rows = list(ex.map(_probe_lane, LANES))
    return rows

def _audit(actor, lane, action, model, result):
    # An audit-write hiccup must never turn an already-succeeded privileged action
    # into a 500. Log the failure; keep the action result truthful.
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a") as f:
            f.write(json.dumps({"ts": time.time(), "actor": actor, "lane": lane,
                                "action": action, "model": model, "result": result}) + "\n")
    except Exception as e:
        msg = f"fleet audit-write failed ({type(e).__name__}): {lane}/{action} result={result}"
        try:
            _log.warning(msg)
        except Exception:
            print(msg, file=sys.stderr)

def _set_env_model(lane: str, model: str) -> None:
    key = LANE_META[lane][3]; path = LANE_META[lane][4]
    lines, found = [], False
    try:
        with open(path) as f: lines = f.read().splitlines()
    except FileNotFoundError:
        raise FleetError(500, f"{lane} .env not found")
    orig_count = len(lines)
    for i, ln in enumerate(lines):
        if ln.startswith(key + "="):
            lines[i] = f"{key}={model}"; found = True; break
    if not found: lines.append(f"{key}={model}")
    new_lines = lines
    # Atomic replace: a crash mid-write must NEVER leave the lane .env truncated
    # (that would drop the lane token + MESH_GATEWAY_TOKEN). Write a temp file in
    # the SAME dir (so os.replace is a same-filesystem atomic rename), fsync, then
    # swap. On any failure the original file is left untouched.
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".env.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(new_lines) + "\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise FleetError(500, f"{lane} .env atomic write failed")
    # Post-replace validation: never silently leave a lane .env shorter than it
    # was or missing the model line.
    try:
        with open(path) as f: after = f.read().splitlines()
    except OSError:
        raise FleetError(500, f"{lane} .env unreadable after write")
    if len(after) < orig_count or f"{key}={model}" not in after:
        raise FleetError(500, f"{lane} .env validation failed after write")
    os.chmod(path, 0o600)

def run_action(lane: str, action: str, model: Optional[str], actor: str = "commander") -> dict:
    _validate(lane, action)
    # Fail closed if the privileged wrapper can't be trusted (missing, non-root,
    # or world/group-writable). This gates ALL actions BEFORE any .env mutation or
    # sudo call. list_services() (read) deliberately does not gate on this.
    if not _wrapper_trusted():
        raise FleetError(503, "fleet control disabled: wrapper integrity check failed")
    if action == "set-model":
        if model not in MODEL_ALLOWLIST.get(lane, ()):  # rejects None + bogus
            raise FleetError(400, f"model {model!r} not allowed for {lane}")
        _set_env_model(lane, model)
    # set-model only restarts the unit; the model is pre-written into the lane
    # .env above, so the wrapper takes no model argv (fleetctl ignores $3).
    # `sudo -n`: the gateway runs as `ubuntu`; sudoers grants NOPASSWD for exactly
    # this wrapper. -n = non-interactive (never prompt; fail if a password is needed).
    argv = ["sudo", "-n", FLEETCTL, action, lane]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        _audit(actor, lane, action, model, "timeout")
        raise FleetError(500, "fleetctl timed out")
    except Exception as e:
        _audit(actor, lane, action, model, f"spawn-error:{type(e).__name__}")
        raise FleetError(500, f"fleetctl spawn failed: {type(e).__name__}")
    ok = p.returncode == 0
    _audit(actor, lane, action, model, "ok" if ok else f"rc={p.returncode}")
    if not ok:
        raise FleetError(500, f"fleetctl rc={p.returncode}: {p.stderr.strip()[:200]}")
    return {"ok": True, "lane": lane, "action": action, "model": model}
