from pathlib import Path

import pytest

from swarph_cli.peer_executor import PeerExecutorError
from swarph_cli.peer_executor_windows import WindowsPeerServiceAuthorizer, WindowsPeerServiceManifest


def _authorizer(tmp_path, **kwargs):
    root = tmp_path / "peer-spool"
    manifest = WindowsPeerServiceManifest("gpt-lc", "S-1-5-21-100", root)
    return WindowsPeerServiceAuthorizer(
        {"gpt-lc": manifest},
        sid_reader=kwargs.get("sid_reader", lambda: "S-1-5-21-100"),
        path_is_private=kwargs.get("path_is_private", lambda path, sid: path == root and sid == "S-1-5-21-100"),
    ), root


def test_windows_authorizer_accepts_matching_sid_and_private_spool(tmp_path):
    authorizer, root = _authorizer(tmp_path)
    authorizer.require_service("gpt-lc", root)


def test_windows_authorizer_rejects_wrong_sid_or_root_or_acl(tmp_path):
    authorizer, root = _authorizer(tmp_path, sid_reader=lambda: "S-1-5-21-200")
    with pytest.raises(PeerExecutorError, match="SID"):
        authorizer.require_service("gpt-lc", root)

    authorizer, root = _authorizer(tmp_path, path_is_private=lambda path, sid: False)
    with pytest.raises(PeerExecutorError, match="not private"):
        authorizer.require_service("gpt-lc", root)

    authorizer, root = _authorizer(tmp_path)
    with pytest.raises(PeerExecutorError, match="spool root"):
        authorizer.require_service("gpt-lc", Path(tmp_path) / "other-spool")
