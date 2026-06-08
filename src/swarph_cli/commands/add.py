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
import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

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
# Registry-record → URI contract helper (T5a) — the discover→install bridge
# --------------------------------------------------------------------------- #


#: Map a registry feature record's ``type`` onto an :data:`ARTIFACT_CLASSES`
#: class. A search face (metaedge.surf) and the cell registry describe a
#: capability by ``type``; this is the one place that vocabulary maps onto the
#: ``swarph add`` artifact classes. Synonyms collapse: ``mcp_server`` → ``mcp``
#: and ``adapter`` → ``tool``.
_TYPE_TO_CLASS = {
    "hook": "hook",
    "mcp": "mcp",
    "mcp_server": "mcp",
    "skill": "skill",
    "tool": "tool",
    "adapter": "tool",
}


def feature_to_uri(record: dict, *, default_publisher: Optional[str] = None) -> str:
    """Build a ``swarph://`` URI from a registry/feature record dict.

    This is the swarph-cli HALF of the discover→install loop: a search face
    (metaedge.surf) or the cell registry hands back a feature record, and this
    turns it into a typed, content-addressed magnet-link URI a user/LLM can
    ``swarph add``. Field resolution:

    * **name**: ``record["name"]`` — required (``ValueError`` if absent/empty).
    * **publisher**: first non-empty of ``record["publisher"]``,
      ``record["cell"]``, then ``default_publisher`` — else ``ValueError``.
    * **class**: ``record["artifact_class"]`` if it is a valid class; else
      ``record["type"]`` mapped through :data:`_TYPE_TO_CLASS`. An
      unmappable/absent type → ``ValueError``.
    * **version**: ``record.get("version")`` — optional.
    * **sha256**: first non-empty of ``record["sha256"]``, ``record["sha"]`` —
      optional.

    Reuses :func:`format_uri` so the output always round-trips with
    :func:`parse_uri`.
    """
    name = record.get("name")
    if not name:
        raise ValueError("record has no 'name' field")

    publisher = (
        record.get("publisher") or record.get("cell") or default_publisher
    )
    if not publisher:
        raise ValueError(
            "no publisher in record and no default_publisher given"
        )

    artifact_class = record.get("artifact_class")
    if artifact_class in ARTIFACT_CLASSES:
        klass = artifact_class
    else:
        klass = _TYPE_TO_CLASS.get(record.get("type"))
        if klass is None:
            raise ValueError(
                f"cannot determine artifact class from record: "
                f"artifact_class={record.get('artifact_class')!r}, "
                f"type={record.get('type')!r}"
            )

    version = record.get("version")
    sha256 = record.get("sha256") or record.get("sha")

    ref = ArtifactRef(
        klass=klass,
        publisher=publisher,
        name=name,
        version=version,
        sha256=sha256,
    )
    return format_uri(ref)


# --------------------------------------------------------------------------- #
# Handler interface + result (T2)
# --------------------------------------------------------------------------- #


#: The publisher token that marks a bundled (trusted) artifact.
_BUILTIN_PUBLISHER = "swarph-builtin"

#: The set of publishers swarph-cli trusts to install via URI in v1. One
#: place defines the trust boundary; the per-class handlers consult
#: :func:`_is_trusted_publisher` rather than inlining the literal so the
#: boundary moves in exactly one edit when the v2 signed-publisher gate lands.
TRUSTED_PUBLISHERS = frozenset({_BUILTIN_PUBLISHER})


def _is_trusted_publisher(publisher: str) -> bool:
    """True iff ``publisher`` is in the v1 trust set (:data:`TRUSTED_PUBLISHERS`)."""
    return publisher in TRUSTED_PUBLISHERS


# --------------------------------------------------------------------------- #
# Content-hash verification (T4) — the #sha256 in a magnet-link URI
# --------------------------------------------------------------------------- #


