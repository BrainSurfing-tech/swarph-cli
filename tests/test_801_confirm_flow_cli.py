"""#801: `cards confirm-flow` — the runnable remedy for the confirmation gate — and the
graph render's provisional `default→peer` per missing step. CLI-side only: payload shape,
override parsing, renderer; the gateway owns the semantics."""
import pytest
from swarph_cli.commands import board


def test_payload_carries_actor_and_parsed_overrides():
    p = board._confirm_flow_payload("lab-ovh", ["plan-review=cursor-lin", "validate = cursor-win "], "w")
    assert p == {"actor": "lab-ovh", "holders": {"plan-review": "cursor-lin", "validate": "cursor-win"}, "what": "w"}
    assert board._confirm_flow_payload("lab-ovh", None) == {"actor": "lab-ovh"}


def test_a_malformed_override_is_refused_naming_the_form():
    with pytest.raises(ValueError, match="STEP=PEER"):
        board._confirm_flow_payload("lab-ovh", ["plan-review"])


def test_parser_accepts_confirm_flow_with_repeated_holders():
    parser = board._build_parser()
    ns = parser.parse_args(["cards", "confirm-flow", "801", "--holder", "build=drop-on-meta-edge",
                            "--holder", "validate=cursor-win", "--what", "x"])
    assert ns.id == 801 and ns.holder == ["build=drop-on-meta-edge", "validate=cursor-win"] and ns.what == "x"


def test_graph_render_shows_the_provisional_default_and_the_confirm_act():
    g = {"card_id": 801, "stage": "proposed", "stage_implied": "pre-build", "gate": {"mode": "warn"},
         "menu": {"source": "fleet", "version": 1}, "missing": ["build"],
         "flow": {"confirmed": False, "unconfirmed": ["build"], "defaults": {"build": "cursor-lin"}},
         "steps": [{"step": "build", "mandatory": True, "state": "missing", "delivery": "ref",
                    "needs": [], "eligible": ["cursor-lin"], "default": {"holder": "cursor-lin", "provisional": True}}]}
    out = board._format_graph(g)
    assert "flow: UNCONFIRMED (1 step(s) — swarph board cards confirm-flow 801)" in out
    assert "default→cursor-lin (provisional)" in out
    g["flow"] = {"confirmed": True, "unconfirmed": []}; g["steps"][0]["default"] = None
    assert "flow: confirmed" in board._format_graph(g) and "default→" not in board._format_graph(g)


def test_confirm_line_names_each_minted_row_and_an_empty_mint():
    d = {"card_id": 801, "by": "lab-ovh", "flow": {"confirmed": True},
         "minted": [{"step": "build", "id": 9, "holder": "cursor-lin", "state": "offered"}]}
    out = board._confirm_flow_line(d)
    assert out.splitlines()[0].startswith("card #801: flow CONFIRMED by lab-ovh — 1 row(s) minted")
    assert "  build -> #9 holder=cursor-lin state=offered" in out
    assert "nothing to mint" in board._confirm_flow_line({"card_id": 1, "by": "x", "flow": {"confirmed": True}, "minted": []})
