"""The terminal holds Core's project-view lease while it shows a project."""

from __future__ import annotations

import asyncio
import json

import httpx

from nebula.terminal.shell import Shell
from nebula.terminal.state import View
from nebula.terminal.transport import AsyncApi, Settings
from nebula.terminal.view_lease import (
    VIEW_LEASE_TTL_SECONDS,
    ProjectViewLease,
    renew_delay,
)

CORE = "https://core.example:8765"
ENTRY = {
    "device_id": "device-a",
    "device_token": "synthetic-device",
    "csrf_token": "synthetic-csrf",
}
PREFIX = "/api/v1/engagements/"


def make_api(tmp_path, handler, core=CORE):
    settings = Settings(tmp_path / "chat.json", {"cores": {core: dict(ENTRY)}}, core)
    return AsyncApi(settings, transport=httpx.MockTransport(handler))


def lease_of(path: str) -> tuple[str, str]:
    project, _, lease = path.removeprefix(PREFIX).partition("/view-leases/")
    return project, lease


class Steps:
    """A sleep that lets the test release each renewal one at a time."""

    def __init__(self):
        self.delays: list[float] = []
        self.gate = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await self.gate.wait()
        self.gate.clear()

    async def wait_for(self, count: int) -> None:
        for _ in range(200):
            if len(self.delays) >= count:
                return
            await asyncio.sleep(0)
        raise AssertionError(f"expected {count} waits, saw {self.delays}")


def test_lease_is_held_renewed_with_the_same_id_and_survives_core_restart(tmp_path):
    requests: list[tuple[str, str, dict]] = []
    restarting = False

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        requests.append((request.method, request.url.path, body))
        if request.method == "DELETE":
            return httpx.Response(204)
        if restarting:
            return httpx.Response(503, json={"detail": "Core is restarting"})
        return httpx.Response(
            200,
            json={
                "lease_id": lease_of(request.url.path)[1],
                "project_id": "project-a",
                "ttl_seconds": body.get("ttl_seconds"),
                "renew_after_seconds": 30,
                "runtime": {"state": "starting"},
            },
        )

    async def run():
        nonlocal restarting
        notices: list[str] = []
        steps = Steps()
        api = make_api(tmp_path, handler)
        lease = ProjectViewLease(notices.append, sleep=steps)
        await lease.hold(api, "project-a")
        await steps.wait_for(1)
        assert steps.delays == [30]
        assert lease.runtime == "starting"
        # Core restarts: the renewal fails and is retried well inside the TTL.
        restarting = True
        steps.gate.set()
        await steps.wait_for(2)
        assert steps.delays[1] == lease.retry_seconds < VIEW_LEASE_TTL_SECONDS
        assert notices == [
            "Could not tell Core this project is open; retrying shortly."
        ]
        # Core is back: the same keyed PUT renews (or recreates) the lease.
        restarting = False
        steps.gate.set()
        await steps.wait_for(3)
        assert steps.delays[2] == 30
        held = lease.lease_id
        await lease.release()
        await api.close()
        puts = [item for item in requests if item[0] == "PUT"]
        assert len(puts) == 3
        assert {path for _m, path, _b in puts} == {
            f"{PREFIX}project-a/view-leases/{held}"
        }
        assert all(body == {"ttl_seconds": 90} for _m, _p, body in puts)
        assert requests[-1][:2] == ("DELETE", f"{PREFIX}project-a/view-leases/{held}")
        assert lease.task is None and lease.lease_id is None

    asyncio.run(run())


def test_switching_projects_releases_the_old_lease_and_same_project_keeps_it(tmp_path):
    requests: list[tuple[str, str]] = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"renew_after_seconds": 30})

    async def run():
        steps = Steps()
        api = make_api(tmp_path, handler)
        lease = ProjectViewLease(sleep=steps)
        await lease.hold(api, "project-a")
        await steps.wait_for(1)
        first = lease.lease_id
        await lease.hold(api, "project-a")
        assert lease.lease_id == first and len(requests) == 1
        await lease.hold(api, "project-b")
        await steps.wait_for(2)
        second = lease.lease_id
        assert second != first
        assert requests == [
            ("PUT", f"{PREFIX}project-a/view-leases/{first}"),
            ("DELETE", f"{PREFIX}project-a/view-leases/{first}"),
            ("PUT", f"{PREFIX}project-b/view-leases/{second}"),
        ]
        await lease.release()
        await api.close()

    asyncio.run(run())


