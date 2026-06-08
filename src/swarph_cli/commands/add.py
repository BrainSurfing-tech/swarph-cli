"""``swarph add`` — artifact "magnet link" URI core (T1: parse/format only).

This module ships the PURE URI model + parse + format for the swarph
artifact reference — a typed, content-addressed "magnet link" that the
eventual unified ``swarph add`` installer will route on. An artifact URI
names an installable piece of swarph content (a hook, an MCP server, a
skill, or a tool) by *class*, *publisher* (cell name or
``swarph-builtin``), *name*, and optionally an exact *version* and a
content *sha256*.

Grammar::

    swarph://<class>/<publisher>/<name>[@<version>][#<sha256>]

- ``<class>`` is one of :data:`ARTIFACT_CLASSES`.
- ``<publisher>`` is a non-empty token (cell name or ``swarph-builtin``).
- ``<name>`` is a non-empty token.
- ``@<version>`` is OPTIONAL (e.g. ``1.0``, ``0.2.3``).
- ``#<sha256>`` is OPTIONAL — an opaque hex-ish content hash; in v1 the
  length is NOT validated, but it must be non-empty when ``#`` is present.

Examples::

    swarph://hook/lab-ovh/cell-resilience@1.0#a3f9c2
    swarph://mcp/swarph-builtin/fmp-server
    swarph://tool/lab-ovh/openrouter@0.4.0
    swarph://skill/lab-ovh/pdf-processing#deadbeef

T1 SCOPE: :class:`ArtifactRef` + :func:`parse_uri` + :func:`format_uri`
only. No CLI command, no argparse, no dispatcher, no install — those land
in later tasks. The parse/format pair round-trips:
``format_uri(parse_uri(s)) == s`` for every valid ``s``.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Protocol

from swarph_cli.commands import hooks


# --------------------------------------------------------------------------- #
# URI model (T1)
# --------------------------------------------------------------------------- #


#: The fixed enum of installable artifact classes.
ARTIFACT_CLASSES = ("hook", "mcp", "skill", "tool")

#: URI scheme prefix.
_SCHEME = "swarph://"


@dataclass(frozen=True)
class ArtifactRef:
    """A parsed, content-addressed reference to an installable artifact.

    ``klass`` is one of :data:`ARTIFACT_CLASSES`. ``publisher`` and
    ``name`` are non-empty tokens. ``version`` and ``sha256`` are the
    optional ``@<version>`` and ``#<sha256>`` URI parts (``None`` when
    absent).
    """

    klass: str
    publisher: str
    name: str
    version: str | None = None
    sha256: str | None = None


def parse_uri(s: str) -> ArtifactRef:
    """Parse a ``swarph://`` artifact URI into an :class:`ArtifactRef`.

    Raises :class:`ValueError` for any malformed input: a wrong scheme,
    the wrong number of ``class/publisher/rest`` segments, an unknown
    class, or any empty required field (publisher, name, the value after
    ``@``, or the value after ``#``).
    """
    if not s.startswith(_SCHEME):
        raise ValueError(f"not a swarph:// URI: {s!r}")

    body = s[len(_SCHEME):]
    parts = body.split("/")
    if len(parts) != 3:
        raise ValueError(
            "expected swarph://<class>/<publisher>/<name>[@<version>][#<sha256>], "
            f"got {s!r}"
        )

    klass, publisher, rest = parts
    if klass not in ARTIFACT_CLASSES:
        raise ValueError(
            f"unknown artifact class {klass!r}; "
            f"valid classes: {', '.join(ARTIFACT_CLASSES)}"
        )
    if not publisher:
        raise ValueError(f"empty publisher in {s!r}")

    # Split off #sha256 FIRST, then @version, from the right.
    sha256: str | None = None
    if "#" in rest:
        rest, sha256 = rest.rsplit("#", 1)
        if not sha256:
            raise ValueError(f"empty sha256 after '#' in {s!r}")

    version: str | None = None
    if "@" in rest:
        rest, version = rest.rsplit("@", 1)
        if not version:
            raise ValueError(f"empty version after '@' in {s!r}")

    name = rest
    if not name:
        raise ValueError(f"empty name in {s!r}")

    return ArtifactRef(
        klass=klass,
        publisher=publisher,
        name=name,
        version=version,
        sha256=sha256,
    )


def format_uri(ref: ArtifactRef) -> str:
    """Reconstruct the ``swarph://`` URI string for an :class:`ArtifactRef`.

    Omits the optional ``@<version>`` and ``#<sha256>`` parts when they
    are ``None``. Round-trips with :func:`parse_uri`:
    ``format_uri(parse_uri(s)) == s`` for every valid ``s``.
    """
    out = f"{_SCHEME}{ref.klass}/{ref.publisher}/{ref.name}"
    if ref.version is not None:
        out += f"@{ref.version}"
    if ref.sha256 is not None:
        out += f"#{ref.sha256}"
    return out


# --------------------------------------------------------------------------- #
# Handler interface + result (T2)
# --------------------------------------------------------------------------- #


#: The publisher token that marks a bundled (trusted) artifact.
_BUILTIN_PUBLISHER = "swarph-builtin"


@dataclass(frozen=True)
class HandlerResult:
    """Outcome of a single handler ``add`` call.

    ``code`` is the exit code (0 = installed). ``detail`` is a short
    human-readable summary. Handlers return a bare ``int`` exit code from
    ``add``; this dataclass is the richer shape the dispatcher MAY wrap a
    result in for callers that want structured reporting.
    """

    code: int
    detail: str


class ArtifactHandler(Protocol):
    """Structural interface a per-class installer must satisfy.

    A handler has a ``klass`` string naming the artifact class it installs
    and an ``add`` method that installs ``ref`` and returns an exit code
    (0 = installed). The dispatcher routes on ``ArtifactRef.klass`` →
    ``handler.klass``.
    """

    klass: str

    def add(self, ref: ArtifactRef, *, assume_yes: bool, out) -> int:  # pragma: no cover - protocol
        ...


class HookHandler:
    """Installs ``hook`` artifacts by URI.

    v1 is BUILTIN-ONLY: a ``swarph-builtin`` publisher resolves to a bundled
    hook and installs via the shipped ``hooks.install_hook``. ANY other
    publisher is a published/untrusted reference and FAILS CLOSED — mirroring
    the shipped ``swarph hooks add`` ``@published`` policy — mutating nothing
    (signed-publisher + security gate is v2, scope §3.1).

    Note: a ``swarph://`` URI can't express a local filesystem path, so the
    URI hook path is builtin-only until the v2 trust gate; local hooks still
    install via ``swarph hooks add <path>``.
    """

    klass = "hook"

    def __init__(
        self,
        *,
        settings_path=hooks._DEFAULT_SETTINGS_PATH,
        hooks_home=hooks._DEFAULT_HOOKS_HOME,
    ) -> None:
        self.settings_path = settings_path
        self.hooks_home = hooks_home

    def add(self, ref: ArtifactRef, *, assume_yes: bool, out) -> int:
        if ref.publisher != _BUILTIN_PUBLISHER:
            out(
                "swarph add: published hooks are not yet trusted — only "
                "swarph-builtin hooks install via URI in v1 (signed-publisher "
                "+ security gate is v2, scope §3.1); nothing installed"
            )
            return 2
        # A ValueError for an unknown builtin name propagates — caught at the
        # CLI layer (run_add).
        bundle = hooks.resolve_builtin(ref.name)
        return hooks.install_hook(
            bundle,
            settings_path=self.settings_path,
            hooks_home=self.hooks_home,
            assume_yes=assume_yes,
            out=out,
        )


class StubHandler:
    """Placeholder handler for an artifact class not yet implemented.

    ``add`` prints a clear "not yet implemented" line (naming the class and a
    one-line ``note`` describing what the real handler will do) and returns 3,
    mutating nothing. Lets ``swarph add`` accept the full URI grammar today
    while the per-class installers land incrementally.
    """

    def __init__(self, klass: str, note: str) -> None:
        self.klass = klass
        self.note = note

    def add(self, ref: ArtifactRef, *, assume_yes: bool, out) -> int:
        out(
            f"swarph add: {self.klass} install is not yet implemented in this "
            f"swarph-cli — {self.note}"
        )
        return 3


def build_registry(*, settings_path=None, hooks_home=None) -> dict:
    """Build the ``klass`` → handler registry for one ``add`` invocation.

    The hook paths are threaded into :class:`HookHandler` (defaulting to the
    hooks-module defaults when ``None``) so tests can point the whole install
    at tmp paths. The not-yet-built classes get :class:`StubHandler`s.
    """
    hook_kwargs = {}
    if settings_path is not None:
        hook_kwargs["settings_path"] = settings_path
    if hooks_home is not None:
        hook_kwargs["hooks_home"] = hooks_home

    return {
        "hook": HookHandler(**hook_kwargs),
        "mcp": StubHandler(
            "mcp", "registers an MCP server in .mcp.json — T3"
        ),
        "skill": StubHandler(
            "skill", "drops a skill bundle into the skills dir — T3"
        ),
        "tool": StubHandler(
            "tool", "bridges to swarph-mesh's adapter registry — follow-on"
        ),
    }


def dispatch_add(ref: ArtifactRef, *, assume_yes: bool, out, registry) -> int:
    """Route ``ref`` to its per-class handler and run ``add``.

    ``ref.klass`` is already validated by :func:`parse_uri`, but a missing
    handler is handled defensively: a clear error + non-zero exit rather than
    a ``KeyError`` traceback.
    """
    handler = registry.get(ref.klass)
    if handler is None:
        out(f"swarph add: no handler registered for class {ref.klass!r}")
        return 4
    return handler.add(ref, assume_yes=assume_yes, out=out)


def run_add(argv, *, settings_path=None, hooks_home=None) -> int:
    """``swarph add <uri> [--yes]`` — parse + dispatch a swarph artifact URI.

    Parses the positional ``uri`` into an :class:`ArtifactRef` (a malformed
    URI → ``swarph add: <msg>`` on stderr, return 2, nothing mutated), builds
    the registry, and dispatches. A ``ValueError`` from the handler (e.g. an
    unknown builtin name from ``resolve_builtin``) is caught here → stderr +
    return 2, nothing installed.
    """
    parser = argparse.ArgumentParser(
        prog="swarph add",
        description="Install a swarph artifact by swarph:// URI.",
    )
    parser.add_argument(
        "uri",
        help="swarph://<class>/<publisher>/<name>[@<version>][#<sha256>]",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip confirmation prompts (no-op for trusted builtins)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    try:
        ref = parse_uri(args.uri)
    except ValueError as exc:
        print(f"swarph add: {exc}", file=sys.stderr)
        return 2

    registry = build_registry(settings_path=settings_path, hooks_home=hooks_home)

    try:
        return dispatch_add(ref, assume_yes=args.yes, out=print, registry=registry)
    except ValueError as exc:
        print(f"swarph add: {exc}", file=sys.stderr)
        return 2
