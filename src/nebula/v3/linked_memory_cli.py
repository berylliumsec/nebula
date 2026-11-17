"""``nebula-core linked-memory``: enable and inspect linked memory per project.

The commands call the running Core's authenticated API with the local
operator token, so every change goes through the project's execution worker
and its revision check, exactly as Butler's actions do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer

linked_memory_app = typer.Typer(
    name="linked-memory",
    help="Enable and inspect linked memory (background curation of completed goals).",
    no_args_is_help=True,
)

ProjectOption = Annotated[
    str, typer.Option("--project", help="Project (engagement) ID.")
]
DataDirOption = Annotated[Path | None, typer.Option("--data-dir")]


def _request(
    data_dir: Path | None, method: str, path: str, body: dict | None = None
) -> Any:
    import httpx

    from .cli import _data_dir
    from .runtime_launcher import discovery, operator_token

    root = _data_dir(data_dir)
    value = discovery(root)
    if value is None:
        raise typer.BadParameter("Core is not running for this data directory")
    headers = {"Authorization": "Bearer " + operator_token(root)}
    if method != "GET":
        headers["Idempotency-Key"] = str(uuid4())
    try:
        with httpx.Client(trust_env=False, timeout=60) as client:
            response = client.request(
                method, value["endpoint"] + path, headers=headers, json=body
            )
    except httpx.HTTPError as error:
        raise typer.BadParameter(
            f"Core request failed: {type(error).__name__}"
        ) from None
    if response.status_code == 409:
        raise typer.BadParameter(
            "Linked memory settings changed; run status and retry with its revision"
        )
    if response.status_code >= 400:
        detail = response.text[:300]
        raise typer.BadParameter(f"Core returned {response.status_code}: {detail}")
    return response.json()


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


@linked_memory_app.command("status")
def status_command(project: ProjectOption, data_dir: DataDirOption = None) -> None:
    """Show the stage, curation backlog, counts, spend and helper state."""

    _print(_request(data_dir, "GET", f"/engagements/{project}/linked-memory/status"))


@linked_memory_app.command("set")
def set_command(
    project: ProjectOption,
    stage: Annotated[str, typer.Option("--stage", help="off or shadow")],
    data_dir: DataDirOption = None,
) -> None:
    """Change a project's stage, checked against the current revision."""

    if stage not in {"off", "shadow"}:
        raise typer.BadParameter("stage must be off or shadow in this release")
    path = f"/engagements/{project}/linked-memory"
    current = _request(data_dir, "GET", path)
    saved = _request(
        data_dir,
        "PUT",
        path,
        {"stage": stage, "expected_revision": current["revision"]},
    )
    observed = _request(data_dir, "GET", path)
    if observed.get("stage") != stage:
        raise typer.BadParameter("Core did not report the saved stage on read-back")
    _print({"saved": saved, "read_back": observed})


@linked_memory_app.command("cards")
def cards_command(
    project: ProjectOption,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
    state: Annotated[str | None, typer.Option("--state")] = None,
    data_dir: DataDirOption = None,
) -> None:
    """List the newest cards with their sources and links."""

    query = f"?limit={limit}" + (f"&state={state}" if state else "")
    _print(
        _request(data_dir, "GET", f"/engagements/{project}/linked-memory/cards{query}")
    )
