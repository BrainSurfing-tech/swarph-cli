"""``swarph skunkworks`` -- run the Skunkworks research pipeline on subscription lanes.

Verbs: ``run``, ``status``, ``show``, ``stop``, ``decide`` (``--as CELL`` and ``--json`` on
each, as in ``swarph board``). The pipeline itself lives in ``swarph_mesh.skunkworks``
(swarph-mesh 0.9.1+). Ideation and the architect are jobs on the mesh queue, research is a
local ``claude -p`` with web tools only, and nothing is metered. There is no daemon: the
process that ran ``run`` is the run, and a killed process leaves it in ERROR.

Runs are refused Monday to Friday 09:30-16:00 America/New_York (no holiday calendar).
Approval produces no code. ``decide`` has no override: it calls ``Pipeline.approve`` and
prints the gate's refusal when the architect did not say APPROVED_FOR_BUILD.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable, Optional

from swarph_cli.commands._display import sanitize_terminal as _s
from swarph_cli.commands.board import (
    _card_say_payload,
    _project_owner_orchestrator,
    _thread_recipient,
)
from swarph_cli.commands.mesh import (
    _add_common,
    _http_get_json,
    _post_json,
    _resolve_self_name,
    _resolve_token,
)

# MessagePostRequest.content max_length (gateway server.py), used by POST /messages, which
# is the route `swarph board cards say` posts a card message through.
CARD_POST_LIMIT = 200_000
NEEDS_MESH = ("swarph skunkworks: needs swarph-mesh 0.9.x with skunkworks "
              "(pip install -U 'swarph-mesh>=0.9.1,<0.10')")

PostCard = Callable[[int, str, str], None]  # (card_id, kind, content); raises on failure


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarph skunkworks", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="start a run in this process (refused 09:30-16:00 New York, weekdays)")
    r.add_argument("--card", type=int, required=True, help="board card the run reports to")
    r.add_argument("--persona", required=True, help="persona pack name, e.g. swarph")
    r.add_argument("--seed-file", required=True, help="file holding the seed")
    r.add_argument("--blocker-question-file", default=None)
    r.add_argument("--context-file", action="append", default=[],
                   help="shown to the architect only; repeatable")

    s = sub.add_parser("status", help="state of a run")
    s.add_argument("run_id")
    w = sub.add_parser("show", help="print a run's stage outputs")
    w.add_argument("run_id")
    w.add_argument("--stage", choices=["ideation", "research", "architecture"], default=None)
    t = sub.add_parser("stop", help="write the STOP file; checked before each job")
    t.add_argument("run_id")
    d = sub.add_parser("decide", help="record the commander's decision (no override exists)")
    d.add_argument("run_id")
    d.add_argument("verdict", help="APPROVED, REJECTED or REVISE")
    d.add_argument("--quote-file", required=True,
                   help="file holding the commander's own words, posted to the card")
    for sp in (r, s, w, t, d):
        sp.add_argument("--json", action="store_true")
        _add_common(sp)
    return p


def _cut(text: str, path: str, limit: int = CARD_POST_LIMIT) -> str:
    if len(text) <= limit:
        return text
    note = f"\n[cut at {limit} characters; full text at {path}]"
    return text[: max(limit - len(note), 0)] + note


def _default_post_card(gw: str, token: str, cell: str, opt_out: bool) -> PostCard:
    """Post onto the card's thread the way ``swarph board cards say`` does."""
    def post(card_id: int, kind: str, content: str) -> None:
        st, card = _http_get_json(f"{gw}/board/cards/{card_id}", token)
        if st != 200:
            raise RuntimeError(f"cannot read card #{card_id}: {card.get('detail', card)}")
        thread = card.get("thread_uuid")
        if not thread:
            raise RuntimeError(f"card #{card_id} has no bound thread")
        owner = None
        if not card.get("assignee"):
            owner = _project_owner_orchestrator(gw, token, card.get("project_id"))
        to_node, _ = _thread_recipient(card, None, owner_orchestrator=owner)
        st, d = _post_json(f"{gw}/messages",
                           _card_say_payload(cell, to_node, kind, content, thread, opt_out=opt_out),
                           token)
        if not (st and 200 <= st < 300):
            raise RuntimeError(f"card post refused, HTTP {st or 'unreachable'}: {d.get('detail', d)}")
    return post


