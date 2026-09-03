"""#591 — the CLI client for the card step graph (contract v0.4.1, plan #147
Task 9): `cards ask --step/--needs/--hours/--holder/--done`, `cards graph`,
`obligations take|decline|amend`, `close --outcome skipped`, and `cards move`
printing the gate's `warn`.

Pure-function tests in the repo's style: payload builders and formatters are
called directly; dispatch tests replace the HTTP seam with a fake that RECORDS
the request (url + body) and returns a canned (status, dict) — asserting on the
request, never on prose about it. Nothing here reaches a gateway.

What each block exists to catch:
- a flag argparse accepts but the dispatch drops (payload tests + dispatch tests);
- `--holder me` sent literally as "me" (the contract binds `me` to the token);
- `warn` — the only non-silent moment of a step-less ask / a warn-mode gate —
  swallowed by a formatter that reads only `id`/`stage` (#562's last-hop class);
- a refusal rendered as success (#319's class): the detail must reach stderr
  with a non-zero exit;
- the argparse traps the review measured: `--holder` sharing the positional's
  dest (clobbered to None when options precede "<what>"), an option BETWEEN
  the pre-#591 form's two positionals, a repeated `--needs` keeping only the
  last value, and the #532/#145 mint-time lines lost on every stepped ask.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import board


# ── _ask_payload ─────────────────────────────────────────────────────────────

def test_ask_payload_holder_me_resolves_to_self():
    p = board._ask_payload("cursor-lin", "fix it", holder="me", step="build",
                           done="https://github.com/x/y/pull/57")
    assert p == {"what": "fix it", "created_by": "cursor-lin", "kind": "action",
                 "holder": "cursor-lin", "step": "build",
                 "done": "https://github.com/x/y/pull/57"}
    assert p["holder"] != "me", "`me` is the token's peer, never the literal"


def test_ask_payload_no_holder_means_key_absent():
    p = board._ask_payload("cursor-lin", "validate it", step="validate")
    assert "holder" not in p, "absent → requested; a null on the wire is a different claim"
    for k in ("accept", "timeout_hours", "hours", "needs", "done"):
        assert k not in p, f"{k} not mentioned → not sent"


def test_ask_payload_needs_comma_list_and_hours():
    p = board._ask_payload("cursor-lin", "w", holder="droplet", step="validate",
                           needs="build, spec-review", hours=12)
    assert p["needs"] == ["build", "spec-review"]
    assert p["hours"] == 12 and p["holder"] == "droplet"


def test_step_list_flattens_repeats_commas_and_plus():
    """`action="append"` + comma lists + the contract's `+step` spelling — a
    plain store silently kept only the LAST --needs (review item 5/6)."""
    assert board._step_list(["build, spec-review", "+plan-review", ""]) == \
        ["build", "spec-review", "plan-review"]
    assert board._step_list("a,b") == ["a", "b"] and board._step_list(None) == []
    assert board._step_list(["-x"]) == ["-x"], "a remove keeps its sign for amend"


def test_ask_payload_keeps_pre_591_fields():
    p = board._ask_payload("lab-ovh", "w", holder="droplet", accept="PASS = a | FAIL = b",
                           timeout_hours=24, kind="question")
    assert p["accept"] == "PASS = a | FAIL = b" and p["timeout_hours"] == 24
    assert p["kind"] == "question"


# ── _ask_line: §2's strings per state ────────────────────────────────────────

def _resp(**kw):
    base = {"id": 91, "card_id": 20, "holder": None, "status": "open",
            "state": "requested", "taken_at": None, "timeout_at": None,
            "thread_uuid": "t", "accept": None, "step": "validate",
            "needs": ["build"], "menu_version": 3, "eligible": ["cursor-win", "drop"],
            "take_with": "swarph board obligations take 91"}
    base.update(kw)
    return base


def test_ask_line_requested_names_eligible_and_take_verb():
    lines = board._ask_line(_resp()).splitlines()
    assert lines[0].startswith("minted #91 requested (validate on #20)")
    assert "offered to: cursor-win, drop" in lines[0]
    assert "menu v3" in lines[0]
    assert lines[0].endswith("take with: swarph board obligations take 91")


def test_ask_line_offered():
    out = board._ask_line(_resp(state="offered", holder="cursor-win", eligible=None,
                                accept="PASS = a | FAIL = b"))
    assert out.splitlines() == [
        "minted #91 offered to cursor-win (validate on #20) — "
        "cursor-win takes with: swarph board obligations take 91",
        "  accept: PASS = a | FAIL = b"]


def test_ask_line_stepped_row_keeps_532_mint_time_line_but_not_145_clock():
    """The gateway sets `state` on EVERY ask since #591, so the `_format_ask`
    fallback never fires live; the NO ACCEPT CHECK line must ride the §2
    line or the #532 warning is gone from every ask (review item 7). A
    stepped row's clock is the menu's, so NO TIMEOUT stays off it."""
    out = board._ask_line(_resp(accept=None))
    assert "NO ACCEPT CHECK" in out and "--accept" in out
    assert "NO TIMEOUT" not in out and "thread t" not in out
    out = board._ask_line(_resp(accept="verify it works"))
    assert "NO FAIL BRANCH" in out and "NO ACCEPT CHECK" not in out


def test_ask_line_unstepped_row_keeps_every_pre_591_warning_and_warn_last():
    warn = "WARN: no --step — row #91 is `unstepped`: it buys no gate credit"
    out = board._ask_line(_resp(state="offered", holder="alice", eligible=None, step=None,
                                needs=None, menu_version=None, accept=None,
                                timeout_at=None, warn=warn))
    lines = out.splitlines()
    assert lines[0].startswith("minted #91 offered to alice (unstepped on #20)")
    assert "NO ACCEPT CHECK" in lines[1]
    assert lines[2].strip() == board._NO_TIMEOUT
    assert "thread t" in lines[3] and "swarph board obligations close 91" in lines[3]
    assert lines[-1] == warn
    assert "NO TIMEOUT" not in board._ask_line(
        _resp(state="offered", holder="alice", step=None, timeout_at="2026-09-04T00:00:00Z"))


def test_ask_line_open_points_at_close():
    out = board._ask_line(_resp(state="open", holder="cursor-win", eligible=None,
                                taken_at="2026-09-02T10:00:00Z", take_with=None))
    assert out.startswith("#91 open (cursor-win, validate on #20)")
    assert "swarph board obligations close 91 --outcome pass --evidence" in out


def test_ask_line_closed_pass_in_one_act():
    out = board._ask_line(_resp(id=90, state="closed:pass", status="closed", step="build",
                                holder="cursor-lin", eligible=None, take_with=None))
    assert out == "minted #90 build on #20 · closed:pass", (
        "a closed row has left the sweep — no NO ACCEPT CHECK / thread lines on it")


def test_ask_line_prints_warn_verbatim_on_its_own_line():
    warn = ("WARN: no --step — row #91 is `unstepped`: it buys no gate credit and "
            "never blocks; steps: build, validate, plan-review")
    out = board._ask_line(_resp(step=None, needs=None, menu_version=None, warn=warn))
    lines = out.splitlines()
    assert lines[-1] == warn
    assert "unstepped on #20" in lines[0]


def test_ask_line_without_state_falls_back_to_pre_591_rendering():
    """A response with no `state` is the pre-#591 gateway's shape; `_format_ask`
    must render it — not the §2 branch, which on a state-less row would say
    `minted #7 unstepped on #42 · None` and STILL carry NO TIMEOUT / NO ACCEPT
    CHECK (a substring assertion passed with the fallback unreachable —
    verification mutation M13). Pin the fallback's own first line."""
    d = {"id": 7, "card_id": 42, "holder": "droplet", "status": "open",
         "timeout_at": None, "thread_uuid": "t"}
    out = board._ask_line(d)
    assert out == board._format_ask(d)
    assert out.startswith("obligation #7 on card #42: droplet owes it, status=open, NO TIMEOUT")
    assert "minted" not in out and "None" not in out