def sha256_hex(content: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``content``."""
    return hashlib.sha256(content).hexdigest()


def verify_sha256(content: bytes, expected_hex: str) -> bool:
    """True iff ``sha256_hex(content)`` equals ``expected_hex``.

    Case-insensitive, surrounding whitespace stripped on the expected value.
    Full-digest exact match only — abbreviated/prefix digests are a future
    nicety (a magnet link should carry the full digest), not supported here.
    """
    return sha256_hex(content) == expected_hex.strip().lower()


def _hash_guard(ref: ArtifactRef, canonical: bytes, out) -> Optional[int]:
    """Verify a URI's ``#sha256`` against an artifact's canonical bytes.

    Returns ``None`` to PROCEED — either the URI carries no ``#sha256``
    (``ref.sha256 is None``) or the digest matches. On MISMATCH, prints the
    refusal line and returns refuse-code ``5`` (distinct from 2=fail-closed /
    3=stub / 4=missing-handler) so the caller installs nothing.
    """
    if ref.sha256 is None:
        return None
    if verify_sha256(canonical, ref.sha256):
        return None
    out(
        "swarph add: content-hash mismatch — the artifact does not match the "
        f"#sha256 in the URI (expected {ref.sha256}, got "
        f"{sha256_hex(canonical)}); refusing to install"
    )
    return 5


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

    def _canonical_bytes(self, bundle) -> bytes:
        """Deterministic installable bytes for a :class:`hooks.HookBundle`.

        The hook's installable content is its inline ``script_body`` — the
        exact bytes written under ``hooks_home/<script_name>``.
        """
        return bundle.script_body.encode("utf-8")

    def add(self, ref: ArtifactRef, *, assume_yes: bool, out) -> int:
        if not _is_trusted_publisher(ref.publisher):
            out(
                "swarph add: published hooks are not yet trusted — only "
                "swarph-builtin hooks install via URI in v1 (signed-publisher "
                "+ security gate is v2, scope §3.1); nothing installed"
            )
            return 2
        # A ValueError for an unknown builtin name propagates — caught at the
        # CLI layer (run_add).
        bundle = hooks.resolve_builtin(ref.name)
        guard = _hash_guard(ref, self._canonical_bytes(bundle), out)
        if guard is not None:
            return guard
        return hooks.install_hook(
            bundle,
            settings_path=self.settings_path,
            hooks_home=self.hooks_home,
            assume_yes=assume_yes,
            out=out,
        )


# --------------------------------------------------------------------------- #
# MCP handler (T3-mcp) — install an MCP server into project .mcp.json
# --------------------------------------------------------------------------- #


def _load_mcp_config(path) -> dict:
    """Load a project ``.mcp.json``, returning ``{}`` for a missing file.

    Mirrors :func:`hooks._load_settings`'s fail-closed contract:

    * Missing file → ``{}`` — a fine empty starting point for the installer.
    * CORRUPT JSON → ``ValueError`` (with the path). We MUST NOT silently
      return ``{}``: a later :func:`_save_mcp_config` would then overwrite the
      user's real-but-unparseable file with our merged-onto-empty result,
      destroying their config.
    * VALID-yet-NON-OBJECT JSON (``[]``, ``null``, ``5`` …) → ``ValueError``
      too. A non-dict can't be merged onto (``setdefault`` would crash) and
      silently treating it as ``{}`` would overwrite the user's real file. The
      contract is "never silently overwrite a user's real .mcp.json."
    * Valid OBJECT → the parsed dict.
    """
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f".mcp.json at {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f".mcp.json at {p} is not a JSON object (got "
            f"{type(data).__name__}); refusing to merge onto a non-object "
            f".mcp.json file"
        )
    return data


def _save_mcp_config(path, obj) -> None:
    """Atomically write ``obj`` as JSON to ``path`` (``indent=2``, mkdir -p).

    Writes to a temp file in the same directory then ``os.replace`` — a crash
    mid-write leaves either the old complete file or the new complete file,
    never a truncated one. Mirrors :func:`hooks._save_settings`.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _merge_mcp_server(config: dict, name: str, server_spec: dict) -> dict:
    """Merge one ``name`` → ``server_spec`` into ``config["mcpServers"]``.

    Mutate-and-return. Ensures ``config.setdefault("mcpServers", {})`` then
    sets ``config["mcpServers"][name] = server_spec``. Idempotent (same
    name+spec twice = identical result). Preserves every other server and
    every other top-level key.
    """
    servers = config.setdefault("mcpServers", {})
    servers[name] = server_spec
    return config


def _unmerge_mcp_server(config: dict, name: str) -> dict:
    """Reverse of :func:`_merge_mcp_server`. Mutate-and-return, idempotent.

    Removes ``mcpServers[name]`` if present; prunes an emptied ``mcpServers``
    key; no-op (never raises) when the key or the server is absent. Preserves
    all siblings.
    """
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return config
    servers.pop(name, None)
    if not servers:
        config.pop("mcpServers", None)
    return config


@dataclass(frozen=True)
class McpBundle:
    """An installable MCP server: a ``.mcp.json`` server-spec + metadata."""

    name: str
    description: str
    publisher: str  # "swarph-builtin" for bundled
    trust: str  # "builtin"
    server_spec: dict  # the .mcp.json server config object


BUILTIN_MCP: dict = {
    "everything": McpBundle(
        name="everything",
        description=(
            "The official Model Context Protocol reference server "
            "(@modelcontextprotocol/server-everything) — exercises prompts, "
            "resources, and tools over stdio. A safe smoke-test MCP server."
        ),
        publisher="swarph-builtin",
        trust="builtin",
        server_spec={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-everything"],
        },
    ),
}


def resolve_builtin_mcp(name: str) -> McpBundle:
    """Return the bundled :class:`McpBundle` named ``name``.

    Raises :class:`ValueError` (listing the available names) for an unknown
    name — propagated to the CLI layer, which surfaces it; nothing installed.
    """
    try:
        return BUILTIN_MCP[name]
    except KeyError:
        available = ", ".join(sorted(BUILTIN_MCP))
        raise ValueError(
            f"unknown builtin MCP server {name!r}; available: {available}"
        ) from None


class McpHandler:
    """Installs ``mcp`` artifacts into a project-scope ``.mcp.json``.

    v1 is BUILTIN-ONLY (mirrors :class:`HookHandler`): a ``swarph-builtin``
    publisher resolves to a bundled :class:`McpBundle` and writes its
    ``server_spec`` under ``mcpServers/<name>``. ANY other publisher is a
    published/untrusted reference and FAILS CLOSED, mutating nothing
    (signed-publisher + per-class security gate is v2, scope §3.1/§4).
    """

    klass = "mcp"

    def __init__(self, *, mcp_config_path=None) -> None:
        self.mcp_config_path = (
            mcp_config_path if mcp_config_path is not None else Path.cwd() / ".mcp.json"
        )

    def _canonical_bytes(self, bundle) -> bytes:
        """Deterministic installable bytes for an :class:`McpBundle`.

        The installable content is the ``server_spec`` written under
        ``mcpServers/<name>``; serialized with sorted keys + compact
        separators so equal specs always hash equal regardless of key order.
        """
        return json.dumps(
            bundle.server_spec, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def add(self, ref: ArtifactRef, *, assume_yes: bool, out) -> int:
        if not _is_trusted_publisher(ref.publisher):
            out(
                "swarph add: published MCP servers are not yet trusted — only "
                "swarph-builtin in v1 (v2: signed-publisher + per-class security "
                "gate, scope §3.1/§4); nothing installed"
            )
            return 2

        # ValueError for an unknown builtin name propagates → caught at run_add.
        bundle = resolve_builtin_mcp(ref.name)

        guard = _hash_guard(ref, self._canonical_bytes(bundle), out)
        if guard is not None:
            return guard

        # ---- show-before-write preview (builtin = trusted, no prompt) ----
        out(f"mcp: {bundle.name}  (trust={bundle.trust}, publisher={bundle.publisher})")
        if bundle.description:
            out(f"  {bundle.description}")
        out(f"config → {Path(self.mcp_config_path).expanduser()}")
        out("  will add this server spec to mcpServers:")
        out(f"    {bundle.name} = {json.dumps(bundle.server_spec)}")

        # ---- load + merge FIRST (load failure aborts with nothing written) --
        config = _load_mcp_config(self.mcp_config_path)
        config = _merge_mcp_server(config, bundle.name, bundle.server_spec)
        _save_mcp_config(self.mcp_config_path, config)

        out(f"installed MCP server '{bundle.name}' into {Path(self.mcp_config_path).expanduser()}")
        return 0


# --------------------------------------------------------------------------- #
# Skill handler (T3-skill) — install a skill bundle into the skills dir
# --------------------------------------------------------------------------- #


_DEFAULT_SKILLS_HOME = Path.home() / ".claude" / "skills"


#: The body of the bundled ``swarph-intro`` skill's ``SKILL.md``. Valid Claude
#: Code skill frontmatter (``name``/``description``) + a short body.
_SWARPH_INTRO_SKILL_MD = """\
---
name: swarph-intro
description: Use when the user asks what the swarph is or how to discover and install capabilities across the mesh.
---
# swarph-intro
The swarph is the agnostic AI coordination mesh. Discover capabilities by \
searching (metaedge), install them with `swarph add \
swarph://<class>/<publisher>/<name>` (class = hook | mcp | skill | tool), or \
browse builtins with `swarph hooks list`. Every build leaves a reusable tool \
in the commons.
"""


@dataclass(frozen=True)
class SkillBundle:
    """An installable skill: its files + metadata.

    ``files`` is a tuple of ``(relative_path, file_content)`` pairs (a tuple,
    not a dict, so the dataclass stays ``frozen``/hashable) and MUST include a
    ``"SKILL.md"`` entry — the YAML-frontmatter + body that Claude Code reads.
    """

    name: str
    description: str
    publisher: str  # "swarph-builtin" for bundled
    trust: str  # "builtin"
    files: tuple  # tuple[tuple[str, str], ...] — (relpath, content) pairs


BUILTIN_SKILLS: dict = {
    "swarph-intro": SkillBundle(
        name="swarph-intro",
        description=(
            "Use when the user asks what the swarph is or how to discover and "
            "install capabilities across the mesh."
        ),
        publisher="swarph-builtin",
        trust="builtin",
        files=(("SKILL.md", _SWARPH_INTRO_SKILL_MD),),
    ),
}


def resolve_builtin_skill(name: str) -> SkillBundle:
    """Return the bundled :class:`SkillBundle` named ``name``.

    Raises :class:`ValueError` (listing the available names) for an unknown
    name — propagated to the CLI layer, which surfaces it; nothing installed.
    """
    try:
        return BUILTIN_SKILLS[name]
    except KeyError:
        available = ", ".join(sorted(BUILTIN_SKILLS))
        raise ValueError(
            f"unknown builtin skill {name!r}; available: {available}"
        ) from None


def _install_skill_files(skills_home, name: str, files) -> None:
    """Write a skill's ``files`` into ``skills_home/name/``.

    For each ``(relpath, content)`` pair, write ``skills_home/name/relpath``
    (creating parents, utf-8). Each file is written atomically (temp +
    ``os.replace``). Idempotent — a second call overwrites with the same
    content. It's a file drop, not a merge.
    """
    dest_dir = Path(skills_home).expanduser() / name
    for relpath, content in files:
        target = dest_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(content)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise


def _remove_skill(skills_home, name: str) -> None:
    """Best-effort remove ``skills_home/name/`` — no-op if absent.

    Swallows errors (a skill drop is fire-and-forget); a missing dir is a
    clean no-op.
    """
    target = Path(skills_home).expanduser() / name
    shutil.rmtree(target, ignore_errors=True)


class SkillHandler:
    """Installs ``skill`` artifacts into the skills home directory.

    v1 is BUILTIN-ONLY (mirrors :class:`HookHandler` / :class:`McpHandler`): a
    ``swarph-builtin`` publisher resolves to a bundled :class:`SkillBundle` and
    drops its files into ``skills_home/<name>/``. ANY other publisher is a
    published/untrusted reference and FAILS CLOSED, writing nothing
    (signed-publisher + per-class security gate is v2, scope §3.1/§4).

    Skills aren't hot-loaded mid-session, so a successful install prints a
    one-time restart/reopen activation note (like hooks).
    """

    klass = "skill"

    def __init__(self, *, skills_home=None) -> None:
        self.skills_home = (
            skills_home if skills_home is not None else _DEFAULT_SKILLS_HOME
        )

    def _canonical_bytes(self, bundle) -> bytes:
        """Deterministic installable bytes for a :class:`SkillBundle`.

        The installable content is the ``files`` tuple-of-(path, content)
        pairs; each pair is serialized as a list (JSON has no tuple) with
        compact separators, preserving file order (``sort_keys=False``).
        """
        return json.dumps(
            [list(p) for p in bundle.files],
            sort_keys=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def add(self, ref: ArtifactRef, *, assume_yes: bool, out) -> int:
        if not _is_trusted_publisher(ref.publisher):
            out(
                "swarph add: published skills are not yet trusted — only "
                "swarph-builtin in v1 (v2: signed-publisher + per-class "
                "security gate, scope §3.1/§4); nothing installed"
            )
            return 2

        # ValueError for an unknown builtin name propagates → caught at run_add.
        bundle = resolve_builtin_skill(ref.name)

        guard = _hash_guard(ref, self._canonical_bytes(bundle), out)
        if guard is not None:
            return guard

        # ---- show-before-write preview (builtin = trusted, no prompt) ----
        dest_dir = Path(self.skills_home).expanduser() / bundle.name
        out(
            f"skill: {bundle.name}  (trust={bundle.trust}, "
            f"publisher={bundle.publisher})"
        )
        if bundle.description:
            out(f"  {bundle.description}")
        out(f"skills dir → {dest_dir}")
        out("  will write:")
        for relpath, _content in bundle.files:
            out(f"    {dest_dir / relpath}")

        _install_skill_files(self.skills_home, bundle.name, bundle.files)

        out(f"installed skill '{bundle.name}' into {dest_dir}")
        out(
            "installed — restart/reopen the session once to pick up the new "
            "skill"
        )
        return 0


# --------------------------------------------------------------------------- #
# Tool handler (T3-tool) — bridge to swarph-mesh's adapter registry (4th class)
# --------------------------------------------------------------------------- #


_DEFAULT_LANES_PATH = Path.home() / ".swarph" / "tool_lanes.json"

#: The builtin adapter names swarph-mesh's registry dispatches. Kept in sync
#: with ``swarph_mesh.adapters.get_adapter`` (v0.5.0: gemini + deepseek + claude
#: + openai + grok). Only used to give an unknown-name ``ValueError`` a useful
#: "available:" list when swarph-mesh's own ``UnknownProvider`` doesn't enumerate
#: the providers.
_BUILTIN_TOOL_ADAPTERS = ("gemini", "deepseek", "claude", "openai", "grok")


def _load_lanes(path) -> dict:
    """Load ``~/.swarph/tool_lanes.json``, returning ``{}`` for a missing file.

    Mirrors :func:`_load_mcp_config`'s fail-closed contract:

    * Missing file → ``{}`` (a fine empty starting point).
    * CORRUPT JSON → ``ValueError`` — never silently return ``{}`` and let a
      later :func:`_save_lanes` overwrite the user's real-but-unparseable file.
    * VALID-yet-NON-OBJECT JSON (``[]``, ``null``, ``5`` …) → ``ValueError`` —
      a non-dict can't be merged onto and silently treating it as ``{}`` would
      destroy the user's real lane config.
    * Valid OBJECT → the parsed dict.
    """
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"tool_lanes.json at {p} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"tool_lanes.json at {p} is not a JSON object (got "
            f"{type(data).__name__}); refusing to merge onto a non-object "
            f"tool_lanes.json file"
        )
    return data


def _save_lanes(path, obj) -> None:
    """Atomically write ``obj`` as JSON to ``path`` (``indent=2``, mkdir -p).

    Temp file in the same directory then ``os.replace`` — a crash mid-write
    leaves either the old complete file or the new complete file, never a
    truncated one. Mirrors :func:`_save_mcp_config`.
    """
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(obj, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _merge_lane(config: dict, name: str, spec_dict: dict) -> dict:
    """Merge one ``name`` → ``spec_dict`` into ``config["lanes"]``.

    Mutate-and-return. Ensures ``config.setdefault("lanes", {})`` then sets
    ``config["lanes"][name] = spec_dict``. Idempotent (same name+spec twice =
    identical result). Preserves every other lane and every other top-level key.
    """
    lanes = config.setdefault("lanes", {})
    lanes[name] = spec_dict
    return config


def resolve_builtin_tool(name: str) -> tuple[str, dict]:
    """Resolve a builtin adapter ``name`` via swarph-mesh's adapter registry.

    Bridges ``swarph add swarph://tool/swarph-builtin/<name>`` to swarph-mesh.
    Returns ``(name, spec_dict)`` where ``spec_dict`` is a plain JSON-able dict
    extracted from the adapter (swarph-mesh adapters are instances exposing
    ``name`` / ``default_model`` / ``cost_per_token``, not pydantic specs).

    * swarph-mesh NOT importable → :class:`RuntimeError` with a clear
      "pip install swarph-mesh" message (NOT a bare ``ImportError``) so the
      handler can present the graceful absent-mesh path.
    * Unknown adapter name → :class:`ValueError` listing the available names.
    """
    try:
        from swarph_mesh import get_adapter
        from swarph_mesh.exceptions import UnknownProvider
    except ImportError as exc:
        raise RuntimeError(
            "tool install requires swarph-mesh — pip install swarph-mesh"
        ) from exc

    try:
        adapter = get_adapter(name, api_key="swarph-cli-introspection")
    except UnknownProvider as exc:
        available = ", ".join(_BUILTIN_TOOL_ADAPTERS)
        raise ValueError(
            f"unknown builtin tool adapter {name!r}; available: {available}"
        ) from exc

    # swarph-mesh adapters are instances (no .model_dump()); extract the
    # stable, serializable fields into a plain dict. cost_per_token(model)
    # returns (input_per_mtok, output_per_mtok) USD; record it for the
    # default model so the lane carries its $0-vs-metered economics.
    adapter_name = getattr(adapter, "name", name)
    default_model = getattr(adapter, "default_model", None)
    spec_dict: dict = {
        "name": adapter_name,
        "kind": "llm-adapter",
        "publisher": _BUILTIN_PUBLISHER,
        "default_model": default_model,
    }
    try:
        in_cost, out_cost = adapter.cost_per_token(default_model)
        spec_dict["cost_per_mtok"] = [in_cost, out_cost]
    except Exception:
        # cost is best-effort metadata; a model lookup miss must not break
        # recording the lane.
        pass
    return adapter_name, spec_dict


class ToolHandler:
    """Installs ``tool`` artifacts as available mesh lanes (4th artifact class).

    v1 is BUILTIN-ONLY (mirrors :class:`HookHandler` / :class:`McpHandler` /
    :class:`SkillHandler`): a ``swarph-builtin`` publisher resolves to a
    swarph-mesh builtin adapter via :func:`resolve_builtin_tool` and records it
    as an available ``$0-first`` lane under ``lanes/<name>`` in a local config
    the mesh reads (``~/.swarph/tool_lanes.json``). ANY other publisher is a
    published/untrusted reference and FAILS CLOSED, writing nothing
    (signed-publisher + per-class security gate is v2, scope §3.1/§4).

    "Installing a tool" RECORDS the lane only — it does NOT run the adapter's
    gate-probes (probing may spawn the wrapped CLI). ``swarph`` can probe the
    lane later.

    swarph-mesh absent is a GRACEFUL path: :func:`resolve_builtin_tool` raises
    a clear :class:`RuntimeError`, which ``add`` turns into a one-line message +
    exit code 6 (distinct: 2=fail-closed / 3=stub / 4=missing-handler /
    5=hash-mismatch / 6=missing-dep), writing nothing.
    """

    klass = "tool"

    def __init__(self, *, lanes_path=None) -> None:
        self.lanes_path = (
            lanes_path if lanes_path is not None else _DEFAULT_LANES_PATH
        )

    def _canonical_bytes(self, spec_dict) -> bytes:
        """Deterministic installable bytes for a resolved adapter spec dict.

        The installable content is the lane ``spec_dict`` recorded under
        ``lanes/<name>``; serialized with sorted keys + compact separators so
        equal specs always hash equal regardless of key order.
        """
        return json.dumps(
            spec_dict, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def add(self, ref: ArtifactRef, *, assume_yes: bool, out) -> int:
        if not _is_trusted_publisher(ref.publisher):
            out(
                "swarph add: published tools are not yet trusted — only "
                "swarph-builtin in v1 (v2: signed-publisher + per-class "
                "security gate, scope §3.1/§4); nothing installed"
            )
            return 2

        # swarph-mesh-absent → graceful (code 6, writes nothing). A ValueError
        # for an unknown builtin name propagates → caught at run_add.
        try:
            name, spec_dict = resolve_builtin_tool(ref.name)
        except RuntimeError as exc:
            out("swarph add: " + str(exc))
            return 6

        guard = _hash_guard(ref, self._canonical_bytes(spec_dict), out)
        if guard is not None:
            return guard

        lanes_path = Path(self.lanes_path).expanduser()

        # ---- show-before-write preview (builtin = trusted, no prompt) ----
        out(
            f"tool: {name}  (trust=builtin, publisher={ref.publisher}, "
            f"kind={spec_dict.get('kind')})"
        )
        if spec_dict.get("default_model"):
            out(f"  default model → {spec_dict['default_model']}")
        out(f"lane config → {lanes_path}")
        out("  will record this adapter as an available mesh lane:")
        out(f"    {name} = {json.dumps(spec_dict)}")

        # ---- load + merge FIRST (load failure aborts with nothing written) --
        config = _load_lanes(self.lanes_path)
        config = _merge_lane(config, name, spec_dict)
        _save_lanes(self.lanes_path, config)

        out(
            f"installed tool lane {name!r} to {lanes_path} — the mesh will use "
            f"it as a $0-first lane"
        )
        out(
            "lane recorded only (not probed) — run `swarph` later to probe the "
            "adapter's gates"
        )
        return 0


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


def build_registry(
    *,
    settings_path=None,
    hooks_home=None,
    mcp_config_path=None,
    skills_home=None,
    lanes_path=None,
) -> dict:
    """Build the ``klass`` → handler registry for one ``add`` invocation.

    The hook paths are threaded into :class:`HookHandler`, the ``.mcp.json``
    path into :class:`McpHandler`, the skills home into :class:`SkillHandler`,
    and the tool lane-config path into :class:`ToolHandler` (each defaulting to
    its module/cwd default when ``None``) so tests can point the whole install
    at tmp paths. All four artifact classes now have real handlers.
    """
    hook_kwargs = {}
    if settings_path is not None:
        hook_kwargs["settings_path"] = settings_path
    if hooks_home is not None:
        hook_kwargs["hooks_home"] = hooks_home

    return {
        "hook": HookHandler(**hook_kwargs),
        "mcp": McpHandler(mcp_config_path=mcp_config_path),
        "skill": SkillHandler(skills_home=skills_home),
        "tool": ToolHandler(lanes_path=lanes_path),
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


def run_add(
    argv,
    *,
    settings_path=None,
    hooks_home=None,
    mcp_config_path=None,
    skills_home=None,
    lanes_path=None,
) -> int:
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

    registry = build_registry(
        settings_path=settings_path,
        hooks_home=hooks_home,
        mcp_config_path=mcp_config_path,
        skills_home=skills_home,
        lanes_path=lanes_path,
    )

    try:
        return dispatch_add(ref, assume_yes=args.yes, out=print, registry=registry)
    except ValueError as exc:
        print(f"swarph add: {exc}", file=sys.stderr)
        return 2
