"""Nonsecret provider discovery and browser-owned credential attachment."""

from __future__ import annotations

from .async_utils import call_async

from typing import Any

from .domain import ProviderProfile, RiskClass
from .storage import NebulaStore
from .read_workers import read_in_worker
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_PAGE = {
    "type": "object",
    "properties": {"offset": {"type": "integer", "minimum": 0, "maximum": 10000}},
    "additionalProperties": False,
}
_PROFILE = {
    "type": "object",
    "properties": {"provider_id": _ID},
    "required": ["provider_id"],
    "additionalProperties": False,
}
_ATTACH = {
    "type": "object",
    "properties": {
        "provider_id": _ID,
        "expected_revision": {"type": "integer", "minimum": 1},
        "credential_ref": {
            "type": "string",
            "pattern": r"^(?:vault|session):[0-9a-f]{32}$",
        },
        "slot": {"type": "string", "enum": ["inference", "management"]},
    },
    "required": ["provider_id", "expected_revision", "credential_ref", "slot"],
    "additionalProperties": False,
}


class ButlerProviders:
    """Resolve exact global providers without sending a secret through Butler."""

    def __init__(self, store: NebulaStore, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    @staticmethod
    def _summary(profile: ProviderProfile) -> dict[str, Any]:
        return {
            "provider_id": profile.id,
            "name": profile.name,
            "provider_type": profile.provider_type,
            "revision": profile.revision,
            "enabled": profile.enabled,
            "has_inference_credential": bool(profile.secret_ref),
            "has_management_credential": bool(profile.openrouter_management_secret_ref),
        }

    async def profiles(self, arguments: dict[str, Any]) -> dict[str, Any]:
        offset = arguments.get("offset", 0)
        rows = await self.store.list_entities(ProviderProfile, offset=offset, limit=51)
        return {
            "providers": [self._summary(profile) for profile in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }

    async def profile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._summary(
            (await self.store.get(ProviderProfile, arguments["provider_id"]))
        )

    async def attach(self, arguments: dict[str, Any]) -> dict[str, Any]:
        profile = await self.store.get(ProviderProfile, arguments["provider_id"])
        if profile.revision != arguments["expected_revision"]:
            raise ValueError("provider changed; read its current revision")
        slot = arguments["slot"]
        if slot == "management" and profile.provider_type != "openrouter":
            raise ValueError("management credentials require an OpenRouter provider")
        return {
            "state": "pending_browser_apply",
            "client_action": {
                "kind": "provider_attach_credential",
                "project_id": self.project_id,
                "provider_id": profile.id,
                "name": profile.name,
                "expected_revision": profile.revision,
                "credential_ref": arguments["credential_ref"],
                "slot": slot,
            },
        }

    def actions(self) -> list[AnalysisTool]:
        operations = (
            (
                "nebula.providers.list",
                "Page global provider identities and current revisions without credential values. Read an exact profile before changing its credential.",
                _PAGE,
                RiskClass.LOCAL_READ,
                self.profiles,
            ),
            (
                "nebula.providers.profile",
                "Read one provider's nonsecret identity, revision, and credential-slot presence.",
                _PROFILE,
                RiskClass.LOCAL_READ,
                self.profile,
            ),
            (
                "nebula.providers.attach_credential",
                "Ask the requesting browser to confirm attaching an available managed credential reference to one exact provider slot at its current revision. The browser patches only that slot and reads Core back. Use the reference returned by nebula.credentials.create; never request the secret value.",
                _ATTACH,
                RiskClass.WORKSPACE_WRITE,
                self.attach,
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
                return await call_async(selected_handler, arguments)

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