def test_ask_line_renders_contract_fragments_only_when_the_gateway_sends_them():
    """§2's full lines carry `menu fleet vN`, `still missing on #20: …`,
    `ack by +24h`, `due …`, `(evidence: …) · unblocked: …` — none on the live
    b6a7bb1 wire, and the CLI derives nothing. Present → the contract's
    position; absent → today's line (pinned by the exact-match tests above),
    so this is the forward edge, not a change to what prints today."""
    out = board._ask_line(_resp(menu_source="fleet", still_missing=["plan-review"]))
    assert out.splitlines()[0] == (
        "minted #91 requested (validate on #20) · offered to: cursor-win, drop · "
        "menu fleet v3 · still missing on #20: plan-review — take with: "
        "swarph board obligations take 91")
    out = board._ask_line(_resp(state="offered", holder="cursor-win", eligible=None,
                                ack_by="+24h"))
    assert out.splitlines()[0] == (
        "minted #91 offered to cursor-win (validate on #20, ack by +24h) — "
        "cursor-win takes with: swarph board obligations take 91")
    out = board._ask_line(_resp(state="open", holder="cursor-win", eligible=None,
                                take_with=None, due="+24h after build closes"))
    assert out.splitlines()[0].startswith(
        "#91 open (cursor-win, validate on #20, due +24h after build closes) — close with:")
    out = board._ask_line(_resp(id=90, state="closed:pass", status="closed", step="build",
                                holder="cursor-lin", eligible=None, take_with=None,
                                evidence="pull/57", unblocked=["validate (requested)"],
                                still_missing=["plan-review"]))
    assert out == ("minted #90 build on #20 · closed:pass (evidence: pull/57) · "
                   "unblocked: validate (requested) · still missing: plan-review")
    bare = board._ask_line(_resp())
    assert "still missing" not in bare and "menu v3 —" in bare, "absent on the wire → absent"


