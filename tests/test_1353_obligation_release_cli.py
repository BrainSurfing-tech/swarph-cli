"""card #1353 — swarph board obligations release."""
from swarph_cli.commands import board


def test_release_is_a_command():
    try:
        args = board._build_parser().parse_args(
            ["obligations", "release", "3293", "--reason", "the laptop was shut down"])
    except SystemExit:
        args = None
    assert getattr(args, "group", None) == "obligations"
    assert getattr(args, "command", None) == "release"
    assert getattr(args, "id", None) == 3293
    assert getattr(args, "reason", None) == "the laptop was shut down"
