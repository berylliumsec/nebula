"""Hold Core's project-view lease while the terminal shows a project.

Reads never start a project's execution worker, so an idle project with no
open chat and no queued work would never load its Notes, Board or Evidence.
Like the GUI's project views, the terminal tells Core the project is open by
holding a keyed view lease (``PUT engagements/{id}/view-leases/{lease}``),
renews it while the project stays open, and releases it
(``DELETE ...``) when it leaves the project, switches Core or exits. A renewal
is a replay of the same keyed PUT, so it is safe to repeat after a lost answer
or a Core restart: Core renews the lease, or recreates it once it lapsed.
Expiry is the backstop when the release cannot run.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx

from .transport import ApiError, AsyncApi, ClientError, key

# Core accepts 30-300 seconds and defaults to 90; it asks for renewal every 30.
VIEW_LEASE_TTL_SECONDS = 90
RENEW_SECONDS = 30
# A failed or rate-limited hold is retried sooner than a renewal, well inside
# the lease's lifetime, so one lost request does not let it lapse.
RETRY_SECONDS = 10
RELEASE_TIMEOUT_SECONDS = 5
# Core refuses a lease it will never grant on retry: a deleted (404) or
# archived (409) project, or a malformed request (422).
_REFUSED = frozenset({404, 409, 422})


def lease_path(project_id: str, lease_id: str) -> str:
    return f"engagements/{key(project_id)}/view-leases/{key(lease_id)}"


def renew_delay(lease: Any) -> float:
    """Core's renew hint, kept well inside the lease's lifetime."""
    value = lease.get("renew_after_seconds") if isinstance(lease, dict) else None
    if type(value) not in {int, float} or value != value:
        return RENEW_SECONDS
    return float(min(max(value, 5), VIEW_LEASE_TTL_SECONDS / 3))


class ProjectViewLease:
    """At most one lease: the project the terminal is showing on one Core."""

    def __init__(
        self,
        notify: Callable[[str], None] | None = None,
        *,
        retry_seconds: float = RETRY_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.notify = notify or (lambda _message: None)
        self.retry_seconds = retry_seconds
        self.sleep = sleep
        self.api: AsyncApi | None = None
        self.project_id: str | None = None
        self.lease_id: str | None = None
        self.runtime: str | None = None
        self.task: asyncio.Task[None] | None = None

    async def hold(self, api: AsyncApi | None, project_id: str | None) -> None:
        """Hold the lease for this project, releasing any other project's first."""
        if (
            api is not None
            and api is self.api
            and project_id == self.project_id
            and self.task is not None
            and not self.task.done()
        ):
            return
        await self.release()
        if api is None or not project_id:
            return
        lease_id = f"terminal-{uuid4().hex}"
        self.api, self.project_id, self.lease_id = api, project_id, lease_id
        self.task = asyncio.create_task(self._keep(api, project_id, lease_id))

    async def _keep(self, api: AsyncApi, project_id: str, lease_id: str) -> None:
        reported: str | None = None
        while True:
            delay = self.retry_seconds
            try:
                lease = await api.request(
                    "PUT",
                    lease_path(project_id, lease_id),
                    json={"ttl_seconds": VIEW_LEASE_TTL_SECONDS},
                )
                delay = renew_delay(lease)
                runtime = lease.get("runtime") if isinstance(lease, dict) else None
                state = runtime.get("state") if isinstance(runtime, dict) else None
                self.runtime = state if isinstance(state, str) else None
                if state == "held" and reported != "held":
                    reason = runtime.get("reason")
                    self.notify(
                        "Core is holding this project's worker"
                        + (f" ({reason})" if isinstance(reason, str) and reason else "")
                        + "; its views load once Core admits it."
                    )
                reported = state if isinstance(state, str) else reported
            except ApiError as exc:
                if exc.status in _REFUSED:
                    self.runtime = "refused"
                    self.notify(
                        f"Core refused to keep this project open: {exc}. "
                        "Its worker will not start for this view."
                    )
                    return
                if reported != "failed":
                    self.notify(
                        "Could not tell Core this project is open; retrying shortly."
                    )
                    reported = "failed"
            except (ClientError, httpx.HTTPError, ValueError):
                if reported != "failed":
                    self.notify(
                        "Could not tell Core this project is open; retrying shortly."
                    )
                    reported = "failed"
            await self.sleep(delay)

    async def release(self) -> None:
        """End the held lease now, so Core's idle grace starts at once."""
        task, api, project_id, lease_id = (
            self.task,
            self.api,
            self.project_id,
            self.lease_id,
        )
        self.task = self.api = self.project_id = self.lease_id = None
        self.runtime = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if api is None or project_id is None or lease_id is None:
            return
        # The lease lapses on its own when the release cannot run.
        with suppress(ClientError, httpx.HTTPError, ValueError, asyncio.TimeoutError):
            await asyncio.wait_for(
                api.request("DELETE", lease_path(project_id, lease_id)),
                RELEASE_TIMEOUT_SECONDS,
            )
