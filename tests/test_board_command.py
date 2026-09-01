"""`swarph board` — CLI wrappers over the mesh-gateway board endpoints.

Pure helpers (URL/query builders, payload builders, link-merge, formatters) are
unit-tested here; the HTTP calls are the injectable seam (reused from mesh.py).
Contract from the live gateway OpenAPI:
  GET  /board/projects                       list
  POST /board/projects  {slug,title,goal,actor}
  GET  /board/cards      ?project&stage&assignee
  POST /board/cards      {project_id,title,body,ai2,priority,actor}   (NO stage — defaults proposed)
  GET  /board/cards/{id}
  PATCH /board/cards/{id} {stage,assignee,links,actor}
"""
from swarph_cli.commands import board


def test_cards_list_url_no_filters():
    assert board._cards_list_url("http://gw:8788") == "http://gw:8788/board/cards"


def test_cards_list_url_with_filters():
    url = board._cards_list_url("http://gw:8788", project="board", stage="idea", assignee="lab-ovh")
    assert url.startswith("http://gw:8788/board/cards?")
    # order-independent query assertion
    q = url.split("?", 1)[1].split("&")
    assert "project=board" in q and "stage=idea" in q and "assignee=lab-ovh" in q


def test_card_add_payload_defaults_and_actor():
    p = board._card_add_payload("lab-ovh", 8, "Title", body=None, ai2=False, priority=0)
    assert p == {"actor": "lab-ovh", "project_id": 8, "title": "Title", "ai2": False, "priority": 0}
    assert "stage" not in p, "create has no stage field — the gateway defaults it to proposed"
    assert "body" not in p, "empty body is omitted, not sent as null"


def test_card_add_payload_with_body():
    p = board._card_add_payload("lab-ovh", 8, "T", body="hello", ai2=True, priority=3)
    assert p["body"] == "hello" and p["ai2"] is True and p["priority"] == 3


def test_card_add_payload_with_due():
    p = board._card_add_payload("cursor-lin", 8, "T", due_at="2026-09-07")
    assert p["due_at"] == "2026-09-07T00:00:00"
    p2 = board._card_add_payload("cursor-lin", 8, "T", due_at="2026-09-07T12:00:00Z")
    assert p2["due_at"] == "2026-09-07T12:00:00Z"
    assert "due_at" not in board._card_add_payload("cursor-lin", 8, "T")


def test_project_add_payload():
    assert board._project_add_payload("lab-ovh", "fed-brain", "Fed Brain", goal="g") == \
        {"actor": "lab-ovh", "slug": "fed-brain", "title": "Fed Brain", "goal": "g"}
    p = board._project_add_payload("lab-ovh", "x", "X", goal=None)
    assert "goal" not in p, "empty goal omitted"


def test_merge_link_adds_without_clobbering():
    existing = {"pr": "#1", "spec": "s.md"}
    merged = board._merge_link(existing, "deploy", "live")
    assert merged == {"pr": "#1", "spec": "s.md", "deploy": "live"}
    assert existing == {"pr": "#1", "spec": "s.md"}, "source not mutated"


def test_merge_link_from_none():
    assert board._merge_link(None, "k", "v") == {"k": "v"}


def test_format_cards_columns():
    data = {"cards": [
        {"id": 36, "stage": "build", "project_id": 6, "ai2": False, "title": "board CLI"},
        {"id": 33, "stage": "idea", "project_id": 9, "ai2": True, "title": "benchmark"},
    ]}
    out = board._format_cards(data)
    assert "36" in out and "build" in out and "board CLI" in out
    assert "33" in out and "idea" in out
    assert not out.startswith("due:"), "no due block → no due header"


