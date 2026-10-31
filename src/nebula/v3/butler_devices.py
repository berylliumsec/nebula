"""Nonsecret device discovery and requesting-browser pairing handoffs for Butler."""

from __future__ import annotations

from typing import Any

from .domain import PairedDeviceSession, RiskClass
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_PAGE = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_PAIR = {
    "type": "object",
    "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["name"],
    "additionalProperties": False,
}
_REVOKE = {
    "type": "object",
    "properties": {
        "device_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_name": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_created_at": {"type": "string", "format": "date-time"},
    },
    "required": ["device_id", "expected_name", "expected_created_at"],
    "additionalProperties": False,
}


class ButlerDevices:
    """Prepare exact device actions without exposing authentication material."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(device: PairedDeviceSession) -> dict[str, Any]:
        return {
            "device_id": device.id,
            "name": device.name,
            "created_at": device.created_at.isoformat(),
            "last_used_at": device.last_used_at.isoformat(),
            "platform": device.platform,
            "app_version": device.app_version,
        }

    def list_devices(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("offset must be from 0 to 10000")
        devices = sorted(
            self.store.find_entities(PairedDeviceSession, {"revoked_at": None}),
            key=lambda device: (device.created_at, device.id),
        )
        page = devices[offset : offset + 51]
        return {
            "devices": [self._summary(device) for device in page[:50]],
            "next_offset": offset + 50 if len(page) > 50 else None,
        }

    def pair(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = arguments["name"].strip()
        if not name:
            raise ValueError("device name must not be blank")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "device_pair",
                "project_id": self.project_id,
                "name": name,
            },
        }

    def revoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = self.store.get(PairedDeviceSession, arguments["device_id"])
        if device.revoked_at is not None:
            raise ValueError("device is already revoked; refresh the device list")
        if (
            device.name != arguments["expected_name"]
            or device.created_at.isoformat() != arguments["expected_created_at"]
        ):
            raise ValueError("device identity changed; read the current device list")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "device_revoke",
                "project_id": self.project_id,
                "device_id": device.id,
                "expected_name": device.name,
                "expected_created_at": device.created_at.isoformat(),
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.devices.list",
                "Page active paired-device names, IDs, creation times and recent use without cookies, token hashes or pairing secrets. Device access is Core-wide, so choose an exact target before revocation.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.list_devices,
            ),
            (
                "nebula.devices.pair",
                "Ask the requesting host browser to create a five-minute, single-use device pairing offer. The secret and code stay on that browser. Core refuses remote creation; do not claim the companion is paired until redemption is observed separately.",
                _PAIR,
                RiskClass.WORKSPACE_WRITE,
                self.pair,
            ),
            (
                "nebula.devices.revoke",
                "Ask the requesting browser to confirm revocation of an exact paired device after fresh Core identity checks. Revoking the current browser ends its own access.",
                _REVOKE,
                RiskClass.WORKSPACE_WRITE,
                self.revoke,
            ),
        )
        result: list[AnalysisTool] = []
        for name, description, schema, risk, handler in operations:

            async def run(
                arguments: dict[str, Any], *, selected_handler: Any = handler
            ) -> dict[str, Any]:
                return selected_handler(arguments)

            result.append(
                AnalysisTool(
                    ToolSpec(
                        name=name,
                        description=description,
                        input_schema=schema,
                        output_schema={"type": "object", "additionalProperties": True},
                        risk_class=risk,
                        idempotency=(
                            IdempotencyBehavior.SAFE
                            if risk == RiskClass.LOCAL_READ
                            else IdempotencyBehavior.NON_IDEMPOTENT
                        ),
                    ),
                    run,
                )
            )
        return result
