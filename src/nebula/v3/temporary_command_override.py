"""Operator-scoped temporary exception while protected host runners are unavailable."""

import os


def unmanaged_commands_allowed(project_id: str | None) -> bool:
    if not project_id:
        return False
    allowed = os.environ.get("NEBULA_TEMP_UNMANAGED_COMMAND_PROJECT_IDS", "")
    return project_id in {item.strip() for item in allowed.split(",") if item.strip()}