# ── _format_graph ────────────────────────────────────────────────────────────

_STATES = [
    "missing", "missing (prior: fail by cursor-win at 2026-09-01T10:00:00Z)",
    "missing (stale pass by lab-ovh at T; upstream re-minted: #95)",
    "missing (skipped by drop, now mandatory: security label)",
    "unstaffable (eligible set empty)", "requested",
    "unstaffed (requested 30h, ack by 24h)", "offered (cursor-win)",
    "unstaffed (offered to cursor-win 30h, ack by 24h)", "open",
    "blocked (needs build #34, open)", "blocked (needs build — no row)",
    "closed:pass", "closed:pass (container unchecked)",
    "closed:pass (pre-v0.4, container unchecked)", "closed:fail",
    "closed:cannot_evaluate", "skipped (by drop: optional)", "not-taken",
]


def _graph():
    steps = []
    for i, st in enumerate(_STATES):
        steps.append({"step": f"s{i}", "mandatory": i % 2 == 0, "state": st,
                      "holder": None, "hours": 24, "delivery": "text", "evidence": None,
                      "row": None, "needs": [], "due": None, "retries": None,
                      "events": [], "eligible": None, "prior_rows": []})
    # the real shapes: a missing step with an eligible set, a blocked step with
    # one satisfied edge and one row-less edge, a closed step with a due
    steps[0]["eligible"] = ["cursor-win", "drop-on-meta-edge"]
    steps[10].update(holder="cursor-win", due="2026-09-03T10:00:00Z", needs=[
        {"step": "build", "row_id": 34, "satisfied": True, "state": "closed:pass"},
        {"step": "spec-review", "row_id": None, "satisfied": False, "state": None},
        {"step": "plan-review", "row_id": 35, "satisfied": False, "state": "open"},
    ])
    return {"card_id": 20, "stage": "build", "assignee": "cursor-lin",
            "menu": {"source": "fleet", "version": 3}, "menu_version": 3, "labels": [],
            "steps": steps,
            "unstepped": [{"id": 23, "holder": "cursor-lin", "status": "closed",
                           "close_outcome": "pass"},
                          {"id": 24, "holder": None, "status": "open", "close_outcome": None}],
            "missing": ["s0", "s2"], "stage_implied": "pre-build (graph unfilled)",
            "gate": {"mode": "warn", "flip_at": None}, "as_of": "2026-09-02T12:00:00Z"}


def test_format_graph_header_carries_stage_gate_menu_missing():
    head = board._format_graph(_graph()).splitlines()[0]
    assert head.startswith("card #20 stage=build implied=pre-build (graph unfilled)")
    assert "gate=warn flip_at=-" in head
    assert "menu fleet v3" in head and "missing: s0, s2" in head