def test_format_cards_due_header_overdue_first():
    """#145: list response already carries `due` + per-card due_state.
    Render at the head with relative age; overdue before today/upcoming.
    Built from the LIST payload only (#661)."""
    data = {
        "due": {"overdue": 2, "today": 1, "upcoming": 1, "undated": 3,
                "unparseable": 0, "as_of": "2026-09-01T04:04:21Z"},
        "cards": [
            {"id": 10, "stage": "plan", "project_id": 1, "ai2": False,
             "title": "undated one", "due_state": None, "days_until": None},
            {"id": 20, "stage": "plan", "project_id": 1, "ai2": False,
             "title": "soon", "due_state": "upcoming", "days_until": 7},
            {"id": 30, "stage": "plan", "project_id": 1, "ai2": False,
             "title": "graduation for xxxx", "due_state": "overdue",
             "days_until": -2},
            {"id": 40, "stage": "spec", "project_id": 1, "ai2": False,
             "title": "today item", "due_state": "today", "days_until": 0},
            {"id": 50, "stage": "plan", "project_id": 1, "ai2": False,
             "title": "older overdue", "due_state": "overdue",
             "days_until": -21},
        ],
    }
    out = board._format_cards(data)
    assert out.startswith("due:")
    assert "overdue=2" in out and "today=1" in out and "upcoming=1" in out
    assert "21 days ago" in out and "2 days ago" in out
    assert "today" in out.splitlines()[3] or "today" in out  # due_state column
    # overdue first among dated rows, most overdue before less overdue
    dated_lines = [ln for ln in out.splitlines() if ln.startswith("  #")]
    assert dated_lines[0].startswith("  #50"), dated_lines
    assert "21 days ago" in dated_lines[0]
    assert dated_lines[1].startswith("  #30")
    assert dated_lines[2].startswith("  #40")  # today before upcoming
    assert dated_lines[3].startswith("  #20")
    # table still present below
    assert "ID" in out and "graduation for xxxx" in out


def test_format_cards_due_counts_without_dated_rows_is_loud():
    """Filtered page can show due counts with zero dated rows in `cards`.
    Silent empty would re-open the #145 lie."""
    data = {"due": {"overdue": 3, "today": 0, "upcoming": 0, "undated": 10},
            "cards": [{"id": 1, "stage": "plan", "project_id": 1, "ai2": False,
                       "title": "x", "due_state": None}]}
    out = board._format_cards(data)
    assert "dated cards not in this page" in out


def test_rel_due_spells_relative_age():
    assert board._rel_due(-21) == "21 days ago"
    assert board._rel_due(-1) == "1 day ago"
    assert board._rel_due(0) == "today"
    assert board._rel_due(1) == "in 1 day"
    assert board._rel_due(7) == "in 7 days"


def test_format_card_detail_shows_links():
    card = {"id": 36, "stage": "build", "project_id": 6, "title": "T",
            "assignee": "lab-ovh", "ai2": False, "links": {"pr": "#113"}, "body": "B"}
    out = board._format_card(card)
    assert "36" in out and "build" in out and "lab-ovh" in out and "pr" in out and "#113" in out


def test_format_projects():
    data = [{"id": 9, "slug": "federation-brain", "title": "Fed Brain"}]
    out = board._format_projects(data)
    assert "9" in out and "federation-brain" in out


def test_project_ref_to_id_passthrough_and_slug():
    projects = [{"id": 9, "slug": "federation-brain"}, {"id": 6, "slug": "board"}]
    assert board._project_ref_to_id("9", projects) == 9        # numeric passthrough
    assert board._project_ref_to_id(9, projects) == 9
    assert board._project_ref_to_id("federation-brain", projects) == 9  # slug lookup
    assert board._project_ref_to_id("nope", projects) is None  # unknown slug
    assert board._project_ref_to_id(None, projects) is None


def test_format_card_strips_terminal_escapes():
    card = {"id": 1, "stage": "idea", "project_id": 9, "title": "t\x1b[2Ktitle",
            "body": "b\x1b[31mody", "links": {"k\x1b[0m": "v\x1b[1m"}}
    out = board._format_card(card)
    assert "\x1b" not in out, "peer-authored card content can't inject terminal escapes"


# ── #191: cards edit — the body is no longer write-once ─────────────────────

def test_card_edit_payload_title_only():
    p = board._card_edit_payload("cursor-lin", "new title", None)
    assert p == {"actor": "cursor-lin", "title": "new title"}
    assert "body" not in p, "None means NOT MENTIONED — the gateway must not "
    "see a body key at all, or an unrelated title edit would clear the body"


def test_card_edit_payload_body_only():
    p = board._card_edit_payload("cursor-lin", None, "corrected body")
    assert p == {"actor": "cursor-lin", "body": "corrected body"}


def test_card_edit_payload_empty_string_body_is_a_real_value():
    p = board._card_edit_payload("cursor-lin", None, "")
    assert p["body"] == "", "'' CLEARS — it must survive the builder; only "
    "None means 'not mentioned' (the null-means-two-things trap)"


def test_card_edit_payload_refuses_the_empty_patch():
    import pytest
    with pytest.raises(ValueError, match="nothing to edit"):
        board._card_edit_payload("cursor-lin", None, None)


def test_card_edit_payload_due_only():
    p = board._card_edit_payload("cursor-lin", None, None, due_at="2026-09-14")
    assert p == {"actor": "cursor-lin", "due_at": "2026-09-14T00:00:00"}


def test_card_edit_payload_clear_due():
    p = board._card_edit_payload("cursor-lin", None, None, due_at="")
    assert p["due_at"] is None


