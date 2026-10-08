"""``swarph sweep`` — the card-#1064 stuck-work sweep, one daily shot.

Finds (a) open/taken rows blocked >=48h on an unmoving dependency and (b)
approved-or-old open PRs with no obligation row naming them; DMs each
finding's BLOCKING holder once per 24h (never the waiter) and appends one
line per finding to the nightly watchtower report file. Never closes or
reassigns: the only board write is the DM itself (POST /messages).

  swarph sweep [--dry-run] [--repos R1,R2] [--state-file P] [--report-file P]
      [--as SELF] [--gateway URL] [--token-file F] [--json]

--dry-run reads the live board and GitHub, prints the findings, and writes
nothing (no DMs, no state, no report) — the rehearsal shape.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

_STUCK_H = 48
_RESEND_H = 24

_DEFAULT_REPOS = (
    "BrainSurfing-tech/swarph-cli",
    "BrainSurfing-tech/mesh-gateway",
    "BrainSurfing-tech/swarph-codegraph-fed",
)

_PR_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE)
_PR_NUM_RE = re.compile(r"\bPR\s*#?(\d+)\b", re.IGNORECASE)


def _parse_ts(v):
    """Tolerant ISO parse -> epoch seconds, or None. Never raises: an
    unparseable timestamp is unevaluable, not zero."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    try:
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _age_days(now, ts):
    return round((now - ts) / 86400)


def _row_last_change(row):
    for k in ("closed_at", "taken_at", "created_at"):
        ts = _parse_ts(row.get(k))
        if ts is not None:
            return ts
    return None


def _named_prs(rows):
    """(repo-or-None, number) pairs obligation rows name: full URLs match
    exactly; bare PR numbers match by number across tracked repos."""
    named = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        blob = " ".join(
            str(row.get(k) or "") for k in ("accept", "close_evidence"))
        for m in _PR_URL_RE.finditer(blob):
            try:
                named.add((m.group(1).lower(), int(m.group(2))))
            except ValueError:
                continue
        for m in _PR_NUM_RE.finditer(blob):
            try:
                named.add((None, int(m.group(1))))
            except ValueError:
                continue
    return named


def _graph_steps(http_get, gateway, token, card_id, cache):
    if card_id in cache:
        return cache[card_id]
    try:
        status, body = http_get(
            f"{gateway}/board/cards/{card_id}/graph", token)
    except Exception:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    steps = body.get("steps")
    if not isinstance(steps, list):
        return None
    cache[card_id] = steps
    return steps


def _row_findings(rows, by_id, http_get, gateway, token, now):
    """Kind (a): one finding per (open taken waiter, unsatisfied old need)."""
    out = []
    cache = {}
    for w in rows:
        if w.get("status") != "open" or not w.get("taken_at"):
            continue
        step = w.get("step")
        if not step:
            continue
        card_id = w.get("card_id")
        if not isinstance(card_id, int) or isinstance(card_id, bool):
            continue
        steps = _graph_steps(http_get, gateway, token, card_id, cache)
        if steps is None:
            continue
        entry = next((e for e in steps
                      if isinstance(e, dict) and e.get("step") == step), None)
        if entry is None:
            continue
        needs = entry.get("needs")
        if not isinstance(needs, list):
            continue
        for need in needs:
            if not isinstance(need, dict):
                continue
            sat = need.get("satisfied")
            if sat is True:
                continue
            if sat is not False:
                continue  # unevaluable need names no finding
            rid = need.get("row_id")
            dep = by_id.get(rid)
            if not isinstance(dep, dict):
                continue
            if dep.get("status") == "closed":
                continue  # terminal: satisfied or a rework case, not unmoving
            changed = _row_last_change(dep)
            if changed is None or now - changed < _STUCK_H * 3600:
                continue
            holder = dep.get("holder")
            blocker = holder if holder and dep.get("holder_known", True) else None
            out.append({
                "kind": "row",
                "waiter_id": w.get("id"),
                "waiter": w.get("holder"),
                "waiter_step": step,
                "card_id": card_id,
                "need": need.get("step"),
                "need_id": rid,
                "blocker": blocker,
                "age_days": _age_days(now, changed),
            })
    return out