def test_format_graph_renders_every_state_string():
    out = board._format_graph(_graph())
    for st in _STATES:
        assert st in out, f"state string not rendered: {st!r}"


def test_format_graph_mandatory_marker_and_eligible_only_when_present():
    lines = board._format_graph(_graph()).splitlines()
    assert lines[1].startswith("  s0 [M] missing holder=- due=- needs=- eligible=cursor-win, drop-on-meta-edge")
    assert lines[2].startswith("  s1 [opt] ")
    assert sum("eligible=" in ln for ln in lines) == 1, (
        "eligible is computed for missing steps only — printing `eligible=-` on a "
        "closed step would read as 'nobody may hold this'")


def test_format_graph_needs_render_ok_state_and_no_row():
    lines = board._format_graph(_graph()).splitlines()
    blocked = next(ln for ln in lines if ln.startswith("  s10 "))
    assert "holder=cursor-win due=2026-09-03T10:00:00Z" in blocked
    assert "needs=build #34 ok, spec-review — no row, plan-review #35 open" in blocked


def test_format_graph_unstepped_section_only_when_non_empty():
    out = board._format_graph(_graph())
    lines = out.splitlines()
    i = lines.index("unstepped:")
    assert lines[i + 1] == "  #23 holder=cursor-lin status=closed outcome=pass"
    assert lines[i + 2] == "  #24 holder=- status=open"
    g = _graph(); g["unstepped"] = []
    assert "unstepped:" not in board._format_graph(g)


def test_format_graph_strips_terminal_escapes_from_peer_strings():
    g = _graph()
    g["steps"][0]["state"] = "missing\x1b[31m"
    g["unstepped"][0]["holder"] = "evil\x1b]0;x\x07"
    out = board._format_graph(g)
    assert "\x1b" not in out and "\x07" not in out


# ── _move_line / _amend_payload / _obligation_act_line ───────────────────────

def test_move_line_with_and_without_warn():
    assert board._move_line({"id": 20, "stage": "build"}) == "card #20 -> build"
    warn = "WARN: build on #20 is missing validate, plan-review; gate flips 2026-09-15"
    out = board._move_line({"id": 20, "stage": "build", "warn": warn})
    assert out.splitlines() == ["card #20 -> build", warn]


def test_amend_payload_empty_holder_is_a_real_value():
    assert board._amend_payload(holder="") == {"holder": ""}, "'' clears → requested"
    assert board._amend_payload(step="build", needs_add=["validate"]) == \
        {"step": "build", "needs_add": ["validate"]}
    assert "holder" not in board._amend_payload(hours=48)


def test_amend_payload_refuses_the_empty_patch():
    with pytest.raises(ValueError, match="nothing to amend"):
        board._amend_payload()
    with pytest.raises(ValueError, match="nothing to amend"):
        board._amend_payload(needs_add=[""], needs=[])


def test_amend_payload_routes_signed_needs_and_flattens_commas():
    """Contract §2 spells edges `--needs +step|-step`; `--needs-add a,b` used to
    send ["a,b"] verbatim (review item 5/6)."""
    p = board._amend_payload(needs=["+validate", "-spec-review"], needs_add=["a,b"],
                             needs_remove=["c"])
    assert p == {"needs_add": ["a", "b", "validate"], "needs_remove": ["c", "spec-review"]}


def test_obligation_act_line_take():
    out = board._obligation_act_line({
        "id": 91, "card_id": 20, "holder": "cursor-win", "taken_at": "T",
        "step": "validate", "state": "blocked (needs build #90, open)", "due": None,
        "close_with": 'swarph board obligations close 91 --outcome pass --evidence "<container>"'})
    lines = out.splitlines()
    assert lines[0] == "#91 blocked (needs build #90, open) (cursor-win, validate on #20) · due: -"
    assert lines[1] == '  close with: swarph board obligations close 91 --outcome pass --evidence "<container>"'


