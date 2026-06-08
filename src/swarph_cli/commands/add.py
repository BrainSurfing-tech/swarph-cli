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

from dataclasses import dataclass


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