def _run_edit(monkeypatch, argv, patch_impl):
    monkeypatch.setattr(board, "_resolve_self_name", lambda *_a, **_k: "cursor-lin")
    monkeypatch.setattr(board, "_resolve_token", lambda *_a, **_k: "tok")
    monkeypatch.setattr(board, "_patch_json", patch_impl)
    return board.run_board(argv)


def test_edit_dispatch_sends_title_and_body(monkeypatch):
    sent = {}
    def fake_patch(url, body, token, **k):
        sent.update(url=url, body=body, token=token)
        return 200, {"id": 125, "title": body.get("title"), "body_version": 3}
    rc = _run_edit(monkeypatch,
                   ["cards", "edit", "125", "--title", "T2", "--body", "B2"],
                   fake_patch)
    assert rc == 0
    assert sent["url"].endswith("/board/cards/125")
    assert sent["body"] == {"actor": "cursor-lin", "title": "T2", "body": "B2"}


def test_edit_dispatch_with_neither_field_never_touches_the_wire(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("an empty edit must not become a PATCH — a 200 "
                             "that changes nothing reads as 'correction landed'")
    rc = _run_edit(monkeypatch, ["cards", "edit", "125"], boom)
    assert rc == 2
    assert "nothing to edit" in capsys.readouterr().err


def test_edit_dispatch_surfaces_the_gateway_refusal(monkeypatch, capsys):
    rc = _run_edit(monkeypatch, ["cards", "edit", "125", "--title", "T"],
                   lambda *a, **k: (403, {"detail": "orchestrator, the assignee "
                                          "with an execute grant, ..."}))
    assert rc == 1
    assert "execute grant" in capsys.readouterr().err


# ── #590: obligations list — the read half of the ledger ────────────────────

def test_obligations_list_url_filters():
    u = board._obligations_list_url("http://gw:8788", status="open",
                                    holder="ws-lc", card_id=307, overdue=True)
    assert u.startswith("http://gw:8788/board/obligations?")
    for part in ("status=open", "holder=ws-lc", "card_id=307", "overdue=true"):
        assert part in u


def test_obligations_list_url_bare_when_unfiltered():
    assert board._obligations_list_url("http://gw:8788") == \
        "http://gw:8788/board/obligations"
    assert "overdue" not in board._obligations_list_url(
        "http://gw:8788", overdue=False)


def test_format_obligations_marks_overdue_unclosable_and_close_outcome():
    out = board._format_obligations({"as_of": "2026-08-26T07:00:00Z", "obligations": [
        {"id": 25, "card_id": 532, "holder": "lab-ovh", "status": "open",
         "kind": "action", "overdue": True, "accept_state": "missing",
         "unclosable_reason": None},
        {"id": 26, "card_id": 307, "holder": "ghost", "status": "open",
         "kind": "action", "overdue": False, "accept_state": "fail-branch-detected",
         "unclosable_reason": "holder-not-a-known-peer"},
        {"id": 27, "card_id": 510, "holder": "ws-lc", "status": "closed",
         "kind": "action", "overdue": False, "accept_state": "fail-branch-detected",
         "unclosable_reason": None, "close_outcome": "pass"},
    ]})
    assert "#25" in out and "OVERDUE" in out and "accept:missing" in out
    assert "UNCLOSABLE:holder-not-a-known-peer" in out
    assert "#27" in out and "outcome:pass" in out
    assert "as_of 2026-08-26T07:00:00Z" in out


def test_format_obligations_empty_state_names_the_causes():
    out = board._format_obligations({"obligations": [], "as_of": "2026-08-26T07:00:00Z"})
    assert "none exist" in out and "filter" in out and "read" in out
    assert "2026-08-26T07:00:00Z" in out, "an empty readout without its instant "
    "claims more than it measured"


def test_obligations_list_dispatch(monkeypatch):
    sent = {}
    monkeypatch.setattr(board, "_resolve_self_name", lambda *_a, **_k: "cursor-lin")
    monkeypatch.setattr(board, "_resolve_token", lambda *_a, **_k: "tok")
    def fake_get(url, tok, **k):
        sent["url"] = url
        return 200, {"obligations": [], "as_of": "2026-08-26T07:00:00Z"}
    monkeypatch.setattr(board, "_http_get_json", fake_get)
    rc = board.run_board(["obligations", "list", "--status", "open",
                          "--holder", "ws-lc", "--overdue"])
    assert rc == 0
    assert "status=open" in sent["url"] and "holder=ws-lc" in sent["url"]
    assert "overdue=true" in sent["url"]
