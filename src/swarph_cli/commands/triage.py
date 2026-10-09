"""``swarph triage`` — the card-#1073 organ triage router, one daily shot.

Sensors emit {source_organ, payload, evidence}; the router assigns
{type, confidence, owner} and appends to the owner's ONE open daily
triage row ("<owner> triage", at most 10 items, carried never
duplicated). First consumer: the #1064 stuck-work sweep's blocked rows.

Seven binding rulings (#1413) hold: identifiers-only evidence, owner map
as data, PROVEN-written thresholds (no literals), deliberate-wait via
the need card's due_at, one open row per owner, benign-in-footer, never
close/edit/delete, scrub-before-model.

  swarph triage [--dry-run] [--state-file P] [--owners-file P] [--json]
      [--as SELF] [--gateway URL] [--token-file F]

--dry-run reads the live board and prints the routing; it sends,
records, appends, and creates nothing. The one-a-day real-stall DM
shares the sweep's state file, so whichever job runs first consumes the
slot and the other stays silent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from swarph_cli.commands import sweep as _sweep
from swarph_cli.triage import backend as _backend
from swarph_cli.triage import job as _job
from swarph_cli.triage import router as _router
from swarph_cli.triage import sensor as _sensor

_OWNERS_DEFAULT = os.path.join(os.path.dirname(
    _router.__file__), "owners.json")


def _need_card_due_at(http_get, gateway, token, card_id, cache):
    if card_id in cache:
        return cache[card_id]
    try:
        status, body = http_get(f"{gateway}/board/cards/{card_id}", token)
    except Exception:
        cache[card_id] = None
        return None
    due = body.get("due_at") if status == 200 and isinstance(body, dict) else None
    cache[card_id] = due if isinstance(due, str) else None
    return cache[card_id]


def _stuck_records(rows, by_id, peers, http_get, gateway, token, now,
                   now_iso):
    """Board walk: like the sweep's kind-(a) matcher, except a CLOSED need
    row classifies as needed-row-closed-race (the sweep skips it) and a
    need naming no row at all becomes a missing-need race record."""
    records = []
    graph_cache: dict = {}
    card_cache: dict = {}
    for waiter in rows:
        if waiter.get("status") != "open" or not waiter.get("taken_at"):
            continue
        step = waiter.get("step")
        if not step:
            continue
        card_id = waiter.get("card_id")
        if not isinstance(card_id, int) or isinstance(card_id, bool):
            continue
        steps = _sweep._graph_steps(http_get, gateway, token, card_id,
                                    graph_cache)
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
            satisfied = need.get("satisfied")
            if satisfied is True:
                continue
            if satisfied is not False:
                continue  # unevaluable need names no record
            rid = need.get("row_id")
            dep = by_id.get(rid)
            base = {
                "source_organ": _sensor.ORGAN_STUCK,
                "waiter_id": waiter.get("id"),
                "card_id": card_id,
                "need": need.get("step"),
                "need_id": rid,
            }
            if not isinstance(dep, dict):
                record = dict(base, type="race", race="missing-need",
                              confidence=None, owner_hint=None,
                              holder_is_peer=False,
                              evidence=_sensor.stuck_evidence(
                                  waiter.get("id"), rid),
                              item_id=(f"stuck:missing:{waiter.get('id')}:"
                                       f"{need.get('step')}"),
                              sharp=False, security=False)
                records.append(record)
                continue
            holder = dep.get("holder")
            blocker = (holder if holder and dep.get("holder_known", True)
                       else None)
            if dep.get("status") == "closed":
                classified = _sensor.map_stuck_finding(
                    {"blocker": blocker, "id": waiter.get("id"),
                     "need": need.get("step")},
                    "closed", None, now_iso)
            else:
                changed = _sweep._row_last_change(dep)
                if changed is None or now - changed < _sweep._STUCK_H * 3600:
                    continue
                need_card_id = dep.get("card_id")
                due_at = (_need_card_due_at(http_get, gateway, token,
                                            need_card_id, card_cache)
                          if isinstance(need_card_id, int)
                          and not isinstance(need_card_id, bool) else None)
                classified = _sensor.map_stuck_finding(
                    {"blocker": blocker, "id": waiter.get("id"),
                     "need": need.get("step")},
                    "open", due_at, now_iso)
            record = dict(
                base, type=classified["type"],
                confidence=classified["confidence"],
                sharp=classified["sharp"], owner_hint=blocker,
                holder_is_peer=bool(blocker and blocker in peers),
                evidence=_sensor.stuck_evidence(waiter.get("id"), rid),
                item_id=(f"stuck:{waiter.get('id')}:{rid}:"
                         f"{need.get('step')}"),
                security=False)
            records.append(record)
    return records


def _load_state(state_file):
    try:
        with open(state_file, encoding="utf-8") as fp:
            state = json.load(fp)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state_file, state):
    try:
        with open(state_file, "w", encoding="utf-8") as fp:
            json.dump(state, fp)
    except OSError as exc:
        print(f"swarph triage: state write failed: {exc}", file=sys.stderr)


def run_triage(*, gateway, token, sender, owners_map, state_file,
               store_backend, peers_fetcher, board_fetcher, http_get,
               http_post, now, dry_run=False):
    """One triage run. Returns (rc, summary). Reads the board; appends to
    owner rows and DMs sharp real-stall blockers (one-a-day, shared with
    the sweep's state) unless dry_run."""
    rows, by_id, peers = board_fetcher()
    if rows is None:
        print("swarph triage: board unreadable — no records, nothing sent",
              file=sys.stderr)
        return 1, {}
    now_iso = datetime.fromtimestamp(now, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    records = _stuck_records(rows, by_id, peers, http_get, gateway, token,
                             now, now_iso)
    sent = _load_state(state_file)

    def dm_sender(record, owner):
        finding = {
            "kind": "row",
            "waiter_id": record.get("waiter_id"),
            "waiter": None,
            "waiter_step": None,
            "card_id": record.get("card_id"),
            "need": record.get("need"),
            "need_id": record.get("need_id"),
            "blocker": record.get("owner_hint"),
            "age_days": None,
        }
        key = _sweep._finding_key(finding)
        try:
            last = _sweep._parse_ts(sent.get(key))
        except Exception:
            last = None
        if last is not None and now - last < _sweep._RESEND_H * 3600:
            return False
        if dry_run:
            return True
        blocker = record.get("owner_hint")
        if blocker is None or blocker not in peers:
            return False
        status, payload = http_post(
            f"{gateway}/messages",
            {"from_node": sender, "to_node": blocker,
             "kind": "fyi", "content": _sweep._dm_content(finding)},
            token)
        if status < 200 or status >= 300:
            detail = (payload.get("detail", "<gateway error>")
                      if isinstance(payload, dict) else "<gateway error>")
            print(f"swarph triage: DM to {blocker} failed "
                  f"{status}: {detail}", file=sys.stderr)
            return False
        sent[key] = now_iso
        return True

    from swarph_cli.triage.rows import TriageStore
    store = TriageStore(store_backend)
    if dry_run:
        for record in records:
            print(f"DRY {record['source_organ']} {record['type']} "
                  f"sharp={record['sharp']} item={record['item_id']}")
        return 0, {"dry_run_records": len(records)}
    summary = _job.route_and_store(store, owners_map, records,
                                   dm_sender=dm_sender)
    _save_state(state_file, sent)
    return 0, summary


def _board_fetcher(http_get, gateway, token):
    def fetch():
        try:
            status, body = http_get(f"{gateway}/board/obligations", token)
        except Exception:
            return None, None, set()
        if (status != 200 or not isinstance(body, dict)
                or not isinstance(body.get("obligations"), list)):
            return None, None, set()
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
        return rows, by_id, peers
    return fetch


def _build_parser():
    p = argparse.ArgumentParser(
        prog="swarph triage",
        description="Daily organ triage router: stuck-work findings onto "
                    "owner triage rows (card #1073). Appends + one-a-day "
                    "DMs only; never closes/edits/deletes.")
    p.add_argument("--dry-run", action="store_true",
                   help="print routing; send/record/append/create nothing")
    p.add_argument("--state-file", default=None,
                   help="one-a-day DM state (default shares the sweep's)")
    p.add_argument("--owners-file", default=None,
                   help="local owner-map override (PROVEN calibration "
                        "writes here; deep-merges over shipped owners.json)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--as", dest="self_name", default=None,
                   help="DM sender identity")
    from swarph_cli.gateway_default import env_gateway
    p.add_argument("--gateway", default=env_gateway())
    p.add_argument("--token-file", default=None)
    return p


def run_triage_cmd(argv):
    from swarph_cli.commands import mesh as _mesh
    from swarph_cli.tokens import resolve_token
    args = _build_parser().parse_args(argv)
    try:
        if not args.self_name and not args.dry_run:
            print("swarph triage: --as a sender is required (automated DMs "
                  "must name their sender; --dry-run needs no identity)",
                  file=sys.stderr)
            return 2
        resolution = resolve_token(
            args.self_name, args.token_file,
            identity_is_explicit=bool(args.self_name))
        if resolution is None:
            print("swarph triage: no token (set MESH_GATEWAY_TOKEN, pass "
                  "--token-file, or --as a sender)", file=sys.stderr)
            return 2
        sender = args.self_name or "(dry-run)"
        try:
            owners_map = _router.load_owners(_OWNERS_DEFAULT,
                                             args.owners_file)
        except (OSError, ValueError) as exc:
            print(f"swarph triage: owner map unreadable: {exc}",
                  file=sys.stderr)
            return 2
        state_file = args.state_file or os.environ.get(
            "SWARPH_SWEEP_STATE", _sweep._default_state_file())
        backend = _backend.BoardBackend(
            args.gateway, resolution.token, sender,
            _mesh._http_get_json, _mesh._post_json)
        rc, summary = run_triage(
            gateway=args.gateway, token=resolution.token, sender=sender,
            owners_map=owners_map, state_file=state_file,
            store_backend=backend,
            peers_fetcher=None,
            board_fetcher=_board_fetcher(_mesh._http_get_json,
                                         args.gateway, resolution.token),
            http_get=_mesh._http_get_json, http_post=_mesh._post_json,
            now=time.time(), dry_run=args.dry_run)
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            footer = summary.get("footer", {})
            print(f"swarph triage: rc={rc} "
                  f"appended={summary.get('appended', 0)} "
                  f"duplicate={summary.get('duplicate', 0)} "
                  f"overflow={summary.get('overflow', 0)} "
                  f"dm_sent={summary.get('dm_sent', 0)} "
                  f"footer={json.dumps(footer, sort_keys=True)}")
        return rc
    except RuntimeError as exc:
        print(f"swarph triage: {exc}", file=sys.stderr)
        return 2
