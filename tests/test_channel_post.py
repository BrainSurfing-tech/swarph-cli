"""`swarph channel post` / `read` — post to and read a mesh channel.

The load-bearing invariant: a channel post sets ``channel`` and OMITS ``to_node``
(the gateway rejects a message carrying both — "exactly one of {to_node, channel}
is required"; lab hit that 422 announcing 0.28.0 by raw HTTP). This verb encodes
the rule so no one has to remember it.
"""
from swarph_cli.commands import channel


def test_post_payload_sets_channel_and_omits_to_node():
    p = channel._post_payload("lab-ovh", "releases", "fyi", "hello")
    assert p == {"from_node": "lab-ovh", "channel": "releases", "kind": "fyi", "content": "hello"}
    assert "to_node" not in p, "a channel post must NOT carry to_node (gateway 422s on both)"


def test_read_url_default_and_limit():
    assert channel._read_url("http://gw:8788", "releases", 20) == \
        "http://gw:8788/messages?channel=releases&limit=20"
    # channel name is url-encoded
    assert "channel=re%2Fpos" in channel._read_url("http://gw:8788", "re/pos", 5)


def test_format_channel_messages():
    payload = {"messages": [
        {"id": 4834, "from_node": "lab-ovh", "kind": "fyi", "content": "swarph-cli 0.28.0 shipped"},
        {"id": 4694, "from_node": "drop", "kind": "fyi", "content": "x" * 200},
    ]}
    out = channel._format_channel_messages(payload)
    assert "4834" in out and "lab-ovh" in out and "0.28.0" in out
    assert "..." in out, "long messages are truncated"


def test_format_channel_messages_empty():
    assert "no messages" in channel._format_channel_messages({"messages": []}).lower()


def test_format_channel_messages_strips_terminal_escapes():
    payload = {"messages": [{"id": 1, "from_node": "evil\x1b[2K", "kind": "fyi",
                             "content": "safe\x1b[1;31mINJECT\x1b[0m"}]}
    out = channel._format_channel_messages(payload)
    assert "\x1b" not in out, "no escape sequence reaches the terminal"
    assert "safe" in out
