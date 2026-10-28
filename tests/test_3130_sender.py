"""Card #1328: a class that differs from from_node is what the cell reads.

The wake line, the channel frame, and the channel read tag share one label.
A missing class leaves the from_node text unchanged.
"""
from swarph_cli.commands import channel
from swarph_cli.dm_frame import frame_text, sender_label
from swarph_cli.scripts.dm_notify_filter import _format_dm


def test_a_row_without_a_class_keeps_from_node():
    row = {"id": 1, "from_node": "drop", "kind": "fyi", "content": "hello"}
    assert sender_label(row) == "drop"
    assert "from drop," in frame_text(row)
    assert _format_dm(row) == "[MESH DM] id=1 from=drop kind=fyi | hello"


def test_a_different_class_is_named_with_the_claim():
    row = {
        "id": 2, "from_node": "drop", "kind": "fyi", "content": "hello",
        "credential_author": "shared", "claimed_from": "drop",
    }
    assert sender_label(row) == "shared, claiming drop"
    assert "from shared, claiming drop," in frame_text(row)
    assert _format_dm(row).startswith("[MESH DM] id=2 from=shared, claiming drop ")
    tagged = channel._format_channel_messages({"messages": [row]})
    assert "shared, claiming drop" in tagged
    assert tagged.startswith("[2] shared, claiming drop (fyi):")