def _pr_findings(named, gh_prs, repos, peers, now):
    """Kind (b): approved-or-old open PRs no row names. An author with no
    mesh peer is report-only — the CLI must not invent a recipient."""
    out = []
    for repo in repos:
        try:
            prs = gh_prs(repo)
        except Exception:
            continue
        if not isinstance(prs, list):
            continue
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            try:
                number = int(pr.get("number"))
            except (TypeError, ValueError):
                continue
            if (repo.lower(), number) in named or (None, number) in named:
                continue
            approved = pr.get("reviewDecision") == "APPROVED"
            created = _parse_ts(pr.get("createdAt"))
            old = created is not None and now - created >= _STUCK_H * 3600
            if not approved and not old:
                continue
            author = pr.get("author") or {}
            login = author.get("login") if isinstance(author, dict) else None
            # blocker names the responsible party even when they have no
            # mesh peer — the SEND step DMs only peers (report-only else).
            out.append({
                "kind": "pr",
                "repo": repo,
                "number": number,
                "title": pr.get("title") or "",
                "url": pr.get("url") or "",
                "approved": approved,
                "age_days": _age_days(now, created) if created else None,
                "blocker": login,
            })
    return out


def _finding_key(f):
    if f["kind"] == "row":
        return f"row:{f['waiter_id']}:{f['need']}"
    return f"pr:{f['repo']}#{f['number']}"


def _report_line(now_iso, f):
    if f["kind"] == "row":
        return (f"{now_iso} STUCK row waiter=#{f['waiter_id']}({f['waiter']}) "
                f"step={f['waiter_step']} card=#{f['card_id']} "
                f"blocked-by=#{f['need_id']}({f['blocker']}) "
                f"need={f['need']} need-age={f['age_days']}d")
    how = "approved" if f["approved"] else f"age={f['age_days']}d"
    return (f"{now_iso} STUCK pr {f['repo']}#{f['number']} "
            f"'{f['title']}' ({how}, no review row) author={f['blocker']}")


def _dm_content(f):
    if f["kind"] == "row":
        return (f"STUCK-WORK SWEEP: row #{f['waiter_id']} ({f['waiter']}, "
                f"{f['waiter_step']}, card #{f['card_id']}) has waited on "
                f"your row #{f['need_id']} ({f['need']}, unchanged "
                f"{f['age_days']}d). Please move it, fail it, or say what "
                f"it waits on.")
    state = "approved" if f["approved"] else "open %sd" % (f['age_days'],)
    return (f"STUCK-WORK SWEEP: your PR {f['repo']}#{f['number']} "
            f"'{f['title']}' ({f['url']}) is {state} "
            f"with no review row naming it. Please land it or ask for review.")