def test_obligation_act_line_decline_names_remaining_and_unstaffable():
    out = board._obligation_act_line({"id": 91, "declined_by": "drop",
                                      "remaining_eligible": ["cursor-win"],
                                      "state": "requested", "unstaffable": False})
    assert out == "#91 declined by drop · remaining eligible: cursor-win · state: requested"
    out = board._obligation_act_line({"id": 91, "declined_by": "drop", "remaining_eligible": [],
                                      "state": "unstaffable (declined by all)",
                                      "unstaffable": True})
    assert "remaining eligible: (nobody)" in out and "UNSTAFFABLE" in out


def test_obligation_act_line_amend_shows_was():
    out = board._obligation_act_line({"id": 23, "amended": ["step", "holder"],
                                      "was": {"step": None, "holder": "ws-lc"},
                                      "state": "offered (workstation-lc)", "due": None})
    assert out == ("#23 amended: step (was: none), holder (was: ws-lc) · "
                   "state: offered (workstation-lc) · due: -")


# ── dispatch: the request on the wire + refusal passthrough ──────────────────

def _wire(monkeypatch, status=200, payload=None):
    """Identity in, HTTP out. Records every call as (method, url, body)."""
    monkeypatch.setattr(board, "_resolve_self_name", lambda *_a, **_k: "cursor-lin")
    monkeypatch.setattr(board, "_resolve_token", lambda *_a, **_k: "tok")
    calls = []

    def post(url, body, token):
        calls.append(("POST", url, body)); return status, dict(payload or {"id": 1})

    def patch(url, body, token):
        calls.append(("PATCH", url, body)); return status, dict(payload or {"id": 1})

    def get(url, token):
        calls.append(("GET", url, None)); return status, dict(payload or {"id": 1})

    monkeypatch.setattr(board, "_post_json", post)
    monkeypatch.setattr(board, "_patch_json", patch)
    monkeypatch.setattr(board, "_http_get_json", get)
    return calls


def test_ask_dispatch_holder_me_step_done(monkeypatch, capsys):
    """Options BEFORE "<what>": the ordering where a `--holder` sharing the
    positional's dest parses holder=None (the `?` positional's empty match
    overwrites the option) — `dest="holder_flag"` is what this pins."""
    calls = _wire(monkeypatch, payload=_resp(id=90, state="closed:pass", step="build",
                                             holder="cursor-lin", take_with=None))
    rc = board.run_board(["cards", "ask", "20", "--step", "build", "--holder", "me",
                          "--done", "https://x/pull/57", "--needs", "spec-review",
                          "--needs", "+plan-review", "--hours", "96", "--kind", "question",
                          "fix + test", "--gateway", "http://gw"])
    assert rc == 0
    assert calls == [("POST", "http://gw/board/cards/20/ask",
                      {"what": "fix + test", "created_by": "cursor-lin", "kind": "question",
                       "holder": "cursor-lin", "step": "build",
                       "needs": ["spec-review", "plan-review"],
                       "hours": 96, "done": "https://x/pull/57"})]
    assert capsys.readouterr().out.strip() == "minted #90 build on #20 · closed:pass"


def test_ask_parser_holder_flag_survives_every_ordering():
    for argv in (["cards", "ask", "20", "--holder", "me", "w"],
                 ["cards", "ask", "--holder", "me", "20", "w"],
                 ["cards", "ask", "20", "w", "--holder", "me"]):
        ns = board._build_parser().parse_args(argv)
        assert (ns.holder_flag, ns.holder, ns.what) == ("me", None, "w"), argv


def test_ask_dispatch_option_between_positionals_keeps_pre_591_form(monkeypatch):
    """`ask <id> <holder> --accept "…" "<what>"` worked before #591; with the
    positional holder optional, chunked parsing read holder=None what=<holder>
    and refused "<what>" as unrecognized (review, blocking)."""
    calls = _wire(monkeypatch, payload=_resp(state="offered", holder="lab-ovh"))
    rc = board.run_board(["cards", "ask", "20", "lab-ovh", "--accept", "PASS = x | FAIL = y",
                          "the what"])
    assert rc == 0
    body = calls[0][2]
    assert (body["holder"], body["what"], body["accept"]) == \
        ("lab-ovh", "the what", "PASS = x | FAIL = y")