def _read(path: str, what: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {what} {path!r}: {exc}") from None
    return text


def _emit(obj: dict, as_json: bool, line: str) -> None:
    print(json.dumps(obj, indent=2) if as_json else line)


def _summary(st) -> dict:
    return {"run_id": st.run_id, "status": st.status.value, "jobs_used": st.jobs_used,
            "jobs_max": 10, "card_id": st.card_id, "error": st.error,
            "cut_queries": len(st.cut_queries),
            "ideation": st.ideation is not None, "research": st.research is not None,
            "architecture": st.architecture is not None,
            "commander_verdict": st.commander_verdict}


def run_skunkworks(
    argv: list[str],
    *,
    post_card: Optional[PostCard] = None,
    runners_factory=None,
    now=None,
    state_root: Optional[Path] = None,
    persona_dir: Optional[str] = None,
) -> int:
    try:  # an older swarph-mesh has no skunkworks: say so in one line
        from swarph_mesh import skunkworks as sk
        from swarph_mesh.skunkworks import artifacts, ceilings, service
    except ImportError:
        print(NEEDS_MESH, file=sys.stderr)
        return 1

    args = _build_parser().parse_args(argv)
    aj = args.json
    try:
        cell = _resolve_self_name(args.self_name)
    except RuntimeError as exc:
        print(f"swarph skunkworks: {exc}", file=sys.stderr)
        return 1
    rdir = ceilings.runs_dir_for(cell, state_root)

    if args.command == "run":
        return _run(args, cell, rdir, sk, service, ceilings, post_card, runners_factory,
                    now, state_root, persona_dir, aj)

    if not service.RUN_ID_RE.match(args.run_id):
        print(f"swarph skunkworks: bad run id {args.run_id!r}", file=sys.stderr)
        return 1
    run_path = rdir / args.run_id
    raw = artifacts.read_json(str(run_path / "state.json"))
    if not isinstance(raw, dict):
        print(f"swarph skunkworks: no run {args.run_id} for cell {cell} under {rdir}", file=sys.stderr)
        return 1
    if args.command == "status":
        service.sweep_dead_runs(rdir)  # a killed run shows as ERROR, not as still running
        raw = artifacts.read_json(str(run_path / "state.json")) or raw
    st = sk.RunState.from_dict(raw)

    if args.command == "status":
        sm = _summary(st)
        _emit(sm, aj, f"{sm['run_id']}  {sm['status']}  jobs {sm['jobs_used']}/{sm['jobs_max']}"
              + (f"  error: {_s(sm['error'])}" if sm["error"] else ""))
        return 0

    if args.command == "show":
        names = [args.stage] if args.stage else list(artifacts.RUN_FILES)
        out = {n: artifacts.read_json(str(run_path / artifacts.RUN_FILES[n])) for n in names}
        if all(v is None for v in out.values()):
            print(f"swarph skunkworks: nothing to show yet for {args.run_id} ({st.status.value})",
                  file=sys.stderr)
            return 1
        print(json.dumps(out if not args.stage else out[args.stage], indent=2))
        return 0

    if args.command == "stop":
        stop = run_path / "STOP"
        stop.write_text("stop requested\n")
        _emit({"run_id": args.run_id, "stop_file": str(stop)}, aj, f"wrote {stop}")
        return 0

    return _decide(args, cell, st, rdir, sk, post_card, aj)


def _run(args, cell, rdir, sk, service, ceilings, post_card, runners_factory, now,
         state_root, persona_dir, aj) -> int:
    try:
        seed = _read(args.seed_file, "seed file").strip()
        blocker = (_read(args.blocker_question_file, "blocker file").strip()
                   if args.blocker_question_file else None)
        ctx = [str(Path(p).resolve()) for p in args.context_file]
        for p in ctx:
            _read(p, "context file")
        if not seed:
            raise ValueError("the seed file is empty; a run needs a seed")
        request = sk.SkunkworksRequest(requesting_swarm=cell, persona=args.persona, seed=seed,
                                       card_id=args.card, blocker_question=blocker or None,
                                       context_md_paths=ctx)
        gw = args.gateway.rstrip("/")
        token = _resolve_token(cell, args.token_file)
    except (ValueError, RuntimeError) as exc:
        print(f"swarph skunkworks run: {exc}", file=sys.stderr)
        return 1

    post_card = post_card or _default_post_card(gw, token, cell, args.no_redact)
    if runners_factory is None:
        runners_factory = service.default_runners(
            gateway_url=gw, cell=cell,
            post=lambda url, body: _post_json(url, body, token),
            get=lambda url: _http_get_json(url, token))

    def post(kind: str, content: str) -> None:
        try:
            post_card(args.card, kind, content)
        except Exception as exc:  # the run goes on; the artifacts are on disk
            print(f"swarph skunkworks: card post failed: {_s(str(exc))}", file=sys.stderr)

    def on_stage(st, stage: str) -> None:
        post("status", json.dumps({"type": "skunkworks_status", "run_id": st.run_id,
                                   "stage": stage, "status": st.status.value,
                                   "jobs_used": st.jobs_used, "jobs_max": 10,
                                   "error": st.error}, sort_keys=True))
        path = rdir / st.run_id
        if stage == "ideation":
            post("fyi", _cut(f"skunkworks ideation, run {st.run_id}\n"
                             + json.dumps(st.ideation, indent=2), str(path / "ideation.json")))
        elif stage == "architecture":
            post("fyi", _cut(f"skunkworks architecture, run {st.run_id}\n"
                             + json.dumps(st.architecture, indent=2), str(path / "architecture.json")))

    kw = {"now": now} if now else {}
    try:
        st = service.start_run(request, runners_factory=runners_factory, state_root=state_root,
                               persona_dir=persona_dir, on_stage=on_stage, **kw)
    except ceilings.RunRefused as exc:
        print(f"swarph skunkworks run: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError) as exc:  # unknown persona
        print(f"swarph skunkworks run: {exc}", file=sys.stderr)
        return 1
    sm = _summary(st)
    _emit(sm, aj, f"{sm['run_id']}  {sm['status']}  jobs {sm['jobs_used']}/{sm['jobs_max']}"
          + (f"  error: {_s(sm['error'])}" if sm["error"] else ""))
    return 0 if sm["status"] == "awaiting_commander" else 1


def _decide(args, cell, st, rdir, sk, post_card, aj) -> int:
    try:
        quote = _read(args.quote_file, "quote file").strip()
    except ValueError as exc:
        print(f"swarph skunkworks decide: {exc}", file=sys.stderr)
        return 1
    if not quote:
        print("swarph skunkworks decide: the quote file is empty; decide records the "
              "commander's own words", file=sys.stderr)
        return 1
    res = sk.Pipeline(runs_dir=str(rdir)).approve(st, args.verdict, notes=quote)
    if "error" in res:  # the gate's refusal dict, or a bad verdict / wrong status
        print(json.dumps(res, indent=2))
        return 1
    try:
        gw = args.gateway.rstrip("/")
        post_card = post_card or _default_post_card(
            gw, _resolve_token(cell, args.token_file), cell, args.no_redact)
        post_card(st.card_id, "status",
                  f"skunkworks decision, run {st.run_id}: {res['status']} "
                  f"(architect verdict {res['arch_verdict_at_decision'] or 'MISSING'}).\n"
                  f"Commander's words, quoted from {args.quote_file}:\n{quote}")
    except Exception as exc:
        print(f"swarph skunkworks decide: recorded locally, card post failed: {_s(str(exc))}",
              file=sys.stderr)
        _emit(res, aj, f"{res['run_id']}  {res['status']}")
        return 2
    _emit(res, aj, f"{res['run_id']}  {res['status']}")
    return 0