def run_sweep(*, gateway, token, sender, repos, state_file, report_file,
              now, http_get, http_post, gh_prs, dry_run=False):
    """One sweep. Returns (rc, findings): rc 1 only when the board itself is
    unreadable (nothing evaluable); findings are the SENT (or dry-run, would
    send) findings. Only ever writes DMs + the two local files."""
    gateway = gateway.rstrip("/")
    try:
        status, body = http_get(f"{gateway}/board/obligations", token)
    except Exception:
        status, body = None, None
    if status != 200 or not isinstance(body, dict) or not isinstance(
            body.get("obligations"), list):
        print("swarph sweep: board unreadable — no findings, nothing sent",
              file=sys.stderr)
        return 1, []
    rows = [r for r in body["obligations"] if isinstance(r, dict)]
    by_id = {r.get("id"): r for r in rows
             if isinstance(r.get("id"), int)}
    try:
        pstatus, pbody = http_get(f"{gateway}/peers", token)
        peers = {p["name"] for p in pbody.get("peers", [])
                 if isinstance(p, dict) and p.get("name")}
        if pstatus != 200:
            peers = set()
    except Exception:
        peers = set()

    findings = _row_findings(rows, by_id, http_get, gateway, token, now)
    findings += _pr_findings(_named_prs(rows), gh_prs, repos, peers, now)

    try:
        with open(state_file, encoding="utf-8") as fp:
            sent = json.load(fp)
        sent = sent if isinstance(sent, dict) else {}
    except (OSError, ValueError):
        sent = {}
    now_iso = datetime.fromtimestamp(now, timezone.utc).isoformat()
    fresh, updates = [], {}
    for f in findings:
        key = _finding_key(f)
        try:
            last = _parse_ts(sent.get(key))
        except Exception:
            last = None
        if last is not None and now - last < _RESEND_H * 3600:
            continue
        fresh.append(f)
        updates[key] = now_iso

    if dry_run:
        for f in fresh:
            print("DRY " + _report_line(now_iso, f))
        return 0, fresh

    for f in fresh:
        # DM only a blocker with a mesh peer: pinging the waiter instead is
        # the FAIL, and inventing a recipient for a gh login is its cousin.
        if f["blocker"] is not None and f["blocker"] in peers:
            status, payload = http_post(
                f"{gateway}/messages",
                {"from_node": sender, "to_node": f["blocker"],
                 "kind": "fyi", "content": _dm_content(f)},
                token)
            if status < 200 or status >= 300:
                detail = (payload.get("detail", "<gateway error>")
                          if isinstance(payload, dict) else "<gateway error>")
                print(f"swarph sweep: DM to {f['blocker']} failed "
                      f"{status}: {detail}", file=sys.stderr)
                continue
        line = _report_line(now_iso, f)
        print(line)
        try:
            with open(report_file, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError as exc:
            print(f"swarph sweep: report append failed: {exc}",
                  file=sys.stderr)
    if updates:
        sent.update(updates)
        try:
            with open(state_file, "w", encoding="utf-8") as fp:
                json.dump(sent, fp)
        except OSError as exc:
            print(f"swarph sweep: state write failed: {exc}", file=sys.stderr)
    return 0, fresh


def _gh_prs(repo):
    """Live gh transport: open PRs with the fields the matcher needs, or
    None when gh is unusable (that repo's findings are skipped, not
    invented)."""
    try:
        p = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--limit", "100", "--json",
             "number,title,createdAt,reviewDecision,url,author"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    try:
        data = json.loads(p.stdout or "[]")
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _default_state_file():
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    root = xdg if xdg else os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(root, "swarph", "stuck-sweep.json")


def _build_parser():
    p = argparse.ArgumentParser(
        prog="swarph sweep",
        description="Daily stuck-work sweep: blocked rows + unreviewed PRs "
                    "(card #1064). DMs blockers, never closes/reassigns.")
    p.add_argument("--dry-run", action="store_true",
                   help="print findings; send/record/write nothing")
    p.add_argument("--repos", default=None,
                   help="comma-separated owner/repo list (default "
                        "$SWARPH_SWEEP_REPOS or the fleet three)")
    p.add_argument("--state-file", default=None)
    p.add_argument("--report-file", default=None,
                   help="nightly watchtower report path "
                        "(default $SWARPH_SWEEP_REPORT or state-adjacent log)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--as", dest="self_name", default=None,
                   help="DM sender identity")
    from swarph_cli.gateway_default import env_gateway
    p.add_argument("--gateway", default=env_gateway())
    p.add_argument("--token-file", default=None)
    return p


def run_sweep_cmd(argv):
    import time as _time
    from swarph_cli.commands import mesh as _mesh
    from swarph_cli.tokens import resolve_token
    args = _build_parser().parse_args(argv)
    try:
        # #872 doctrine: an automated DM must name its sender. Dry-run sends
        # nothing and needs no identity.
        if not args.self_name and not args.dry_run:
            print("swarph sweep: --as a sender is required (automated DMs "
                  "must name their sender; --dry-run needs no identity)",
                  file=sys.stderr)
            return 2
        resolution = resolve_token(
            args.self_name, args.token_file,
            identity_is_explicit=bool(args.self_name))
        if resolution is None:
            print("swarph sweep: no token (set MESH_GATEWAY_TOKEN, pass "
                  "--token-file, or --as a sender)", file=sys.stderr)
            return 2
        sender = args.self_name or "(dry-run)"
        repos = ([r.strip() for r in args.repos.split(",") if r.strip()]
                 if args.repos else
                 [r.strip() for r in os.environ.get(
                     "SWARPH_SWEEP_REPOS", ",".join(_DEFAULT_REPOS)
                 ).split(",") if r.strip()])
        state_file = args.state_file or os.environ.get(
            "SWARPH_SWEEP_STATE", _default_state_file())
        report_file = (args.report_file or os.environ.get("SWARPH_SWEEP_REPORT")
                       or os.path.splitext(state_file)[0] + ".log")
        rc, findings = run_sweep(
            gateway=args.gateway, token=resolution.token, sender=sender,
            repos=repos, state_file=state_file, report_file=report_file,
            now=_time.time(), http_get=_mesh._http_get_json,
            http_post=_mesh._post_json, gh_prs=_gh_prs,
            dry_run=args.dry_run)
        if args.json:
            print(json.dumps(findings, indent=2))
        return rc
    except RuntimeError as exc:
        print(f"swarph sweep: {exc}", file=sys.stderr)
        return 2
