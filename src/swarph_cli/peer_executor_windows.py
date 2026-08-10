"""Windows identity boundary for unattended peer-service workers.

The service installer owns ACL creation.  This module verifies the installed
mapping at runtime and deliberately requires an explicit ACL verifier instead
of treating a locale-dependent command-line ACL listing as an authority.
"""
from __future__ import annotations

import csv
import io
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .peer_executor import PeerExecutorError


@dataclass(frozen=True)
class WindowsPeerServiceManifest:
    peer: str
    service_sid: str
    spool_root: Path


def current_windows_sid() -> str:
    """Return the current principal SID using Windows' machine-readable form."""
    result = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if len(rows) != 1 or len(rows[0]) != 2 or not rows[0][1].startswith("S-"):
        raise PeerExecutorError("could not determine current Windows service SID")
    return rows[0][1]


class WindowsPeerServiceAuthorizer:
    """Fail-closed peer-to-SID and private-spool mapping verifier."""

    def __init__(
        self,
        manifests: Mapping[str, WindowsPeerServiceManifest],
        *,
        sid_reader: Callable[[], str] = current_windows_sid,
        path_is_private: Callable[[Path, str], bool],
    ):
        self.manifests = dict(manifests)
        self.sid_reader = sid_reader
        self.path_is_private = path_is_private

    def require_service(self, peer: str, spool_root: Path) -> None:
        manifest = self.manifests.get(peer)
        if manifest is None:
            raise PeerExecutorError("no Windows peer-service mapping exists")
        expected_root = manifest.spool_root.resolve(strict=False)
        actual_root = Path(spool_root).resolve(strict=False)
        if actual_root != expected_root:
            raise PeerExecutorError("spool root does not match the peer-service manifest")
        if self.sid_reader().casefold() != manifest.service_sid.casefold():
            raise PeerExecutorError("current Windows service SID does not match manifest")
        if not self.path_is_private(expected_root, manifest.service_sid):
            raise PeerExecutorError("peer-service spool is not private to its service SID")
