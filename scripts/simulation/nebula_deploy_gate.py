#!/usr/bin/env python3
"""Read the live Core before a simulation deployment touches its service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PAGE = 1_000
MAX_ITEMS = 100_000
ACTIVE_RUNS = {
    "queued",
    "planning",
    "running",
    "waiting_approval",
    "paused",
    "cancelling",
    "interrupted",
}


def service_token(environment_file: Path) -> str:
    entry = environment_file.lstat()
    if (
        not stat.S_ISREG(entry.st_mode)
        or entry.st_uid != os.getuid()
        or stat.S_IMODE(entry.st_mode) & 0o077
    ):
        raise ValueError("Nebula service environment is not private")
    for line in environment_file.read_text().splitlines():
        if line.startswith("NEBULA_V3_API_TOKEN="):
            token = line.partition("=")[2].strip().strip("\"'")
            if token and not any(char.isspace() for char in token):
                return token
    raise ValueError("Nebula service token is unavailable")


class CoreClient:
    def __init__(self, origin: str, token: str) -> None:
        if origin not in {"http://127.0.0.1:8000", "http://127.0.0.1:18000"}:
            raise ValueError("deployment gate must read a local Core")
        self.origin = origin
        self.token = token

    def get(self, path: str, **parameters: object) -> Any:
        query = f"?{urlencode(parameters)}" if parameters else ""
        request = Request(
            f"{self.origin}/api/v1/{path.lstrip('/')}{query}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise ValueError(f"Core returned HTTP {response.status} for {path}")
            return json.load(response)

    def all_entities(self, path: str, **parameters: object) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            page = self.get(path, offset=len(items), limit=PAGE, **parameters)
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                raise ValueError(f"Core returned an invalid {path} page")
            items.extend(page)
            if len(items) > MAX_ITEMS:
                raise ValueError(f"Core {path} exceeds the bounded deployment audit")
            if len(page) < PAGE:
                return items


def blockers(core: CoreClient) -> list[str]:
    """Return bounded reasons to retain a candidate; any failed read raises."""

    agents = core.get("work/agents")
    if not isinstance(agents, list):
        raise ValueError("Core Work roster has an invalid shape")
    if agents:
        return [f"{len(agents)} active Work agents"]

    projects = core.all_entities("engagements")
    reasons: list[str] = []
    for project in projects:
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("Core returned a project without an ID")
        encoded = quote(project_id, safe="")
        activity = core.get("chat/session-activity", engagement_id=project_id)
        if not isinstance(activity, list):
            raise ValueError("Core chat activity has an invalid shape")
        if any(
            not isinstance(item, dict) or item.get("state") != "idle"
            for item in activity
        ):
            reasons.append(f"{project_id}: chat pending")
        workspace = core.get(f"engagements/{encoded}/workspace/reset-status")
        if not isinstance(workspace, dict):
            raise ValueError("Core workspace status has an invalid shape")
        for field in ("active_terminal_count", "active_execution_count"):
            value = workspace.get(field)
            if type(value) is not int or value < 0:
                raise ValueError(f"Core workspace {field} has an invalid value")
            if value:
                reasons.append(f"{project_id}: {value} {field}")
        notebooks = core.get(f"engagements/{encoded}/notebooks/runs/active")
        if not isinstance(notebooks, list):
            raise ValueError("Core notebook activity has an invalid shape")
        if notebooks:
            reasons.append(f"{project_id}: {len(notebooks)} active notebook runs")
        for status in ("queued", "running", "cancelling"):
            executions = core.get(
                f"engagements/{encoded}/executions", status=status, limit=1
            )
            if not isinstance(executions, list):
                raise ValueError("Core executions have an invalid shape")
            if executions:
                reasons.append(f"{project_id}: {status} operator execution")
        runs = core.all_entities("runs", engagement_id=project_id)
        if any(run.get("status") in ACTIVE_RUNS for run in runs):
            reasons.append(f"{project_id}: active or resumable mission")
        if len(reasons) >= 50:
            return reasons
    return reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--environment-file",
        type=Path,
        default=Path("/home/simulation/.config/nebula/service.env"),
    )
    args = parser.parse_args()
    reasons = blockers(CoreClient(args.origin, service_token(args.environment_file)))
    print(json.dumps({"ready": not reasons, "blockers": reasons}, sort_keys=True))
    return 0 if not reasons else 3


__all__ = ["CoreClient", "blockers", "service_token"]


if __name__ == "__main__":
    raise SystemExit(main())
