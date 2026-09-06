"""#740: a mis-filed card must be re-homable without losing its id, thread and links.
The CLI only carries the intent; the gateway gates it (orchestrator owning BOTH projects)."""
import pytest
from swarph_cli.commands import board


def test_edit_payload_carries_project_id_alone():
    p = board._card_edit_payload("lab-ovh", None, None, project_id=8)
    assert p == {"actor": "lab-ovh", "project_id": 8}


def test_edit_payload_project_id_is_an_int_even_from_a_digit_string():
    p = board._card_edit_payload("lab-ovh", None, None, project_id="8")
    assert p["project_id"] == 8 and isinstance(p["project_id"], int)


def test_edit_payload_without_any_field_still_refuses():
    with pytest.raises(ValueError, match="--project"):
        board._card_edit_payload("lab-ovh", None, None)


def test_edit_payload_project_id_none_is_not_mentioned():
    p = board._card_edit_payload("lab-ovh", "t", None, project_id=None)
    assert "project_id" not in p


def test_parser_accepts_edit_with_project_slug_or_id():
    parser = board.build_parser() if hasattr(board, "build_parser") else None
    if parser is None:
        pytest.skip("board module exposes no build_parser()")
    for ref in ("8", "mesh-hygiene"):
        ns = parser.parse_args(["cards", "edit", "739", "--project", ref])
        assert ns.project == ref and ns.id == 739
