#!/usr/bin/env python3
"""Capture Assistant authentication from Python API calls on isolated data.

Core lifespan stays disabled. Every request targets a harmless read or read-cursor
write; no provider/harness/tool or running service is contacted.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
from nebula.v3.api import create_app
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.database import Database
from nebula.v3.domain import ChatSession, PairedDeviceSession
from nebula.v3.storage import NebulaStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
ORIGIN = "https://nebula.test:9443"


def normalize(value):
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "<generated>"
            if key in {"created_at", "updated_at", "request_id", "error_id"}
            else normalize(item)
            for key, item in value.items()
        }
    return value


def collect_auth():
    device = PairedDeviceSession(
        id="paired",
        name="Fixture phone",
        token_sha256=sha256(b"fixture-device").hexdigest(),
        csrf_sha256=sha256(b"fixture-csrf").hexdigest(),
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=10),
        last_used_at=NOW,
        idle_expires_at=NOW + timedelta(days=1),
        absolute_expires_at=NOW + timedelta(days=60),
        metadata={"preserve": "opaque"},
        capabilities=["read-fixture"],
    )
    session = ChatSession(
        id="session",
        engagement_id="project",
        title="Fixture",
        provider_profile_id="provider",
        model="fixture",
        created_at=NOW - timedelta(days=10),
        updated_at=NOW - timedelta(days=10),
    )
    base_cookie = "nebula_device=fixture-device; nebula_csrf=fixture-csrf"
    cases = []

    def case(
        name, method="GET", headers=None, device_changes=None, *, raw_headers=None
    ):
        cases.append(
            {
                "name": name,
                "method": method,
                "headers": headers or {},
                "device_changes": device_changes or {},
                "raw_headers": raw_headers or {},
            }
        )

    case("missing")
    case("bearer", headers={"Authorization": "Bearer fixture-core"})
    case("case-insensitive-bearer", headers={"Authorization": "bEaReR fixture-core"})
    case("wrong-bearer", headers={"Authorization": "Bearer wrong"})
    case("empty-bearer", headers={"Authorization": "Bearer "})
    case("non-ascii-bearer", raw_headers={"Authorization": b"Bearer \xc3\xa9".hex()})
    case("paired-read", headers={"Cookie": base_cookie})
    case(
        "bearer-fallback-cookie",
        headers={"Cookie": base_cookie, "Authorization": "Bearer wrong"},
    )
    case(
        "quoted-cookie",
        headers={
            "Cookie": 'nebula_device="fixture\\055device"; nebula_csrf=fixture-csrf'
        },
    )
    case(
        "duplicate-cookie-last-wins",
        headers={"Cookie": "nebula_device=wrong; " + base_cookie},
    )
    case("wrong-cookie", headers={"Cookie": "nebula_device=wrong"})
    case(
        "revoked",
        headers={"Cookie": base_cookie},
        device_changes={"revoked_at": (NOW - timedelta(seconds=1)).isoformat()},
    )
    case(
        "idle-expired",
        headers={"Cookie": base_cookie},
        device_changes={"idle_expires_at": NOW.isoformat()},
    )
    case(
        "absolute-expired",
        headers={"Cookie": base_cookie},
        device_changes={
            "idle_expires_at": NOW.isoformat(),
            "absolute_expires_at": NOW.isoformat(),
        },
    )
    case("read-origin-valid", headers={"Cookie": base_cookie, "Origin": ORIGIN})
    case(
        "read-origin-wrong",
        headers={"Cookie": base_cookie, "Origin": "https://wrong.test"},
    )
    case(
        "non-ascii-origin",
        headers={"Cookie": base_cookie},
        raw_headers={"Origin": b"https://nebula.test\xc3\xa9".hex()},
    )
    case("empty-host", headers={"Cookie": base_cookie, "Host": ""})
    for bad_host in [
        "user@nebula.test:9443",
        "nebula.test/path",
        "nebula.test?query",
        "nebula.test#fragment",
    ]:
        case(
            "invalid-host-" + str(len(cases)),
            headers={"Cookie": base_cookie, "Host": bad_host},
        )
    case(
        "ipv6-host",
        headers={
            "Cookie": base_cookie,
            "Host": "[::1]:9443",
            "Origin": "https://[::1]:9443",
        },
    )
    case(
        "mutation-csrf-missing",
        "PUT",
        headers={"Cookie": base_cookie, "Origin": ORIGIN},
    )
    case(
        "mutation-csrf-wrong",
        "PUT",
        headers={"Cookie": base_cookie, "Origin": ORIGIN, "X-Nebula-CSRF": "wrong"},
    )
    case(
        "mutation-csrf-cookie-missing",
        "PUT",
        headers={
            "Cookie": "nebula_device=fixture-device",
            "Origin": ORIGIN,
            "X-Nebula-CSRF": "fixture-csrf",
        },
    )
    case(
        "mutation-origin-missing",
        "PUT",
        headers={"Cookie": base_cookie, "X-Nebula-CSRF": "fixture-csrf"},
    )
    case(
        "mutation-origin-wrong",
        "PUT",
        headers={
            "Cookie": base_cookie,
            "X-Nebula-CSRF": "fixture-csrf",
            "Origin": "https://wrong.test",
        },
    )
    case(
        "non-ascii-csrf",
        "PUT",
        headers={"Cookie": base_cookie, "Origin": ORIGIN},
        raw_headers={"X-Nebula-CSRF": b"\xc3\xa9".hex()},
    )
    case(
        "mutation-valid",
        "PUT",
        headers={
            "Cookie": base_cookie,
            "X-Nebula-CSRF": "fixture-csrf",
            "Origin": ORIGIN,
        },
    )
    case("bearer-mutation", "PUT", headers={"Authorization": "Bearer fixture-core"})
    case(
        "idle-refresh",
        headers={"Cookie": base_cookie},
        device_changes={"last_used_at": (NOW - timedelta(minutes=6)).isoformat()},
    )
    case(
        "idle-refresh-capped",
        headers={"Cookie": base_cookie},
        device_changes={
            "last_used_at": (NOW - timedelta(minutes=6)).isoformat(),
            "absolute_expires_at": (NOW + timedelta(days=2)).isoformat(),
        },
    )
    with tempfile.TemporaryDirectory(
        prefix="nebula-assistant-auth-oracle-"
    ) as directory:
        root = Path(directory)
        database = Database(root / "nebula.db")
        try:
            store = NebulaStore(database)
            store.create(session)
            store.create(device)
            app = create_app(
                store,
                artifact_store=ArtifactStore(root / "artifacts"),
                auth_token="fixture-core",
                enable_executable_missions=False,
                bootstrap_workspace=False,
            )
            # No TestClient context manager: entering it would start Core lifespan.
            client = TestClient(app, base_url=ORIGIN, raise_server_exceptions=False)
            try:
                with (
                    patch("nebula.v3.api.utc_now", return_value=NOW),
                    patch("nebula.v3.storage.utc_now", return_value=NOW),
                ):
                    for item in cases:
                        current = PairedDeviceSession.model_validate(
                            {**device.model_dump(mode="json"), **item["device_changes"]}
                        )
                        # Both implementations must start from the same canonical
                        # persisted values, not Python's pre-validation inputs.
                        item["device_before"] = current.model_dump(mode="json")
                        with sqlite3.connect(root / "nebula.db") as raw:
                            raw.execute(
                                "UPDATE entities SET payload=?,revision=1,"
                                "created_at=?,updated_at=? WHERE id=?",
                                (
                                    current.model_dump_json(),
                                    current.created_at.astimezone(
                                        timezone.utc
                                    ).strftime("%Y-%m-%d %H:%M:%S.%f"),
                                    current.updated_at.astimezone(
                                        timezone.utc
                                    ).strftime("%Y-%m-%d %H:%M:%S.%f"),
                                    device.id,
                                ),
                            )
                            raw.execute(
                                "DELETE FROM entities WHERE kind='chat_read_cursors'"
                            )
                        headers = {
                            **item["headers"],
                            **{
                                k: bytes.fromhex(v)
                                for k, v in item["raw_headers"].items()
                            },
                        }
                        body = (
                            {
                                "expected_revision": 0,
                                "device_id": "supplied-device",
                                "through_at": "2020-01-01T00:00:00Z",
                            }
                            if item["method"] == "PUT"
                            else None
                        )
                        path = "/api/v1/chat/sessions/session/" + (
                            "read-cursor" if body else "decisions"
                        )
                        response = client.request(
                            item["method"], path, headers=headers, json=body
                        )
                        item["path"] = path
                        item["body"] = body
                        item["expected"] = {
                            "status": response.status_code,
                            "body": normalize(response.json()),
                            "www_authenticate": response.headers.get(
                                "www-authenticate"
                            ),
                        }
                        item["device_after"] = normalize(
                            store.get(PairedDeviceSession, device.id).model_dump(
                                mode="json"
                            )
                        )
            finally:
                client.close()
        finally:
            database.dispose()
    return {
        "format": "nebula.assistant-auth-oracle/v1",
        "now": NOW.isoformat(),
        "origin": ORIGIN,
        "core_token": "fixture-core",
        "device_schema": PairedDeviceSession.model_json_schema(),
        "device": device.model_dump(mode="json"),
        "session": session.model_dump(mode="json"),
        "cases": cases,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in ("api", "domain", "storage")
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = collect_auth()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(result["cases"]),
                "statuses": sorted({c["expected"]["status"] for c in result["cases"]}),
            }
        )
    )


if __name__ == "__main__":
    main()