def test_ask_dispatch_no_holder_no_step_is_refused_locally(monkeypatch, capsys):
    """The same form with "<what>" forgotten parses what=<holder>; before #591
    argparse refused it (what required). It must not reach the wire as a
    holder-less mint whose text is a peer name."""
    calls = _wire(monkeypatch)
    rc = board.run_board(["cards", "ask", "20", "lab-ovh", "--accept", "PASS = x | FAIL = y"])
    assert rc == 2 and not calls
    err = capsys.readouterr().err
    assert "--step" in err and "--holder" in err and "'lab-ovh'" in err


def test_ask_parser_refuses_three_positionals():
    with pytest.raises(SystemExit):
        board._build_parser().parse_args(["cards", "ask", "20", "a", "b", "c"])


def test_ask_dispatch_positional_holder_still_parses(monkeypatch):
    """GUIDE.md/README/test_307 grammar: `ask <id> <holder> "<what>"`."""
    calls = _wire(monkeypatch, payload=_resp(state="offered", holder="droplet"))
    rc = board.run_board(["cards", "ask", "42", "droplet", "please review", "--step", "validate"])
    assert rc == 0
    assert calls[0][2]["holder"] == "droplet" and calls[0][2]["what"] == "please review"


def test_ask_dispatch_holder_twice_is_refused_locally(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc = board.run_board(["cards", "ask", "42", "droplet", "w", "--holder", "me"])
    assert rc == 2 and not calls
    assert "holder given twice" in capsys.readouterr().err


def test_ask_dispatch_refusal_prints_detail_verbatim(monkeypatch, capsys):
    detail = ("holder ws-lc is not eligible for validate on #20 — eligible: cursor-win, "
              "drop; ask with: swarph board cards ask 20 \"w\" --step validate --holder cursor-win")
    _wire(monkeypatch, status=403, payload={"detail": detail})
    rc = board.run_board(["cards", "ask", "20", "w", "--step", "validate", "--holder", "ws-lc"])
    assert rc == 1
    err = capsys.readouterr().err
    assert detail in err and "403" in err


def test_graph_dispatch_gets_the_graph_endpoint(monkeypatch, capsys):
    calls = _wire(monkeypatch, payload=_graph())
    rc = board.run_board(["cards", "graph", "20", "--gateway", "http://gw"])
    assert rc == 0
    assert calls == [("GET", "http://gw/board/cards/20/graph", None)]
    assert capsys.readouterr().out.startswith("card #20 stage=build")


def test_graph_dispatch_json_dumps_raw(monkeypatch, capsys):
    _wire(monkeypatch, payload=_graph())
    assert board.run_board(["cards", "graph", "20", "--json"]) == 0
    import json
    assert json.loads(capsys.readouterr().out)["stage_implied"] == "pre-build (graph unfilled)"


def test_take_dispatch_posts_empty_body(monkeypatch, capsys):
    calls = _wire(monkeypatch, payload={"id": 91, "card_id": 20, "holder": "cursor-lin",
                                        "taken_at": "T", "step": "validate", "state": "open",
                                        "due": "2026-09-03T10:00:00Z", "close_with": "swarph board obligations close 91 --outcome pass --evidence \"<container>\""})
    rc = board.run_board(["obligations", "take", "91", "--gateway", "http://gw"])
    assert rc == 0
    assert calls == [("POST", "http://gw/board/obligations/91/take", {})]
    out = capsys.readouterr().out
    assert "#91 open (cursor-lin, validate on #20) · due: 2026-09-03T10:00:00Z" in out
    assert "close with: swarph board obligations close 91" in out


def test_take_dispatch_409_propagates(monkeypatch, capsys):
    _wire(monkeypatch, status=409, payload={"detail": "obligation 91 already taken by cursor-win"})
    assert board.run_board(["obligations", "take", "91"]) == 1
    assert "already taken by cursor-win" in capsys.readouterr().err


def test_decline_dispatch_sends_why(monkeypatch, capsys):
    calls = _wire(monkeypatch, payload={"id": 91, "declined_by": "cursor-lin",
                                        "remaining_eligible": ["drop"], "state": "requested",
                                        "unstaffable": False})
    rc = board.run_board(["obligations", "decline", "91", "--why", "on another card",
                          "--gateway", "http://gw"])
    assert rc == 0
    assert calls == [("POST", "http://gw/board/obligations/91/decline", {"why": "on another card"})]
    assert "remaining eligible: drop" in capsys.readouterr().out


def test_amend_dispatch_patches_and_clears_holder(monkeypatch, capsys):
    calls = _wire(monkeypatch, payload={"id": 23, "amended": ["holder", "needs_extra"],
                                        "was": {"holder": "ws-lc"}, "state": "requested",
                                        "due": None})
    rc = board.run_board(["obligations", "amend", "23", "--holder", "", "--needs-add", "build",
                          "--needs-remove", "spec-review", "--step", "validate",
                          "--accept", "PASS = x | FAIL = y", "--hours", "48",
                          "--needs", "+plan-review", "--needs=-second-seat",
                          "--gateway", "http://gw"])
    assert rc == 0
    assert calls == [("PATCH", "http://gw/board/obligations/23/amend",
                      {"holder": "", "step": "validate", "accept": "PASS = x | FAIL = y",
                       "hours": 48, "needs_add": ["build", "plan-review"],
                       "needs_remove": ["spec-review", "second-seat"]})]
    assert "holder (was: ws-lc)" in capsys.readouterr().out


def test_amend_dispatch_empty_patch_never_touches_the_wire(monkeypatch, capsys):
    calls = _wire(monkeypatch)
    rc = board.run_board(["obligations", "amend", "23"])
    assert rc == 2 and not calls
    assert "nothing to amend" in capsys.readouterr().err


def test_amend_dispatch_409_due_earlier_propagates(monkeypatch, capsys):
    _wire(monkeypatch, status=409, payload={"detail": "would move #23 due EARLIER (was T1, now T0)"})
    assert board.run_board(["obligations", "amend", "23", "--hours", "1"]) == 1
    assert "due EARLIER" in capsys.readouterr().err


def test_close_accepts_skipped(monkeypatch):
    calls = _wire(monkeypatch, payload={"id": 5, "status": "closed", "closed_by": "cursor-lin",
                                        "close_outcome": "skipped"})
    rc = board.run_board(["obligations", "close", "5", "--outcome", "skipped",
                          "--evidence", "optional step, not needed on this card"])
    assert rc == 0
    assert calls[0][2]["outcome"] == "skipped"


def test_move_dispatch_prints_gate_warn(monkeypatch, capsys):
    warn = "WARN: #20 -> build with validate, plan-review missing; gate flips 2026-09-15"
    _wire(monkeypatch, payload={"id": 20, "stage": "build", "warn": warn})
    assert board.run_board(["cards", "move", "20", "build"]) == 0
    assert capsys.readouterr().out.splitlines() == ["card #20 -> build", warn]


def test_warn_lines_are_terminal_sanitized_on_ask_and_move():
    """The verify lens's surviving mutation: `warn` is gateway text that can carry
    peer/step names; it must go through the same escape stripping as every
    other peer-authored string."""
    out = board._ask_line({"id": 1, "card_id": 2, "state": "requested", "step": "build",
                           "eligible": ["a"], "menu_version": 1, "take_with": "t",
                           "warn": "WARN: x\x1b[31mred"})
    assert "WARN: x" in out and "\x1b" not in out
    out = board._move_line({"id": 2, "stage": "build", "warn": "WARN: y\x1b[0m"})
    assert "WARN: y" in out and "\x1b" not in out


def test_empty_needs_sends_no_key_and_still_missing_is_read():
    assert "needs" not in board._ask_payload("me-peer", "w", step="build", needs="")
    assert "needs" not in board._ask_payload("me-peer", "w", step="build", needs=[",,"])
    out = board._ask_line({"id": 9, "card_id": 20, "state": "requested", "step": "validate",
                           "eligible": ["cursor-win", "drop-on-meta-edge"], "menu_version": 3,
                           "menu_source": "fleet", "still_missing": ["plan-review"],
                           "take_with": "swarph board obligations take 9"})
    assert out.startswith("minted #9 requested (validate on #20) · offered to: cursor-win, drop-on-meta-edge · menu fleet v3 · still missing on #20: plan-review — take with: swarph board obligations take 9")
