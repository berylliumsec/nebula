#!/usr/bin/env python3
"""Read the live Core before a simulation deployment touches its service."""

from __future__ import annotations

import argparse
import json
import os
from http.client import HTTPException
from pathlib import Path
import stat
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


PAGE = 1_000
MAX_ITEMS = 100_000
# A retiring worker answers nothing for about a second before its project
# becomes ownerless. Past this bound the read fails closed.
OWNER_READ_ATTEMPTS = 3
# Deployment status can take tens of seconds on a busy host during handoffs.
READ_TIMEOUT_SECONDS = 60
READ_ATTEMPTS = 4
READ_RETRY_DELAY_SECONDS = 5.0
OWNER_READ_DELAY_SECONDS = 1.0
# A promotion reads through a busy or restarting Core: a supervisor swap
# refuses connections, and a big project's handoff can exhaust a Core process's
# own database pool so that even status answers 500 for a few minutes. These
# answers are retried with backoff for about this long, then the run stops; its
# journal is saved, so the next ``promote`` resumes where it stopped.
TRANSIENT_HTTP_STATUSES = frozenset({500, 502, 503, 504})
TRANSIENT_BUDGET_SECONDS = 150.0
TRANSIENT_FIRST_DELAY_SECONDS = 2.0
TRANSIENT_MAX_DELAY_SECONDS = 20.0
RESUME_HINT = (
    "the deployment journal is saved; re-run `nebula_deployer promote` to resume"
)
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


class CoreUnavailable(RuntimeError):
    """Core kept answering a transient failure past the retry bound."""


def transient_reason(error: BaseException) -> str | None:
    """Describe a failure a busy or restarting Core recovers from; else None.

    4xx answers and other HTTP statuses are real answers and are never retried.
    """
    if isinstance(error, HTTPError):
        if error.code in TRANSIENT_HTTP_STATUSES:
            return f"HTTP {error.code}"
        return None
    if isinstance(error, (TimeoutError, URLError, ConnectionError, HTTPException)):
        reason = getattr(error, "reason", None) or error
        return f"{type(error).__name__}: {reason}"[:200]
    return None


def _log(message: str) -> None:
    print(f"simulation Nebula deployer: {message}", file=sys.stderr, flush=True)


def tolerate_transient(
    call: Callable[[], Any],
    description: str,
    *,
    budget_seconds: float | None = None,
) -> Any:
    """Run ``call``, retrying transient Core failures with bounded backoff.

    Only callers whose request is safe to repeat use this: reads, and
    deployment operations re-sent under their saved operation ID. A
    non-transient failure is raised at once; a transient one that outlasts the
    bound raises ``CoreUnavailable`` with the resume instruction.
    """
    budget = TRANSIENT_BUDGET_SECONDS if budget_seconds is None else budget_seconds
    started = time.monotonic()
    delay = TRANSIENT_FIRST_DELAY_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            return call()
        except (OSError, HTTPException) as error:
            reason = transient_reason(error)
            if reason is None:
                raise
            elapsed = time.monotonic() - started
            if elapsed + delay > budget:
                raise CoreUnavailable(
                    f"{description} kept failing for {elapsed:.0f} s over "
                    f"{attempt} attempts (last: {reason}); {RESUME_HINT}"
                ) from error
            _log(
                f"{description} failed ({reason}); retrying in {delay:.0f} s "
                f"(attempt {attempt}, {elapsed:.0f} of {budget:.0f} s)"
            )
            time.sleep(delay)
            delay = min(delay * 2, TRANSIENT_MAX_DELAY_SECONDS)


class CoreClient:
    def __init__(self, origin: str, token: str) -> None:
        if origin not in {"http://127.0.0.1:8000", "http://127.0.0.1:18000"}:
            raise ValueError("deployment gate must read a local Core")
        self.origin = origin
        self.token = token

    def get(self, path: str, **parameters: object) -> Any:
        """Read Core; a read that times out or cannot connect is asked again.

        Reads change nothing, so repeating one is safe. Deployment status lists
        every project and can take tens of seconds while handoffs run; one slow
        read must not end a deployment that resumes from its journal anyway.
        """
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return self._get_once(path, parameters)
            except HTTPError:
                raise
            except (TimeoutError, URLError, ConnectionError):
                # diagnostic-expected: a slow or briefly unreachable Core is read again; the last failure is raised.
                if attempt == READ_ATTEMPTS:
                    raise
                time.sleep(READ_RETRY_DELAY_SECONDS * attempt)
        raise AssertionError("unreachable")

    def read(self, path: str, **parameters: object) -> Any:
        """Read Core through transient failures, for a promotion in progress.

        Unlike ``get``, HTTP 500, 502, 503 and 504 are asked again too, with
        backoff, for about ``TRANSIENT_BUDGET_SECONDS``. Callers that read a
        503 as an answer (an ownerless project, for example) use ``get``.
        """
        return tolerate_transient(
            lambda: self._get_once(path, parameters), f"Core read {path}"
        )

    def _get_once(self, path: str, parameters: dict[str, object]) -> Any:
        query = f"?{urlencode(parameters)}" if parameters else ""
        request = Request(
            f"{self.origin}/api/v1/{path.lstrip('/')}{query}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with urlopen(request, timeout=READ_TIMEOUT_SECONDS) as response:
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


def _has_execution_owner(core: CoreClient, project_id: str) -> bool:
    status = core.get("deployment/status")
    projects = status.get("projects") if isinstance(status, dict) else None
    if not isinstance(projects, list):
        raise ValueError("Core deployment status has an invalid shape")
    return any(
        isinstance(item, dict)
        and item.get("project_id") == project_id
        and item.get("runtime_id") is not None
        for item in projects
    )


def _worker_workspace(core: CoreClient, project_id: str) -> dict[str, Any] | None:
    """Read the project's live terminal and execution counts from its worker.

    In supervisor mode reads never start a worker, so a project without an
    execution owner answers 503. Its terminals lived only in a worker, which
    could not quiesce while one was open, so there is none left to count. Its
    saved executions are read separately from SQL.
    """
    encoded = quote(project_id, safe="")
    for attempt in range(1, OWNER_READ_ATTEMPTS + 1):
        try:
            return core.get(f"engagements/{encoded}/workspace/reset-status")
        except HTTPError as error:
            if error.code != 503:
                raise
            if not _has_execution_owner(core, project_id):
                return None
            if attempt == OWNER_READ_ATTEMPTS:
                raise
        time.sleep(OWNER_READ_DELAY_SECONDS)
    raise AssertionError("unreachable")


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
        workspace = _worker_workspace(core, project_id)
        if workspace is not None:
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


__all__ = [
    "CoreClient",
    "CoreUnavailable",
    "blockers",
    "service_token",
    "tolerate_transient",
    "transient_reason",
]


if __name__ == "__main__":
    raise SystemExit(main())
