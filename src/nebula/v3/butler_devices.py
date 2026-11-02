"""Nonsecret paired-device discovery and requesting-browser handoffs."""

from __future__ import annotations

from typing import Any

from .domain import PairedDeviceSession, RiskClass
from .storage import NebulaStore
from .read_workers import read_in_worker
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_PAGE = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
_DEVICE = {
    "type": "object",
    "properties": {"device_id": {"type": "string", "minLength": 1, "maxLength": 200}},
    "required": ["device_id"],
    "additionalProperties": False,
}
_REVOKE = {
    "type": "object",
    "properties": {
        "device_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "expected_created_at": {"type": "string", "minLength": 1, "maxLength": 100},
    },
    "required": ["device_id", "expected_created_at"],
    "additionalProperties": False,
}


class ButlerDevices:
    """Keep pairing offers and device revocation on the requesting browser."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(device: PairedDeviceSession) -> dict[str, Any]:
        wire = device.model_dump(mode="json")
        return {
            "device_id": device.id,
            "name": device.name,
            "created_at": wire["created_at"],
            "last_used_at": wire["last_used_at"],
            "platform": device.platform,
            "app_version": device.app_version,
        }

    def devices(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.find_entities(
            PairedDeviceSession,
            {"revoked_at": None},
            offset=offset,
            limit=51,
        )
        return {
            "devices": [self._summary(device) for device in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def device(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = self.store.get(PairedDeviceSession, arguments["device_id"])
        if device.revoked_at is not None:
            raise ValueError("paired device has been revoked; list current devices")
        return self._summary(device)

    def pair(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": "pending_browser_apply",
            "client_action": {"kind": "device_pair", "project_id": self.project_id},
        }

    def revoke(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device = self.device(arguments)
        if device["created_at"] != arguments["expected_created_at"]:
            raise ValueError("paired device changed; read its current identity")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "device_revoke",
                "project_id": self.project_id,
                "device_id": device["device_id"],
                "name": device["name"],
                "expected_created_at": device["created_at"],
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.devices.list",
                "Page nonsecret paired-device identities. This global list is available only to interactive Butler; pairing tokens, CSRF values and browser cookies are never returned.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.devices,
            ),
            (
                "nebula.devices.device",
                "Read one currently paired device's nonsecret identity before revocation.",
                _DEVICE,
                RiskClass.LOCAL_READ,
                self.device,
            ),
            (
                "nebula.devices.pair",
                "Open Paired devices on the requesting browser. Only the Nebula host browser may create a five-minute, single-use pairing link; the link and confirmation code never enter Butler's transcript. This handoff does not create an offer.",
                _EMPTY,
                RiskClass.WORKSPACE_WRITE,
                self.pair,
            ),
            (
                "nebula.devices.revoke",
                "Ask the requesting browser to confirm revocation of one exact paired-device identity. The browser rereads Core before deletion and checks list absence afterward. Revoking the current browser uses its visible Settings control because authentication ends immediately.",
                _REVOKE,
                RiskClass.WORKSPACE_WRITE,
                self.revoke,
            ),
        )
        result: list[AnalysisTool] = []
        for name, description, schema, risk, handler in operations:

            async def run(
                arguments: dict[str, Any],
                *,
                selected_handler: Any = handler,
                selected_read: bool = risk == RiskClass.LOCAL_READ,
            ) -> dict[str, Any]:
                if selected_read:
                    return await read_in_worker(selected_handler, arguments)
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
