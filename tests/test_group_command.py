"""Tests for the ``swarph group`` / ``swarph rights`` verbs.

The group module binds its gateway helpers by name (``from ... import
post_json``), so the fakes are patched as attributes of the group module.
Identity/token resolve from env so no token files are touched. No real
network is used anywhere in this file.
"""

from __future__ import annotations

import pytest

from swarph_cli.commands import group


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    monkeypatch.setenv("SWARPH_SELF", "c1")
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "tok")


# ── pure builders ──────────────────────────────────────────────────────────


def test_group_url_encodes_name():
    assert group._group_url("http://gw:8788", "a/b c") == "http://gw:8788/groups/a%2Fb%20c"


def test_members_url():
    assert group._members_url("http://gw:8788", "eng") == "http://gw:8788/groups/eng/members"


def test_member_url_encodes_peer():
    url = group._member_url("http://gw:8788", "eng", "lab ovh")
    assert url == "http://gw:8788/groups/eng/members/lab%20ovh"


def test_grants_url():
    assert group._grants_url("http://gw:8788", "eng") == "http://gw:8788/groups/eng/grants"


def test_peer_grants_url_encodes_peer():
    url = group._peer_grants_url("http://gw:8788", "lab/ovh")
    assert url == "http://gw:8788/peers/lab%2Fovh/grants"


def test_group_create_payload_omits_empty_description():
    p = group._group_create_payload("eng", None, "custom")
    assert p == {"name": "eng", "kind": "custom"}
    assert "description" not in p


def test_group_create_payload_with_description():
    p = group._group_create_payload("eng", "engineering team", "role")
    assert p == {"name": "eng", "kind": "role", "description": "engineering team"}


def test_member_add_payload():
    assert group._member_add_payload("lab-ovh") == {"peer": "lab-ovh"}


def test_grant_add_payload():
    assert group._grant_add_payload("board", "cards", "execute") == {
        "grant_type": "board", "target": "cards", "level": "execute",
    }


def test_revoke_payload():
    assert group._revoke_payload("board", "cards") == {"grant_type": "board", "target": "cards"}


def test_check_payload():
    assert group._check_payload("lab-ovh", "board", "cards") == {
        "peer": "lab-ovh", "grant_type": "board", "target": "cards",
    }


# ── formatters ─────────────────────────────────────────────────────────────


def test_format_groups_empty():
    assert group._format_groups([]) == "(no groups)"


def test_format_groups_lists_kind_and_member_count():
    data = [
        {"name": "eng", "kind": "role", "description": "engineering", "member_count": 3},
        {"name": "adhoc", "kind": "custom", "description": "", "member_count": 0},
    ]
    out = group._format_groups(data)
    assert "eng" in out and "role" in out and "3 members" in out and "engineering" in out
    assert "adhoc" in out and "custom" in out and "0 members" in out


def test_format_members_empty():
    assert group._format_members([]) == "(no members)"


def test_format_members_one_per_line():
    data = [{"peer": "lab-ovh"}, {"peer": "gemini"}]
    out = group._format_members(data)
    assert out.splitlines() == ["lab-ovh", "gemini"]


def test_format_grants_empty():
    assert group._format_grants([]) == "(no grants)"


def test_format_grants_columns():
    data = [{"grant_type": "board", "target": "cards", "level": "execute"}]
    out = group._format_grants(data)
    assert "board" in out and "cards" in out and "execute" in out


def test_format_rights_header_and_direct_fallback():
    data = {"peer": "lab-ovh", "groups": ["eng", "ops"], "grants": [
        {"grant_type": "board", "target": "cards", "level": "read", "via_group": None},
    ]}
    out = group._format_rights(data)
    lines = out.splitlines()
    assert lines[0] == "lab-ovh — groups: eng, ops"
    assert "board" in lines[1] and "cards" in lines[1] and "read" in lines[1]
    assert "(via direct)" in lines[1]


def test_format_rights_via_group():
    data = {"peer": "lab-ovh", "groups": ["eng"], "grants": [
        {"grant_type": "board", "target": "cards", "level": "admin", "via_group": "eng"},
    ]}
    out = group._format_rights(data)
    assert "(via eng)" in out


