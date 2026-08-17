"""Card #125 / #194 — pending_channel_posts must honour the polling cell's OWN wake_policy.

The monitor's pending list is a NOTIFY surface: it is what `monitor status` shows and
what a wake is built from. So it must respect what the cell asked for. (The read path
-- `swarph channel read` / GET /messages -- stays unfiltered by design: asking to not
be WOKEN is not asking to be denied READ access.)

The fail-direction is the whole design, so most of these tests are about it: an
unknown policy, a malformed mentions blob, or a gateway that never sends wake_policy
must all ADMIT the post. Failing closed would drop channel posts silently, which is
card #125's original defect rather than a safe default.
"""
from __future__ import annotations

import pytest

from swarph_cli.commands import mesh


class _State:
    def __init__(self, tmp_path, self_name="cellA"):
        self.gateway = "http://gw.invalid:8788"
        self.token = "t"
        self.self_name = self_name
        self.log_prefix = "[monitor]"
        self.channel_cursors = {}
        self.pending_channel_posts = []
        self.cursor = {}
        self.cursor_path = tmp_path / "cursor.json"


def _wire(monkeypatch, channels, messages):
    """Fake the two gateway calls; record which channels were actually fetched."""
    fetched = []

    def _get(url, token, *a, **kw):
        if "/channels?" in url:
            return 200, {"channels": channels}
        chan = url.split("channel=")[1].split("&")[0]
        fetched.append(chan)
        return 200, {"messages": messages.get(chan, [])}

    monkeypatch.setattr(mesh, "_http_get_json", _get)
    monkeypatch.setattr(mesh, "_write_cursor_atomic", lambda *a, **kw: None)
    return fetched


# ── the predicate itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize("policy", ["all", None, "some-future-value", ""])
def test_non_mentions_policies_admit_everything(policy):
    """Only mentions_only filters. Everything else — including a value this build
    has never heard of — admits, because a policy we cannot interpret must not be
    interpreted as silence."""
    assert mesh._wake_policy_admits(policy, {"mentions": "[]"}, "cellA") is True


def test_mentions_only_admits_when_named():
    assert mesh._wake_policy_admits("mentions_only", {"mentions": '["cellA"]'}, "cellA") is True


def test_mentions_only_EXCLUDES_when_not_named():
    """NON-VACUITY: without this, every assertion above is satisfied by a predicate
    that just returns True."""
    assert mesh._wake_policy_admits("mentions_only", {"mentions": '["other"]'}, "cellA") is False


def test_mentions_arrives_as_a_JSON_STRING_not_a_list():
    """Measured against the live gateway: mentions is '[]', a STRING. A build that
    assumed a list would treat every post as unmentioned and silently mute the cell."""
    assert mesh._wake_policy_admits("mentions_only", {"mentions": '["cellA","x"]'}, "cellA") is True
    assert mesh._wake_policy_admits("mentions_only", {"mentions": ["cellA"]}, "cellA") is True


@pytest.mark.parametrize("bad", ["not json", "{}", None, 42, '"cellA"'])
def test_malformed_mentions_FAILS_OPEN(bad):
    """A blob we cannot parse must not decide 'not mentioned'. Erring toward
    delivery is recoverable by the reader; erring toward silence is not."""
    assert mesh._wake_policy_admits("mentions_only", {"mentions": bad}, "cellA") is True


# ── the poll loop ──────────────────────────────────────────────────────────────

def test_muted_channel_is_never_even_FETCHED(monkeypatch, tmp_path):
    """muted is honoured before spending a request — cheaper, and it proves the
    policy is consulted rather than applied as an afterthought to results."""
    state = _State(tmp_path)
    fetched = _wire(monkeypatch,
                    [{"name": "loud", "is_member": True, "wake_policy": "muted"},
                     {"name": "ops", "is_member": True, "wake_policy": "all"}],
                    {"ops": [{"id": 3, "from_node": "other"}],
                     "loud": [{"id": 9, "from_node": "other"}]})
    mesh._poll_channel_subscriptions(state)
    assert fetched == ["ops"], "a muted channel must not be fetched at all"
    assert [m["id"] for m in state.pending_channel_posts] == [3]


def test_mentions_only_filters_the_pending_list(monkeypatch, tmp_path):
    state = _State(tmp_path)
    _wire(monkeypatch,
          [{"name": "ops", "is_member": True, "wake_policy": "mentions_only"}],
          {"ops": [{"id": 1, "from_node": "other", "mentions": '["someone-else"]'},
                   {"id": 2, "from_node": "other", "mentions": '["cellA"]'},
                   {"id": 3, "from_node": "other", "mentions": "[]"}]})
    mesh._poll_channel_subscriptions(state)
    assert [m["id"] for m in state.pending_channel_posts] == [2]


def test_absent_wake_policy_admits_AND_SAYS_SO(monkeypatch, tmp_path, capsys):
    """>>> #184c APPLIED TO THIS FILTER. <<< The deployed gateway does not send
    wake_policy yet (C1 added it on an unmerged branch), so this filter is INERT in
    production today. It must pass everything through AND announce that it is not
    filtering — an unenforceable policy that looks enforced is the exact defect that
    convinced seventeen cells they had subscribed.
    """
    state = _State(tmp_path)
    _wire(monkeypatch,
          [{"name": "ops", "is_member": True}],          # no wake_policy key at all
          {"ops": [{"id": 5, "from_node": "other", "mentions": "[]"}]})
    mesh._poll_channel_subscriptions(state)
    assert [m["id"] for m in state.pending_channel_posts] == [5], "must fail OPEN"
    err = capsys.readouterr().err
    assert "wake_policy absent" in err and "INERT" in err


def test_the_inert_warning_does_NOT_fire_when_a_policy_is_present(monkeypatch, tmp_path, capsys):
    """A warning that fires when nothing is wrong is a warning nobody reads when
    something is."""
    state = _State(tmp_path)
    _wire(monkeypatch,
          [{"name": "ops", "is_member": True, "wake_policy": "all"}],
          {"ops": [{"id": 6, "from_node": "other"}]})
    mesh._poll_channel_subscriptions(state)
    assert "wake_policy absent" not in capsys.readouterr().err


def test_filtering_never_advances_the_cursor_past_an_admitted_post(monkeypatch, tmp_path):
    """A filtered-out post must not strand a later admitted one: the cursor may only
    advance to what was actually taken, or the next poll skips real mail."""
    state = _State(tmp_path)
    _wire(monkeypatch,
          [{"name": "ops", "is_member": True, "wake_policy": "mentions_only"}],
          {"ops": [{"id": 10, "from_node": "other", "mentions": '["cellA"]'},
                   {"id": 11, "from_node": "other", "mentions": '["nobody"]'}]})
    mesh._poll_channel_subscriptions(state)
    assert [m["id"] for m in state.pending_channel_posts] == [10]
    assert state.channel_cursors["ops"] == 10, (
        "cursor must track ADMITTED posts, not the raw feed high-water mark")
