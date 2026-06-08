"""Tests for the swarph:// artifact-URI parse/format core (add.py T1)."""

from __future__ import annotations

import pytest

from swarph_cli.commands.add import (
    ARTIFACT_CLASSES,
    ArtifactRef,
    format_uri,
    parse_uri,
)


# --------------------------------------------------------------------------- #
# parse: full URIs, all 5 fields, for each class
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("klass", ARTIFACT_CLASSES)
def test_parse_full_uri_all_fields(klass: str) -> None:
    s = f"swarph://{klass}/lab-ovh/cell-resilience@1.0#a3f9c2"
    ref = parse_uri(s)
    assert ref.klass == klass
    assert ref.publisher == "lab-ovh"
    assert ref.name == "cell-resilience"
    assert ref.version == "1.0"
    assert ref.sha256 == "a3f9c2"


def test_artifact_classes_value() -> None:
    assert ARTIFACT_CLASSES == ("hook", "mcp", "skill", "tool")


# --------------------------------------------------------------------------- #
# parse: optional parts
# --------------------------------------------------------------------------- #


def test_parse_no_version() -> None:
    ref = parse_uri("swarph://tool/lab-ovh/openrouter#deadbeef")
    assert ref.name == "openrouter"
    assert ref.version is None
    assert ref.sha256 == "deadbeef"


def test_parse_no_sha() -> None:
    ref = parse_uri("swarph://tool/lab-ovh/openrouter@0.4.0")
    assert ref.name == "openrouter"
    assert ref.version == "0.4.0"
    assert ref.sha256 is None


def test_parse_neither_version_nor_sha() -> None:
    ref = parse_uri("swarph://mcp/swarph-builtin/fmp-server")
    assert ref.klass == "mcp"
    assert ref.publisher == "swarph-builtin"
    assert ref.name == "fmp-server"
    assert ref.version is None
    assert ref.sha256 is None


def test_parse_sha_without_version() -> None:
    ref = parse_uri("swarph://skill/lab-ovh/pdf-processing#deadbeef")
    assert ref.name == "pdf-processing"
    assert ref.version is None
    assert ref.sha256 == "deadbeef"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("s", ["http://x", "hook/p/n", "swarph:/hook/p/n", ""])
def test_parse_bad_scheme(s: str) -> None:
    with pytest.raises(ValueError):
        parse_uri(s)


def test_parse_unknown_class_lists_valid() -> None:
    with pytest.raises(ValueError) as exc:
        parse_uri("swarph://foo/p/n")
    msg = str(exc.value)
    for c in ARTIFACT_CLASSES:
        assert c in msg


def test_parse_too_few_segments() -> None:
    with pytest.raises(ValueError):
        parse_uri("swarph://hook/onlypublisher")


def test_parse_too_many_segments() -> None:
    with pytest.raises(ValueError):
        parse_uri("swarph://hook/p/n/extra")


def test_parse_empty_name() -> None:
    with pytest.raises(ValueError):
        parse_uri("swarph://hook/p/")


def test_parse_empty_publisher() -> None:
    with pytest.raises(ValueError):
        parse_uri("swarph://hook//n")


def test_parse_empty_version() -> None:
    with pytest.raises(ValueError):
        parse_uri("swarph://hook/p/n@")


def test_parse_empty_sha() -> None:
    with pytest.raises(ValueError):
        parse_uri("swarph://hook/p/n#")


# --------------------------------------------------------------------------- #
# round-trip
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "s",
    [
        "swarph://hook/lab-ovh/cell-resilience@1.0#a3f9c2",  # full
        "swarph://tool/lab-ovh/openrouter@0.4.0",            # version only
        "swarph://skill/lab-ovh/pdf-processing#deadbeef",    # sha only
        "swarph://mcp/swarph-builtin/fmp-server",            # bare
    ],
)
def test_round_trip(s: str) -> None:
    assert format_uri(parse_uri(s)) == s
