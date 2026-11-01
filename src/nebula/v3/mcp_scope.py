"""Shared visibility for MCP profiles in a selected project."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .domain import McpServerProfile
from .storage import NebulaStore


def mcp_metadata_is_visible(metadata: dict[str, Any], project_id: str) -> bool:
    owner = metadata.get("engagement_id")
    return not owner or owner == project_id


def project_mcp_profiles(
    store: NebulaStore, project_id: str, *, offset: int = 0
) -> Iterator[tuple[int, McpServerProfile]]:
    """Yield visible profiles with their raw store offsets, including past hidden pages."""

    cursor = offset
    while True:
        rows = store.list_entities(McpServerProfile, offset=cursor, limit=100)
        for profile in rows:
            row_offset = cursor
            cursor += 1
            if mcp_metadata_is_visible(profile.metadata, project_id):
                yield row_offset, profile
        if len(rows) < 100:
            return