def test_format_rights_direct_bool_wins_over_via_group():
    """The gateway's explicit `direct` bool is canonical: on a live-union
    endpoint a grant can carry a via_group AND direct=true, and the CLI must
    render `direct` (not the via_group) so an audit reads the truth."""
    data = {"peer": "lab-ovh", "groups": ["eng"], "grants": [
        {"grant_type": "board", "target": "cards", "level": "admin",
         "via_group": "eng", "direct": True},
    ]}
    out = group._format_rights(data)
    assert "(via direct)" in out
    assert "(via eng)" not in out


def test_format_rights_direct_false_renders_via_group():
    data = {"peer": "lab-ovh", "groups": ["eng"], "grants": [
        {"grant_type": "channel", "target": "releases", "level": "read",
         "via_group": "eng", "direct": False},
    ]}
    out = group._format_rights(data)
    assert "(via eng)" in out


def test_format_rights_no_grants():
    data = {"peer": "lab-ovh", "groups": [], "grants": []}
    out = group._format_rights(data)
    assert out.splitlines()[0] == "lab-ovh — groups: "
    assert "(no grants)" in out


def test_format_check_allow_true():
    assert group._format_check({"allow": True, "via_group": "eng", "level": "read"}) == \
        "allow=true via eng (read)"


def test_format_check_allow_true_direct():
    assert group._format_check({"allow": True, "via_group": None, "level": "admin"}) == \
        "allow=true via direct (admin)"


def test_format_check_allow_false():
    assert group._format_check({"allow": False}) == "allow=false"


# ── run_group dispatch (network monkeypatched) ────────────────────────────


def test_group_create_defaults_kind_custom(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body, token=token)
        return (201, {"name": "eng", "kind": "custom", "created_by": "c1", "created_at": "t"})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["create", "eng"])
    assert rc == 0
    assert cap["url"].endswith("/groups")
    assert cap["body"] == {"name": "eng", "kind": "custom"}
    assert cap["token"] == "tok"


def test_group_create_with_description_and_kind(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(body=body)
        return (201, {"name": "eng", "kind": "role"})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["create", "eng", "--description", "engineering", "--kind", "role"])
    assert rc == 0
    assert cap["body"] == {"name": "eng", "kind": "role", "description": "engineering"}


