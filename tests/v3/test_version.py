import sys

import pytest

from nebula.v3.version import __version__, build_metadata


def test_source_build_metadata_is_explicit_and_complete(monkeypatch):
    monkeypatch.setenv("NEBULA_BUILD_COMMIT", "abc123")
    monkeypatch.setenv("NEBULA_BUILD_TARGET", "test-target")
    monkeypatch.setenv("NEBULA_BUILD_TIMESTAMP", "2026-07-12T12:00:00Z")
    monkeypatch.setenv("NEBULA_DISTRIBUTION_CHANNEL", "qa")

    assert build_metadata() == {
        "version": __version__,
        "commit": "abc123",
        "target": "test-target",
        "build_timestamp": "2026-07-12T12:00:00Z",
        "distribution_channel": "qa",
    }


def test_frozen_core_requires_baked_build_metadata(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(RuntimeError, match="missing BUILD_INFO"):
        build_metadata()
