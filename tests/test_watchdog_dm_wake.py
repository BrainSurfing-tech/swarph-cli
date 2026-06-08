"""Tests for ``swarph watchdog --dm-wake`` (T1: flag + A1-DM cross-host wake).

Covers the additive ``--dm-wake`` argparse flag and the pure ``_dm_wake``
cross-host wake-DM send action. ``_dm_wake`` POSTs a mesh DM to a stranded
peer on another host; the peer's sidecar/inbox-watcher then wakes it.
"""

from __future__ import annotations

from unittest.mock import patch

from swarph_cli.commands.watchdog import (
    _DM_WAKE_PROMPT,
    _build_parser,
    _dm_wake,
)


# ---------------------------------------------------------------------------
# --dm-wake flag parses
# ---------------------------------------------------------------------------


def test_dm_wake_flag_parses_true():
    args = _build_parser().parse_args(["--check", "--dm-wake"])
    assert args.dm_wake is True


def test_dm_wake_flag_defaults_false():
    args = _build_parser().parse_args(["--check"])
    assert args.dm_wake is False


# ---------------------------------------------------------------------------
# _dm_wake — pure cross-host wake-DM send
# ---------------------------------------------------------------------------


def test_dm_wake_success_returns_true_and_posts_expected_body():
    with patch(
        "swarph_cli.commands.watchdog._post_json",
        return_value=(200, {"id": 1}),
    ) as mock_post:
        ok = _dm_wake(
            gateway="http://gw:8788",
            self_peer="lab-ovh",
            target_peer="gpu-wsl",
            token="tok",
            content=_DM_WAKE_PROMPT,
        )
    assert ok is True
    # _post_json(url, body, token, ...)
    args, kwargs = mock_post.call_args
    url = args[0]
    body = args[1]
    assert url == "http://gw:8788/messages"
    assert body["from_node"] == "lab-ovh"
    assert body["to_node"] == "gpu-wsl"
    assert body["kind"] == "fyi"
    assert isinstance(body["content"], str) and body["content"]


def test_dm_wake_strips_trailing_slash_on_gateway():
    with patch(
        "swarph_cli.commands.watchdog._post_json",
        return_value=(201, {"id": 2}),
    ) as mock_post:
        ok = _dm_wake(
            gateway="http://gw:8788/",
            self_peer="lab-ovh",
            target_peer="gpu-wsl",
            token="tok",
            content="wake",
        )
    assert ok is True
    assert mock_post.call_args[0][0] == "http://gw:8788/messages"


def test_dm_wake_non_2xx_returns_false():
    with patch(
        "swarph_cli.commands.watchdog._post_json",
        return_value=(500, {}),
    ):
        ok = _dm_wake(
            gateway="http://gw:8788",
            self_peer="lab-ovh",
            target_peer="gpu-wsl",
            token="tok",
            content="wake",
        )
    assert ok is False


def test_dm_wake_raise_returns_false_never_propagates():
    with patch(
        "swarph_cli.commands.watchdog._post_json",
        side_effect=RuntimeError("boom"),
    ):
        ok = _dm_wake(
            gateway="http://gw:8788",
            self_peer="lab-ovh",
            target_peer="gpu-wsl",
            token="tok",
            content="wake",
        )
    assert ok is False