def test_a_refused_lease_stops_and_explains_why(tmp_path):
    puts = 0

    def handler(request):
        nonlocal puts
        if request.method == "PUT":
            puts += 1
            return httpx.Response(
                409,
                json={
                    "detail": "An archived project keeps no worker; restore it to work in it"
                },
            )
        return httpx.Response(204)

    async def run():
        notices: list[str] = []
        steps = Steps()
        api = make_api(tmp_path, handler)
        lease = ProjectViewLease(notices.append, sleep=steps)
        await lease.hold(api, "project-a")
        await lease.task
        assert puts == 1 and steps.delays == []
        assert lease.runtime == "refused"
        assert "archived project keeps no worker" in notices[0]
        await lease.release()
        await api.close()

    asyncio.run(run())


def test_renew_hint_stays_inside_the_lease_lifetime():
    assert renew_delay({"renew_after_seconds": 30}) == 30
    assert renew_delay({"renew_after_seconds": 1}) == 5
    assert renew_delay({"renew_after_seconds": 600}) == VIEW_LEASE_TTL_SECONDS / 3
    assert renew_delay({"renew_after_seconds": "soon"}) == 30
    assert renew_delay(None) == 30


def test_shell_moves_the_lease_with_the_project_and_releases_it_on_core_switch(
    tmp_path,
):
    requests: list[tuple[str, str]] = []
    project_a = {"id": "project-a", "name": "Project A"}
    project_b = {"id": "project-b", "name": "Project B"}
    other = "https://other.example:8765"

    def handler(request):
        path = request.url.path
        if "/view-leases/" in path:
            requests.append((request.method, path))
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"renew_after_seconds": 30})
        if path == "/api/v1/health":
            return httpx.Response(200, json={"terminal_client_protocol": 1})
        raise AssertionError(f"unexpected {request.method} {path}")

    class UI:
        def say(self, value=""):
            pass

    async def run():
        settings = Settings(
            tmp_path / "chat.json",
            {"cores": {CORE: dict(ENTRY), other: dict(ENTRY)}},
            CORE,
        )
        api = AsyncApi(settings, transport=httpx.MockTransport(handler))
        shell = Shell(settings, ui=UI(), api=api)
        shell.view_lease.sleep = Steps()
        # A new conversation in a project is a project view.
        await shell.open_view(project_a, None)
        await shell.view_lease.sleep.wait_for(1)
        first = shell.view_lease.lease_id
        await shell.open_view(project_b, None)
        await shell.view_lease.sleep.wait_for(2)
        second = shell.view_lease.lease_id
        assert requests == [
            ("PUT", f"{PREFIX}project-a/view-leases/{first}"),
            ("DELETE", f"{PREFIX}project-a/view-leases/{first}"),
            ("PUT", f"{PREFIX}project-b/view-leases/{second}"),
        ]

        # Switching Core releases on the old Core before closing its client.
        def other_core(request):
            if request.url.path == "/api/v1/health":
                return httpx.Response(200, json={"terminal_client_protocol": 1})
            raise AssertionError(f"unexpected {request.method} {request.url.path}")

        original = AsyncApi.__init__

        def patched(self, settings, *, transport=None):
            original(self, settings, transport=httpx.MockTransport(other_core))

        AsyncApi.__init__ = patched
        try:
            await shell.connect(other)
        finally:
            AsyncApi.__init__ = original
        assert requests[-1] == ("DELETE", f"{PREFIX}project-b/view-leases/{second}")
        assert shell.view is None and shell.view_lease.task is None
        await shell.api.close()

    asyncio.run(run())
