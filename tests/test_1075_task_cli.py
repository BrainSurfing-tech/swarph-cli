"""card #1075 — the ask carries a task id, and withdraw is its own act."""
from swarph_cli.commands import board


def test_ask_payload_sends_task_and_omits_it_when_absent():
    p = board._ask_payload(
        "cursor-lin", "wire it", holder="me", step="build",
        task="task:pr-lab-orchestrator-160",
        task_needs=["task:pr-lab-orchestrator-159"])
    assert p["task"] == "task:pr-lab-orchestrator-160"
    assert p["task_needs"] == ["task:pr-lab-orchestrator-159"]
    assert p["holder"] == "cursor-lin"
    bare = board._ask_payload("cursor-lin", "wire it", step="build")
    assert "task" not in bare and "task_needs" not in bare


def test_withdraw_is_a_command():
    parser = board._build_parser()
    args = parser.parse_args(
        ["obligations", "withdraw", "1422", "--reason", "asker pulls it"])
    assert args.group == "obligations"
    assert args.command == "withdraw"
    assert args.id == 1422
    assert args.reason == "asker pulls it"
