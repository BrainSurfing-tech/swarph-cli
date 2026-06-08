"""Tests for the ``feature_to_uri`` contract helper (add.py T5a).

The discover→install bridge: turn a registry/feature record dict (the shape a
search face like metaedge.surf serves) into a ``swarph://`` magnet-link URI a
user/LLM can ``swarph add``.
"""

from __future__ import annotations

import pytest

from swarph_cli.commands.add import feature_to_uri, parse_uri


# --------------------------------------------------------------------------- #
# full record → URI
# --------------------------------------------------------------------------- #


def test_full_record() -> None:
    record = {
        "name": "cell-resilience",
        "publisher": "lab-ovh",
        "artifact_class": "hook",
        "version": "1.0",
        "sha256": "abc",
    }
    assert feature_to_uri(record) == "swarph://hook/lab-ovh/cell-resilience@1.0#abc"


# --------------------------------------------------------------------------- #
# class resolution: artifact_class wins, else type→class map
# --------------------------------------------------------------------------- #


def test_type_mcp_server_maps_to_mcp() -> None:
    record = {"name": "fmp", "cell": "omega", "type": "mcp_server"}
    assert feature_to_uri(record) == "swarph://mcp/omega/fmp"


def test_type_adapter_maps_to_tool() -> None:
    record = {"name": "x", "cell": "c", "type": "adapter"}
    assert feature_to_uri(record) == "swarph://tool/c/x"


@pytest.mark.parametrize(
    "type_value,expected_class",
    [
        ("hook", "hook"),
        ("mcp", "mcp"),
        ("mcp_server", "mcp"),
        ("skill", "skill"),
        ("tool", "tool"),
        ("adapter", "tool"),
    ],
)
def test_type_to_class_table(type_value: str, expected_class: str) -> None:
    record = {"name": "n", "cell": "c", "type": type_value}
    assert feature_to_uri(record) == f"swarph://{expected_class}/c/n"


def test_artifact_class_preferred_over_type() -> None:
    record = {"name": "n", "cell": "c", "artifact_class": "skill", "type": "mcp"}
    assert feature_to_uri(record) == "swarph://skill/c/n"


# --------------------------------------------------------------------------- #
# publisher resolution: publisher > cell > default_publisher
# --------------------------------------------------------------------------- #


def test_cell_used_when_publisher_absent() -> None:
    record = {"name": "n", "cell": "omega", "type": "hook"}
    assert feature_to_uri(record) == "swarph://hook/omega/n"


def test_default_publisher_used_when_both_absent() -> None:
    record = {"name": "n", "type": "hook"}
    assert (
        feature_to_uri(record, default_publisher="fallback")
        == "swarph://hook/fallback/n"
    )


def test_publisher_wins_over_cell_and_default() -> None:
    record = {"name": "n", "publisher": "p", "cell": "c", "type": "hook"}
    assert feature_to_uri(record, default_publisher="d") == "swarph://hook/p/n"


def test_empty_publisher_falls_through_to_cell() -> None:
    record = {"name": "n", "publisher": "", "cell": "c", "type": "hook"}
    assert feature_to_uri(record) == "swarph://hook/c/n"


def test_no_publisher_no_default_raises() -> None:
    record = {"name": "n", "type": "hook"}
    with pytest.raises(ValueError, match="no publisher"):
        feature_to_uri(record)


# --------------------------------------------------------------------------- #
# sha / sha256 alias; version optional
# --------------------------------------------------------------------------- #


def test_sha_alias_used_when_sha256_absent() -> None:
    record = {"name": "n", "cell": "c", "type": "hook", "sha": "deadbeef"}
    assert feature_to_uri(record) == "swarph://hook/c/n#deadbeef"


def test_sha256_preferred_over_sha() -> None:
    record = {"name": "n", "cell": "c", "type": "hook", "sha256": "aaa", "sha": "bbb"}
    assert feature_to_uri(record) == "swarph://hook/c/n#aaa"


def test_version_optional_omitted_means_no_at() -> None:
    record = {"name": "n", "cell": "c", "type": "hook"}
    uri = feature_to_uri(record)
    assert "@" not in uri
    assert uri == "swarph://hook/c/n"


# --------------------------------------------------------------------------- #
# error paths
# --------------------------------------------------------------------------- #


def test_missing_name_raises() -> None:
    record = {"cell": "c", "type": "hook"}
    with pytest.raises(ValueError, match="name"):
        feature_to_uri(record)


def test_unmappable_type_raises() -> None:
    record = {"name": "n", "cell": "c", "type": "wormhole"}
    with pytest.raises(ValueError, match="artifact class"):
        feature_to_uri(record)


def test_absent_type_and_class_raises() -> None:
    record = {"name": "n", "cell": "c"}
    with pytest.raises(ValueError, match="artifact class"):
        feature_to_uri(record)


# --------------------------------------------------------------------------- #
# round-trip through parse_uri
# --------------------------------------------------------------------------- #


def test_round_trip_recovers_fields() -> None:
    record = {
        "name": "cell-resilience",
        "publisher": "lab-ovh",
        "artifact_class": "hook",
        "version": "1.0",
        "sha256": "abc",
    }
    ref = parse_uri(feature_to_uri(record))
    assert ref.klass == "hook"
    assert ref.publisher == "lab-ovh"
    assert ref.name == "cell-resilience"
    assert ref.version == "1.0"
    assert ref.sha256 == "abc"
