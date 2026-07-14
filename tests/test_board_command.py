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
