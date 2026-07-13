"""Canonical Nebula 3 version and immutable build identity."""

from __future__ import annotations

import json
import os
import sys
import sysconfig
from importlib import resources
from typing import TypedDict

__version__ = "3.0.0-alpha.1"


class BuildMetadata(TypedDict):
    version: str
    commit: str
    target: str
    build_timestamp: str
    distribution_channel: str


def build_metadata() -> BuildMetadata:
    """Return baked release identity, or explicit development identity.

    PyInstaller builds contain ``BUILD_INFO.json`` alongside this module. A
    frozen Core without that resource is invalid and fails closed. Source-tree
    executions intentionally use visible ``development``/``unknown`` values,
    which can be overridden by build/test automation without probing Git or a
    shell at runtime.
    """

    resource = resources.files(__package__).joinpath("BUILD_INFO.json")
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if getattr(sys, "frozen", False):
            raise RuntimeError("frozen Nebula Core is missing BUILD_INFO.json")
        payload = {
            "version": __version__,
            "commit": os.getenv("NEBULA_BUILD_COMMIT", "unknown"),
            "target": os.getenv("NEBULA_BUILD_TARGET", sysconfig.get_platform()),
            "build_timestamp": os.getenv("NEBULA_BUILD_TIMESTAMP", "development"),
            "distribution_channel": os.getenv(
                "NEBULA_DISTRIBUTION_CHANNEL", "development"
            ),
        }
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("Nebula Core build metadata is unreadable") from exc

    required = (
        "version",
        "commit",
        "target",
        "build_timestamp",
        "distribution_channel",
    )
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str) or not payload[key].strip()
        for key in required
    ):
        raise RuntimeError("Nebula Core build metadata is incomplete")
    if payload["version"] != __version__:
        raise RuntimeError(
            "Nebula Core build metadata version does not match the application"
        )
    return {key: payload[key] for key in required}  # type: ignore[return-value]