def test_group_list_does_get(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (200, [])

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_group(["list"])
    assert rc == 0
    assert cap["url"].endswith("/groups")


def test_group_delete_uses_delete_json(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (204, {})

    monkeypatch.setattr(group, "delete_json", fake)
    rc = group.run_group(["delete", "eng"])
    assert rc == 0
    assert cap["url"].endswith("/groups/eng")


def test_group_members_does_get(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (200, [])

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_group(["members", "eng"])
    assert rc == 0
    assert cap["url"].endswith("/groups/eng/members")


def test_group_add_posts_peer(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body)
        return (201, {})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["add", "eng", "lab-ovh"])
    assert rc == 0
    assert cap["url"].endswith("/groups/eng/members")
    assert cap["body"] == {"peer": "lab-ovh"}


def test_group_remove_uses_delete_json_with_peer_path(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (204, {})

    monkeypatch.setattr(group, "delete_json", fake)
    rc = group.run_group(["remove", "eng", "lab-ovh"])
    assert rc == 0
    assert cap["url"].endswith("/groups/eng/members/lab-ovh")


def test_group_grants_does_get(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (200, [])

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_group(["grants", "eng"])
    assert rc == 0
    assert cap["url"].endswith("/groups/eng/grants")


def test_group_grant_defaults_level_read(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body)
        return (201, {})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["grant", "eng", "board", "cards"])
    assert rc == 0
    assert cap["url"].endswith("/groups/eng/grants")
    assert cap["body"] == {"grant_type": "board", "target": "cards", "level": "read"}


def test_group_grant_explicit_level(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(body=body)
        return (201, {})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["grant", "eng", "board", "cards", "--level", "admin"])
    assert rc == 0
    assert cap["body"]["level"] == "admin"


def test_group_revoke_sends_body_via_delete_json_body(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body, token=token)
        return (204, {})

    monkeypatch.setattr(group, "_delete_json_body", fake)
    rc = group.run_group(["revoke", "eng", "board", "cards"])
    assert rc == 0
    assert cap["url"].endswith("/groups/eng/grants")
    assert cap["body"] == {"grant_type": "board", "target": "cards"}
    assert cap["token"] == "tok"


def test_group_revoke_does_not_call_plain_delete_json(monkeypatch):
    called = {"plain_delete": False}

    def fail_plain(*a, **k):
        called["plain_delete"] = True
        return (204, {})

    def fake_body(url, body, token, **k):
        return (204, {})

    monkeypatch.setattr(group, "delete_json", fail_plain)
    monkeypatch.setattr(group, "_delete_json_body", fake_body)
    rc = group.run_group(["revoke", "eng", "board", "cards"])
    assert rc == 0
    assert called["plain_delete"] is False


def test_group_check_renders_allow(monkeypatch, capsys):
    def fake(url, body, token, **k):
        return (200, {"allow": True, "via_group": "eng", "level": "read"})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["check", "lab-ovh", "board", "cards"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "allow=true via eng (read)" in out


def test_group_check_url_and_payload(monkeypatch):
    cap = {}

    def fake(url, body, token, **k):
        cap.update(url=url, body=body)
        return (200, {"allow": False})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["check", "lab-ovh", "board", "cards"])
    assert rc == 0
    assert cap["url"].endswith("/authz/check")
    assert cap["body"] == {"peer": "lab-ovh", "grant_type": "board", "target": "cards"}


def test_group_json_flag_emits_raw_body(monkeypatch, capsys):
    def fake(url, token, **k):
        return (200, [{"name": "eng", "kind": "role", "member_count": 1}])

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_group(["list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"name": "eng"' in out


def test_group_name_url_encoded_in_delete(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (204, {})

    monkeypatch.setattr(group, "delete_json", fake)
    rc = group.run_group(["delete", "a/b c"])
    assert rc == 0
    assert "a%2Fb%20c" in cap["url"]


def test_group_non_2xx_returns_1(monkeypatch, capsys):
    def fake(url, token, **k):
        return (404, {"detail": "no such group"})

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_group(["members", "ghost"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "swarph group: gateway 404: no such group" in err


def test_group_unreachable_returns_1(monkeypatch, capsys):
    def fake(url, body, token, **k):
        return (0, {"detail": "Connection refused"})

    monkeypatch.setattr(group, "post_json", fake)
    rc = group.run_group(["create", "eng"])
    assert rc == 1
    assert "unreachable" in capsys.readouterr().err


# ── run_rights dispatch ────────────────────────────────────────────────────


def test_rights_no_arg_resolves_self(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (200, {"peer": "c1", "groups": [], "grants": []})

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_rights([])
    assert rc == 0
    assert cap["url"].endswith("/peers/c1/grants")


def test_rights_explicit_peer(monkeypatch):
    cap = {}

    def fake(url, token, **k):
        cap.update(url=url)
        return (200, {"peer": "lab-ovh", "groups": [], "grants": []})

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_rights(["lab-ovh"])
    assert rc == 0
    assert cap["url"].endswith("/peers/lab-ovh/grants")


def test_rights_renders_human_format(monkeypatch, capsys):
    def fake(url, token, **k):
        return (200, {"peer": "lab-ovh", "groups": ["eng"], "grants": [
            {"grant_type": "board", "target": "cards", "level": "read", "via_group": "eng"},
        ]})

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_rights(["lab-ovh"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lab-ovh — groups: eng" in out
    assert "(via eng)" in out


def test_rights_json_flag(monkeypatch, capsys):
    def fake(url, token, **k):
        return (200, {"peer": "lab-ovh", "groups": [], "grants": []})

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_rights(["lab-ovh", "--json"])
    assert rc == 0
    assert '"peer": "lab-ovh"' in capsys.readouterr().out


def test_rights_non_2xx_uses_rights_prefix(monkeypatch, capsys):
    def fake(url, token, **k):
        return (403, {"detail": "forbidden"})

    monkeypatch.setattr(group, "get_json", fake)
    rc = group.run_rights(["lab-ovh"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "swarph rights: gateway 403: forbidden" in err


# ── verb registration ──────────────────────────────────────────────────────


def test_group_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["group"] == "swarph_cli.commands.group.run_group"


def test_rights_registered_in_verb_handlers():
    from swarph_cli.main import _VERB_HANDLERS
    assert _VERB_HANDLERS["rights"] == "swarph_cli.commands.group.run_rights"
