"""Read the selected project's Core-owned dashboard summary on demand."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .transport import ClientError, key, terminal_text

if TYPE_CHECKING:
    from .shell import Shell


async def _all(shell: Shell, resource: str, project_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        batch = await shell.api.request(
            "GET",
            resource,
            params={"engagement_id": project_id, "limit": 1000, "offset": len(items)},
        )
        if not isinstance(batch, list):
            raise ClientError(f"Core returned an invalid {resource} list.")
        items.extend(batch)
        if len(batch) < 1000:
            return items


def _recent(item: dict[str, Any]) -> float:
    value = item.get("last_update_at")
    if not isinstance(value, str):
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


async def _current_work(
    shell: Shell, project_id: str, session_id: str | None
) -> dict[str, Any] | None:
    item: dict[str, Any] | None = None
    work_project_id = project_id
    if session_id:
        linked = await shell.api.request(
            "GET", "work/updates", params={"source_session_id": session_id, "limit": 1}
        )
        if linked:
            update = linked[0]
            work_project_id = update["engagement_id"]
            item = await shell.api.request(
                "GET",
                f"engagements/{key(work_project_id)}/work/{key(update['item_id'])}",
            )
    if item is None:
        items = await shell.api.request("GET", f"engagements/{key(project_id)}/work")
        active = sorted(
            (
                row
                for row in items
                if row.get("status") != "done" and row.get("last_update_at")
            ),
            key=lambda row: (-_recent(row), str(row.get("id", ""))),
        )
        item = next(
            (
                row
                for row in active
                if session_id
                and (
                    row.get("assignee_session_id") == session_id
                    or (
                        row.get("source_kind") == "chat"
                        and row.get("source_id") == session_id
                    )
                )
            ),
            active[0] if active else None,
        )
    if item is None:
        return None
    updates = await shell.api.request(
        "GET", f"engagements/{key(work_project_id)}/work/{key(item['id'])}/updates"
    )
    return {"item": item, "update": updates[0]} if updates else None


def _line(value: Any) -> str:
    return " ".join(terminal_text(value).split())


def _cost(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    precision = 6 if 0 < value < 0.0001 else 4 if 0 < value < 0.01 else 2
    return f"${value:.{precision}f}"


def _format(project: dict[str, Any], values: dict[str, Any]) -> str:
    lines = [f"PROJECT SNAPSHOT  ·  {_line(project.get('name') or 'Project')}", ""]
    work = values["work"]
    lines.append("CURRENT PROGRESS")
    if isinstance(work, Exception):
        lines.append("  Unavailable · refresh to retry")
    elif work is None:
        lines.append("  No Work check-in has been saved for this project.")
    else:
        item, update = work["item"], work["update"]
        lines.append(f"  {_line(item.get('title', 'Work item'))}")
        lines.append(
            f"  {_line(item.get('status', '')).replace('_', ' ').title()} · {_line(item.get('priority', 'normal'))} priority"
        )
        lines.append(f"  {_line(update.get('summary', ''))}")
        if update.get("next_step"):
            lines.append(f"  Next: {_line(update['next_step'])}")
        if update.get("blocker"):
            lines.append(f"  Blocked: {_line(update['blocker'])}")
    lines.extend(["", "PROJECT TOTALS"])
    assets = values["assets"]
    findings = values["findings"]
    runs = values["runs"]
    approvals = values["approvals"]
    lines.append(
        f"  Assets       {len(assets) if isinstance(assets, list) else 'Unavailable'}"
    )
    if isinstance(runs, list):
        run = runs[-1] if runs else None
        if run:
            metadata = run.get("metadata") or {}
            title = metadata.get("name") or run.get("objective") or "Mission"
            lines.append(
                f"  Mission      {_line(run.get('status', 'unknown')).replace('_', ' ').title()} · {_line(title)}"
            )
            total = metadata.get("total_tasks")
            if isinstance(total, int) and total > 0:
                lines.append(
                    f"  Progress     {metadata.get('completed_tasks', 0)} of {total} tasks"
                )
            lines.append(
                f"  Model cost   {_cost(metadata.get('spent_usd'))} for this mission"
            )
        else:
            lines.extend(["  Mission      No active run", "  Model cost   —"])
    else:
        lines.extend(["  Mission      Unavailable", "  Model cost   Unavailable"])
    if isinstance(findings, list):
        validated = sum(
            row.get("status") in {"validated", "confirmed"} for row in findings
        )
        priority = sum(row.get("severity") in {"critical", "high"} for row in findings)
        lines.append(
            f"  Findings     {validated} validated · {len(findings)} total · {priority} priority"
        )
    else:
        lines.append("  Findings     Unavailable")
    if isinstance(approvals, list):
        pending = sum(row.get("status") == "pending" for row in approvals)
        lines.append(f"  Approvals    {pending} waiting for review")
    else:
        lines.append("  Approvals    Unavailable")
    unavailable = [
        name for name, value in values.items() if isinstance(value, Exception)
    ]
    if unavailable:
        failure = next(
            value for value in values.values() if isinstance(value, Exception)
        )
        lines.extend(
            [
                "",
                "Could not read "
                + ", ".join(unavailable)
                + ". Other totals are current.",
                _line(str(failure)) + " · Press R or run /snapshot again to retry.",
            ]
        )
    return "\n".join(lines)


async def show_project_snapshot(shell: Shell) -> None:
    view = shell.current()
    project = view.project.copy()
    project_id = view.project_id
    session_id = view.session_id if view.session else None

    async def refresh() -> str:
        resources = {
            "work": _current_work(shell, project_id, session_id),
            "assets": _all(shell, "assets", project_id),
            "runs": _all(shell, "runs", project_id),
            "findings": _all(shell, "findings", project_id),
            "approvals": _all(shell, "approvals", project_id),
        }
        results = await asyncio.gather(*resources.values(), return_exceptions=True)
        return _format(project, dict(zip(resources, results, strict=True)))

    if hasattr(shell.ui, "begin_loading"):
        shell.ui.begin_loading("Loading project snapshot…")
    try:
        content = await refresh()
    finally:
        if hasattr(shell.ui, "clear_loading"):
            shell.ui.clear_loading()
    await shell.ui.show_snapshot(content, refresh=refresh)
