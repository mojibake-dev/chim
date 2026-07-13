"""Shared pytest fixtures for the chim test suite.

The synthetic ``sample.esp`` fixture (built by ``fixtures/make_fixture.py``) is
the anchor for the whole suite: it is a minimal-but-valid Skyrim SE plugin that
walks clean (parse -> serialize is byte-identical, every GRUP consumes exactly
its children). Exposing its path and bytes here keeps individual test modules
from each re-deriving the path.

These fixtures are additive: test modules that define their own ``raw`` /
``fixture_path`` locally simply shadow these, so nothing here changes existing
behavior.
"""

from __future__ import annotations

import os

import pytest

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "sample.esp")


@pytest.fixture(scope="session")
def fixture_path() -> str:
    """Absolute path to the on-disk ``sample.esp`` fixture."""
    return FIXTURE_PATH


@pytest.fixture(scope="session")
def raw() -> bytes:
    """Raw bytes of the ``sample.esp`` fixture."""
    with open(FIXTURE_PATH, "rb") as fh:
        return fh.read()
