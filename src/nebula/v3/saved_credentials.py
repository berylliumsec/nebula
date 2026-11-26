"""Named saved passwords that agents can enter without reading them.

The operator saves a password once in Settings. Core keeps the value in its
encrypted credential files and stores only a ``store:`` reference here. Agents
list the names and put ``{{credential:<name>}}`` in any tool argument; the tool
brokers substitute the value only in what they hand the tool and put the
placeholder back wherever the value is echoed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import Field, SecretStr, field_validator

from .credentials import (
    CredentialCreateRequest,
    CredentialError,
    CredentialStore,
    CredentialUnavailableError,
)
from .credential_placeholders import find_placeholders, placeholder, scrub, scrub_bytes
from .diagnostics import record_caught_exception
from .domain import (
    SAVED_CREDENTIAL_NAME_PATTERN,
    NebulaModel,
    SavedCredential,
)
from .runtime_credential_access import resolve_credential_async
from .storage import NebulaStore, NotFoundError
from .tools import (
    AnalysisTool,
    GuidedToolError,
    IdempotencyBehavior,
    RiskClass,
    ToolSpec,
)

SAVED_CREDENTIALS_PATH = "/api/v1/saved-credentials"
MAX_SAVED_CREDENTIALS = 200


def saved_credential_id(name: str) -> str:
    return f"saved-credential:{name}"


class SavedCredentialCreate(NebulaModel):
    name: str = Field(pattern=SAVED_CREDENTIAL_NAME_PATTERN)
    description: str = Field(default="", max_length=500)
    secret: SecretStr

    @field_validator("secret")
    @classmethod
    def _bounded_secret(cls, value: SecretStr) -> SecretStr:
        if not 1 <= len(value.get_secret_value()) <= 16_384:
            raise ValueError("password must contain 1 to 16384 characters")
        return value


class SavedCredentialUpdate(NebulaModel):
    description: str | None = Field(default=None, max_length=500)
    # A new value replaces the old encrypted file; omit to keep the value.
    secret: SecretStr | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("secret")
    @classmethod
    def _bounded_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not 1 <= len(value.get_secret_value()) <= 16_384:
            raise ValueError("password must contain 1 to 16384 characters")
        return value


class SavedCredentialView(NebulaModel):
    """What the operator and agents see. Never the value."""

    name: str
    description: str
    placeholder: str
    available: bool
    revision: int
    created_at: datetime
    updated_at: datetime


class SavedCredentialRefused(CredentialError):
    """A placeholder named a saved password that does not exist."""


class SavedCredentialService:
    def __init__(self, store: NebulaStore, credentials: CredentialStore) -> None:
        self.store = store
        self.credentials = credentials

    async def _view(self, item: SavedCredential) -> SavedCredentialView:
        status = await asyncio.to_thread(self.credentials.status, item.reference)
        return SavedCredentialView(
            name=item.name,
            description=item.description,
            placeholder=placeholder(item.name),
            available=status.available,
            revision=item.revision,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    async def entries(self) -> list[SavedCredential]:
        items = await self.store.list_entities(
            SavedCredential, limit=MAX_SAVED_CREDENTIALS
        )
        return sorted(items, key=lambda item: item.name)

    async def list(self) -> list[SavedCredentialView]:
        return [await self._view(item) for item in await self.entries()]

    async def get(self, name: str) -> SavedCredential:
        return await self.store.get(SavedCredential, saved_credential_id(name))

    async def create(self, body: SavedCredentialCreate) -> SavedCredentialView:
        try:
            await self.get(body.name)
        except NotFoundError:
            # diagnostic-expected: a new name is not yet saved.
            pass
        else:
            raise HTTPException(409, f"a saved password named {body.name!r} exists")
        if len(await self.entries()) >= MAX_SAVED_CREDENTIALS:
            raise HTTPException(409, "Nebula keeps at most 200 saved passwords")
        status = await asyncio.to_thread(
            self.credentials.create,
            CredentialCreateRequest(secret=body.secret, persistence="store"),
        )
        try:
            item = await self.store.create(
                SavedCredential(
                    id=saved_credential_id(body.name),
                    name=body.name,
                    description=body.description.strip(),
                    reference=status.reference,
                )
            )
        except Exception:
            # The row did not land, so the encrypted value has no owner.
            await asyncio.to_thread(self.credentials.delete, status.reference)
            raise
        return await self._view(item)

    async def update(
        self, name: str, body: SavedCredentialUpdate
    ) -> SavedCredentialView:
        current = await self.get(name)
        changes: dict[str, Any] = {}
        if body.description is not None:
            changes["description"] = body.description.strip()
        replaced: str | None = None
        if body.secret is not None:
            status = await asyncio.to_thread(
                self.credentials.create,
                CredentialCreateRequest(secret=body.secret, persistence="store"),
            )
            changes["reference"] = status.reference
            replaced = current.reference
        try:
            item = await self.store.update(
                SavedCredential,
                current.id,
                changes,
                expected_revision=body.expected_revision,
            )
        except Exception:
            if "reference" in changes:
                await asyncio.to_thread(self.credentials.delete, changes["reference"])
            raise
        if replaced is not None:
            await asyncio.to_thread(self.credentials.delete, replaced)
        return await self._view(item)

    async def delete(self, name: str, *, expected_revision: int | None = None) -> None:
        current = await self.get(name)
        await self.store.delete(
            SavedCredential, current.id, expected_revision=expected_revision
        )
        await asyncio.to_thread(self.credentials.delete, current.reference)

    async def resolve(self, names: Iterable[str]) -> dict[str, str]:
        """Each named value, or a refusal naming the first unknown one."""

        resolved: dict[str, str] = {}
        for name in sorted(set(names)):
            try:
                item = await self.get(name)
            except NotFoundError:
                raise SavedCredentialRefused(
                    f"No saved password is named {name!r}. List saved passwords "
                    "with credentials.list and use one of those names."
                ) from None
            secret = await resolve_credential_async(
                self.credentials, item.reference, purpose="saved_credential"
            )
            resolved[name] = secret.get_secret_value()
        return resolved


async def saved_credentials_in(
    store: NebulaStore, credentials: CredentialStore | None, arguments: Any
) -> dict[str, str]:
    """The saved passwords a tool call's arguments name, resolved for this call.

    Raises a model-safe ``GuidedToolError`` before execution when a name is
    unknown or the encrypted store cannot be read.
    """

    names = find_placeholders(arguments)
    if not names:
        return {}
    try:
        return await SavedCredentialService(
            store, credentials or CredentialStore()
        ).resolve(names)
    except SavedCredentialRefused as exc:
        raise GuidedToolError(
            str(exc),
            code="saved_credential_refused",
            category="invalid_arguments",
            problem=str(exc),
            next_action=(
                "Call credentials.list to see the saved passwords. Do not ask "
                "for or guess the password."
            ),
            before_execution=True,
        ) from exc
    except CredentialError as exc:
        raise GuidedToolError(
            "saved password could not be read",
            code="saved_credential_unavailable",
            category="unavailable_resource",
            problem="Nebula could not read the saved password from its encrypted store.",
            next_action=(
                "Tell the operator the saved password is unavailable and ask "
                "them to save it again in Settings > Saved passwords."
            ),
            before_execution=True,
        ) from exc


def _scrub_file(path: Path | None, secrets: dict[str, str]) -> None:
    if path is None or not path.is_file():
        return
    raw = path.read_bytes()
    clean = scrub_bytes(raw, secrets)
    if clean != raw:
        path.write_bytes(clean)


def scrub_tool_result(result: Any, secrets: dict[str, str]) -> Any:
    """A ``ToolExecutionResult`` with every substituted value put back."""

    if not secrets:
        return result
    for path in (result.stdout_artifact_path, result.stderr_artifact_path):
        _scrub_file(path, secrets)
    failure = result.failure_error
    if failure is not None and not isinstance(failure, GuidedToolError):
        text = str(failure)
        if scrub(text, secrets) != text:
            failure = RuntimeError(scrub(text, secrets))
    return result.model_copy(
        update={
            "output": scrub(result.output, secrets),
            "stdout": scrub(result.stdout, secrets),
            "stderr": scrub(result.stderr, secrets),
            "artifacts": scrub(result.artifacts, secrets),
            "mcp_content_blocks": scrub(result.mcp_content_blocks, secrets),
            "parser_error": scrub(result.parser_error, secrets),
            "failure_error": failure,
        }
    )


SAVED_CREDENTIAL_LIST_TOOL = "credentials.list"
_LIST_DESCRIPTION = (
    "List the passwords the operator saved for you to enter, without their "
    "values. To enter one, put its placeholder, for example "
    "{{credential:mac-login}}, in the argument where the password belongs: "
    "text an MCP tool types or pastes into a password dialog, a run_command "
    "command (Nebula passes it as an environment variable, so keep it inside "
    "double quotes or unquoted, never single quotes, e.g. "
    "printf '%s\\n' \"{{credential:mac-login}}\" | sudo -S true), process_io "
    "input, or any other tool's argument. Nebula substitutes the value only "
    "in what it hands the tool and puts the placeholder back wherever the value "
    "is echoed. You never see the value: do not ask the operator for it, guess "
    "it, or try to reveal it, and enter it only into the prompt it is for."
)


def saved_credential_list_tool(store: NebulaStore) -> AnalysisTool:
    """The discovery tool offered to every chat and Butler turn with tools."""

    async def run(_arguments: dict[str, Any]) -> dict[str, Any]:
        current = await store.list_entities(
            SavedCredential, limit=MAX_SAVED_CREDENTIALS
        )
        return {
            "credentials": [
                {
                    "name": item.name,
                    "placeholder": placeholder(item.name),
                    "description": item.description,
                }
                for item in sorted(current, key=lambda entry: entry.name)
            ]
        }

    return AnalysisTool(
        ToolSpec(
            name=SAVED_CREDENTIAL_LIST_TOOL,
            description=_LIST_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={"type": "object", "additionalProperties": True},
            risk_class=RiskClass.LOCAL_READ,
            idempotency=IdempotencyBehavior.SAFE,
            display_name="Saved passwords",
        ),
        run,
    )


def _credential_failure(exc: CredentialError) -> HTTPException:
    record_caught_exception(
        "api",
        "api.saved_credentials.credential_failure",
        "A saved-password operation could not reach the encrypted store.",
        exc,
        stage="saved_credentials",
    )
    return HTTPException(
        503 if isinstance(exc, CredentialUnavailableError) else 409, str(exc)
    )


def mount_saved_credential_routes(
    app: FastAPI,
    service: SavedCredentialService,
    *,
    dependencies: list[Any],
) -> None:
    @app.get(
        SAVED_CREDENTIALS_PATH,
        response_model=list[SavedCredentialView],
        tags=["credentials"],
        dependencies=dependencies,
    )
    async def list_saved_credentials() -> list[SavedCredentialView]:
        return await service.list()

    @app.post(
        SAVED_CREDENTIALS_PATH,
        response_model=SavedCredentialView,
        status_code=201,
        tags=["credentials"],
        dependencies=dependencies,
    )
    async def create_saved_credential(
        body: SavedCredentialCreate,
    ) -> SavedCredentialView:
        try:
            return await service.create(body)
        except CredentialError as exc:
            raise _credential_failure(exc) from exc

    @app.patch(
        SAVED_CREDENTIALS_PATH + "/{name}",
        response_model=SavedCredentialView,
        tags=["credentials"],
        dependencies=dependencies,
    )
    async def update_saved_credential(
        name: str, body: SavedCredentialUpdate
    ) -> SavedCredentialView:
        try:
            return await service.update(name, body)
        except NotFoundError as exc:
            raise HTTPException(404, f"no saved password is named {name!r}") from exc
        except CredentialError as exc:
            raise _credential_failure(exc) from exc

    @app.delete(
        SAVED_CREDENTIALS_PATH + "/{name}",
        status_code=204,
        tags=["credentials"],
        dependencies=dependencies,
    )
    async def delete_saved_credential(
        name: str, expected_revision: int | None = None
    ) -> Response:
        try:
            await service.delete(name, expected_revision=expected_revision)
        except NotFoundError as exc:
            raise HTTPException(404, f"no saved password is named {name!r}") from exc
        except CredentialError as exc:
            raise _credential_failure(exc) from exc
        return Response(status_code=204)


__all__ = [
    "SAVED_CREDENTIALS_PATH",
    "SAVED_CREDENTIAL_LIST_TOOL",
    "saved_credential_list_tool",
    "saved_credentials_in",
    "scrub_tool_result",
    "SavedCredentialCreate",
    "SavedCredentialRefused",
    "SavedCredentialService",
    "SavedCredentialUpdate",
    "SavedCredentialView",
    "mount_saved_credential_routes",
    "saved_credential_id",
]
