"""Performance diagnostics over authenticated, non-loopback HTTP to real Core."""

from __future__ import annotations
from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import hashlib
import ipaddress
import json
import secrets
import socket
import threading
import time
import zipfile
from io import BytesIO

import httpx
import pytest
import uvicorn

from nebula.v3.api import create_app
from nebula.v3.diagnostics import DiagnosticManager
from nebula.v3.storage import NebulaStore
from nebula.v3.version import build_metadata


async def test_live_core_reports_correlated_reads_exports_and_stops_its_monitor(
    tmp_path,
):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        # Route lookup only; sends no packet to this documentation address.
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    if ipaddress.ip_address(address).is_loopback:
        pytest.skip("A non-loopback LAN interface is required")
    listener = socket.socket()
    listener.bind((address, 0))
    listener.listen(128)
    origin = f"http://{address}:{listener.getsockname()[1]}"
    token = secrets.token_urlsafe(32)
    auth = {"Authorization": f"Bearer {token}"}
    manager = DiagnosticManager(tmp_path / "synthetic-core", watch_settings=False)
    store = NebulaStore(tmp_path / "synthetic-core" / "nebula.db")
    performance = store.database.performance
    performance.slow_ms = 0
    app = create_app(store, auth_token=token, diagnostic_manager=manager)
    server = uvicorn.Server(uvicorn.Config(app, host=address, log_level="error"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Synthetic LAN Core did not start"
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            assert client.get("/api/v1/diagnostics/performance").status_code == 401
            assert (
                client.get(
                    "/api/v1/diagnostics/performance",
                    headers={"Authorization": "Bearer wrong"},
                ).status_code
                == 401
            )
            created = client.post(
                "/api/v1/engagements",
                headers=auth,
                json={"name": "Synthetic telemetry project"},
            )
            assert created.status_code == 201
            listing = client.get("/api/v1/engagements", headers=auth)
            assert listing.status_code == 200
            response = client.get("/api/v1/diagnostics/performance", headers=auth)
            assert response.status_code == 200
            snapshot = response.json()
            assert snapshot["schema"] == "nebula.performance/v1"
            slow = next(
                item
                for item in snapshot["recent_slow"]
                if item["operation"] == "http.response GET /api/v1/engagements"
            )
            assert slow["request_id"] == listing.headers["x-request-id"]
            assert any(
                item["operation"].startswith("db.execute") for item in slow["phases"]
            )
            assert response.headers["cache-control"] == "private, no-store"
            assert token not in response.text
            assert "Synthetic telemetry project" not in response.text
            exported = client.post("/api/v1/diagnostics/export", headers=auth)
            assert exported.status_code == 200
            with zipfile.ZipFile(BytesIO(exported.content)) as archive:
                payload = archive.read("performance.json")
                manifest = json.loads(archive.read("SHA256SUMS.json"))
                assert (
                    manifest["sha256"]["performance.json"]
                    == hashlib.sha256(payload).hexdigest()
                )
                assert json.loads(payload)["operations"]
            # A refreshed client reads the same process's authoritative counters.
        with httpx.Client(base_url=origin, trust_env=False, timeout=10) as client:
            refreshed = client.get("/api/v1/diagnostics/performance", headers=auth)
            assert refreshed.status_code == 200
            assert refreshed.json()["uptime_seconds"] >= snapshot["uptime_seconds"]
            assert (
                client.delete(
                    f"/api/v1/engagements/{created.json()['id']}", headers=auth
                ).status_code
                == 204
            )
        receipt = {
            "origin": origin,
            "build": build_metadata(),
            "fixture": "isolated synthetic Core; normal bearer authentication; no provider calls",
            "journey": "auth rejection, create/list, request correlation, snapshot refresh, manifested export, cleanup",
        }
        (tmp_path / "performance-receipt.json").write_text(
            json.dumps(receipt, indent=2)
        )
        print(json.dumps(receipt))
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        manager.close()
        (await store.database.dispose())
        assert not thread.is_alive(), "Synthetic Core did not shut down"
    lag_before = next(
        (
            item["count"]
            for item in performance.snapshot()["operations"]
            if item["operation"] == "runtime.event_loop_lag"
        ),
        0,
    )
    time.sleep(1.1)
    lag_after = next(
        (
            item["count"]
            for item in performance.snapshot()["operations"]
            if item["operation"] == "runtime.event_loop_lag"
        ),
        0,
    )
    assert lag_after == lag_before


pytestmark = pytest.mark.anyio
