"""Nonsecret harness discovery and browser-owned credential attachment."""

from __future__ import annotations

from typing import Any

from .domain import (
    HarnessAuthMode,
    HarnessConnectionMode,
    HarnessKind,
    HarnessProfile,
    RiskClass,
)
from .storage import NebulaStore
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_PAGE = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_HARNESS = {
    "type": "object",
    "properties": {"harness_id": _ID},
    "required": ["harness_id"],
    "additionalProperties": False,
}
_ATTACH = {
    "type": "object",
    "properties": {
        "harness_id": _ID,
        "expected_revision": {"type": "integer", "minimum": 1},
        "credential_ref": {
            "type": "string",
            "pattern": r"^(?:vault|session):[0-9a-f]{32}$",
        },
    },
    "required": ["harness_id", "expected_revision", "credential_ref"],
    "additionalProperties": False,
}


class ButlerHarnesses:
    """Prepare exact global harness changes while leaving secrets on the browser."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(profile: HarnessProfile) -> dict[str, Any]:
        return {
            "harness_id": profile.id,
            "name": profile.name,
            "kind": profile.kind.value,
            "connection_mode": profile.connection_mode.value,
            "auth_mode": profile.auth_mode.value,
            "revision": profile.revision,
            "enabled": profile.enabled,
            "has_credential": bool(profile.secret_ref),
        }

    @staticmethod
    def _credential_mode(profile: HarnessProfile) -> HarnessAuthMode:
        if profile.kind == HarnessKind.GROK_ACP:
            raise ValueError(
                "Grok ACP uses its existing vendor login; no credential can be attached"
            )
        if (
            profile.kind == HarnessKind.CLAUDE_AGENT_SDK
            and profile.native_capabilities.shell
        ):
            raise ValueError(
                "Claude native shell requires existing-session authentication"
            )
        return (
            HarnessAuthMode.ENDPOINT_BEARER
            if profile.connection_mode == HarnessConnectionMode.ENDPOINT
            else HarnessAuthMode.SECRET_REF
        )

    def harnesses(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = self.store.list_entities(HarnessProfile, offset=offset, limit=51)
        return {
            "harnesses": [self._summary(profile) for profile in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    def harness(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._summary(self.store.get(HarnessProfile, arguments["harness_id"]))

    def attach(self, arguments: dict[str, Any]) -> dict[str, Any]:
        profile = self.store.get(HarnessProfile, arguments["harness_id"])
        if profile.revision != arguments["expected_revision"]:
            raise ValueError("harness changed; read its current revision")
        mode = self._credential_mode(profile)
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "harness_attach_credential",
                "project_id": self.project_id,
                "harness_id": profile.id,
                "name": profile.name,
                "expected_revision": profile.revision,
                "credential_ref": arguments["credential_ref"],
                "auth_mode": mode.value,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.harnesses.list",
                "Page global Assistant harness identities, authentication modes and current revisions without credential values.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.harnesses,
            ),
            (
                "nebula.harnesses.harness",
                "Read one Assistant harness's nonsecret identity, authentication mode and current revision before a credential change.",
                _HARNESS,
                RiskClass.LOCAL_READ,
                self.harness,
            ),
            (
                "nebula.harnesses.attach_credential",
                "Ask the requesting browser to confirm attaching an available managed credential reference to one exact harness at its current revision. The browser sets only auth_mode and secret_ref, then reads Core back. Grok cached login and Claude native shell cannot use this action. Never request the secret value.",
                _ATTACH,
                RiskClass.WORKSPACE_WRITE,
                self.attach,
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
