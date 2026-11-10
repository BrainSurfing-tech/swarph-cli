"""Card #1541: the .env open guard for tests/test_cell_selfcheck.py.

The guard itself lives in tests/conftest.py (`_no_host_env_open`, autouse,
scoped to this module and test_cell_selfcheck.py). It patches
builtins.open, io.open, os.open and pathlib.Path.open and RAISES
AssertionError BEFORE any open whose name ends in '.env'/'dsn.env' and whose
resolved path is outside the test's tmp_path. It never calls through on a
forbidden path, so no real host .env is ever opened by these tests.

The "outside" paths used here are nonexistent or dummy-fixture paths — never
a real host .env.
"""
import io
import os
from pathlib import Path

import pytest


def test_guard_allows_fixture_env_under_tmp_path(tmp_path):
    fixture = tmp_path / "fixture.env"
    fixture.write_text("SENTINEL=fixture\n", encoding="utf-8")
    assert fixture.read_text(encoding="utf-8") == "SENTINEL=fixture\n"
    with open(fixture, encoding="utf-8") as fh:
        assert "SENTINEL=fixture" in fh.read()
    with io.open(fixture, encoding="utf-8") as fh:
        assert "SENTINEL=fixture" in fh.read()
    fd = os.open(fixture, os.O_RDONLY)
    try:
        assert b"SENTINEL" in os.read(fd, 64)
    finally:
        os.close(fd)


@pytest.mark.parametrize("name", ["host.env", "dsn.env"])
def test_guard_raises_before_opening_env_outside_tmp_path(tmp_path, name):
    # Nonexistent absolute path ending in .env: the guard must raise itself,
    # before the OS is ever asked to open anything.
    outside = Path("/nonexistent-cell-fixture") / name
    with pytest.raises(AssertionError, match="forbidden .env open"):
        open(outside, encoding="utf-8")
    with pytest.raises(AssertionError, match="forbidden .env open"):
        outside.read_text(encoding="utf-8")
    with pytest.raises(AssertionError, match="forbidden .env open"):
        io.open(outside)
    with pytest.raises(AssertionError, match="forbidden .env open"):
        os.open(outside, os.O_RDONLY)


def test_guard_ignores_non_env_opens(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hello\n", encoding="utf-8")
    assert other.read_text(encoding="utf-8") == "hello\n"
