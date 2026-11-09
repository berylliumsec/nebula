"""Core-owned, revisioned tool knowledge for provider chats.

Native declarations are the complete representation of native tools. Deferred
definitions live in one managed instruction section, outside summarizable
history. Discovery and ranking never establish schema presence. This module
does not participate in native-harness loaded-tool tracking or context delivery.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import EntityRow, ProviderToolManifestRow
from .domain import ChatBackend, ChatSession, NebulaModel, utc_now
from .providers import ModelRequest, ModelToolResult, ToolDefinition
from .storage import ConflictError, CorruptRecordError, StoreTransaction
from .tools import ToolSpec

MANIFEST_KEY = "provider_tool_manifest"
SECTION_START = "\n\nBEGIN NEBULA MANAGED TOOLS (JSON DATA)\n"
SECTION_END = "\nEND NEBULA MANAGED TOOLS"
HINTS_START = "\n\nTool relevance hints (ranking only; no schemas): "


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def definition_digest(
    *,
    name: str,
    source_id: str | None,
    description: str,
    input_schema: Mapping[str, Any],
    strict: bool = True,
) -> str:
    """Hash the complete model-facing definition, including every constraint."""

    return hashlib.sha256(
        canonical_json(
            {
                "name": name,
                "source_id": source_id,
                "description": description,
                "input_schema": input_schema,
                "strict": strict,
            }
        ).encode("utf-8")
    ).hexdigest()


def spec_digest(spec: ToolSpec) -> str:
    return definition_digest(
        name=spec.name,
        source_id=spec.source_id,
        description=spec.description,
        input_schema=spec.input_schema,
    )


class ToolKnowledge(NebulaModel):
    # ToolSpec and ToolDefinition preserve model-facing text verbatim. The
    # general domain model's string trimming must not change these bytes.
    model_config = ConfigDict(str_strip_whitespace=False)

    name: str
    source_id: str | None = None
    description: str
    input_schema: dict[str, Any]
    strict: bool = True
    definition_digest: str
    representation: Literal["native", "deferred"]
    schema_present: bool = False
    introduction_reason: str

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            strict=self.strict,
        )


class ToolManifest(NebulaModel):
    session_id: str
    project_id: str
    revision: int = Field(default=0, ge=0)
    definitions: list[ToolKnowledge] = Field(default_factory=list)

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json(
                [item.model_dump(mode="json") for item in self.definitions]
            ).encode("utf-8")
        ).hexdigest()

    def reference(self) -> dict[str, Any]:
        return {"revision": self.revision, "digest": self.digest}

    @property
    def present_digests(self) -> dict[str, str]:
        return {
            item.name: item.definition_digest
            for item in self.definitions
            if item.schema_present
        }


def reconcile(
    previous: ToolManifest,
    specs: Mapping[str, ToolSpec],
    *,
    native: Sequence[ToolDefinition],
    deferred: Iterable[str],
    introduce: Iterable[str] = (),
    reason: str = "preload",
) -> ToolManifest:
    """A full, eligible selection replaces active state; identical state is a no-op.

    Updated loaded definitions remain present with the new complete definition.
    Removing a tool removes its active definition; historical revisions and calls
    remain immutable. A re-enabled deferred tool must be loaded again.
    """

    before = {item.name: item for item in previous.definitions}
    declared = {item.name: item for item in native}
    deferred_names, introduced = set(deferred), set(introduce)
    definitions: list[ToolKnowledge] = []
    for name in sorted(specs):
        spec = specs[name]
        if name not in declared and name not in deferred_names:
            continue
        definition = declared.get(name) or ToolDefinition(
            name=name,
            description=spec.description,
            input_schema=spec.input_schema,
        )
        digest = definition_digest(
            name=name,
            source_id=spec.source_id,
            description=definition.description,
            input_schema=definition.input_schema,
            strict=definition.strict,
        )
        prior = before.get(name)
        same = prior is not None and prior.definition_digest == digest
        present = (
            name in declared
            or name in introduced
            or bool(prior and prior.schema_present)
        )
        introduction = (
            prior.introduction_reason
            if same and prior.schema_present
            else "definition_update"
            if prior and prior.schema_present
            else "native_selection"
            if name in declared
            else reason
            if name in introduced
            else "discoverable"
        )
        definitions.append(
            ToolKnowledge(
                name=name,
                source_id=spec.source_id,
                description=definition.description,
                input_schema=definition.input_schema,
                strict=definition.strict,
                definition_digest=digest,
                representation="native" if name in declared else "deferred",
                schema_present=present,
                introduction_reason=introduction,
            )
        )
    candidate = previous.model_copy(update={"definitions": definitions}, deep=True)
    if candidate.digest != previous.digest or previous.revision == 0:
        candidate.revision = previous.revision + 1
    return candidate


def absent_candidates(
    deferred: Mapping[str, ToolSpec], manifest: ToolManifest
) -> dict[str, ToolSpec]:
    present = manifest.present_digests
    return {
        name: spec
        for name, spec in deferred.items()
        if present.get(name) != spec_digest(spec)
    }


def successful_load_names(history: Iterable[Mapping[str, Any]]) -> set[str]:
    """Only durable successful load results establish historical membership."""

    names: set[str] = set()
    for entry in history:
        if (
            entry.get("name") != "tool_catalog.load"
            or entry.get("status") != "complete"
        ):
            continue
        facts = entry.get("provider_tool_loaded")
        if isinstance(facts, Mapping):
            names.update(
                name
                for name, digest in facts.items()
                if isinstance(name, str)
                and isinstance(digest, str)
                and len(digest) == 64
                and all(char in "0123456789abcdef" for char in digest)
            )
        output = entry.get("provider_result")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except json.JSONDecodeError:
                continue
        if not isinstance(output, Mapping) or not isinstance(
            output.get("loaded"), list
        ):
            continue
        for item in output["loaded"]:
            if (
                isinstance(item, Mapping)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("input_schema"), Mapping)
                and isinstance(item.get("description"), str)
            ):
                names.add(item["name"])
    return names


def catalog_load_receipt(
    output: Mapping[str, Any],
    manifest: ToolManifest,
) -> tuple[dict[str, Any], dict[str, str]] | None:
    """Record validated raw load success before ordinary result bounding.

    Full definitions already have a durable owner, even while discoverable.
    The turn records successful names/digests; its next request introduces them
    atomically from that owner. Requested names alone never establish success.
    """

    loaded = output.get("loaded")
    if not isinstance(loaded, list) or not loaded:
        return None
    definitions = {item.name: item for item in manifest.definitions}
    facts: dict[str, str] = {}
    for item in loaded:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            return None
        definition = definitions.get(item["name"])
        if (
            definition is None
            or item.get("description") != definition.description
            or item.get("input_schema") != definition.input_schema
        ):
            return None
        facts[definition.name] = definition.definition_digest
    return {
        "loaded": [
            {"name": name, "definition_digest": digest}
            for name, digest in facts.items()
        ],
        "note": "Complete definitions are retained by Core in the managed tools section; call through tool_catalog.call.",
    }, facts


def legacy_present_names(
    snapshot: Mapping[str, Any],
    history: Iterable[Mapping[str, Any]],
) -> set[str]:
    """Initialize old sessions from durable selection and successful-load facts.

    The caller intersects these with current eligible definitions and introduces
    those full current definitions before the new request. Descriptions in old
    prose, suggestions, inferred names, failed loads and direct calls alone are
    deliberately insufficient evidence.
    """

    receipt = snapshot.get("tool_catalog") or snapshot.get("tool_suggestions") or {}
    deferred = set(receipt.get("deferred", []))
    selected = set(snapshot.get("available_tool_names", [])) - deferred
    preloaded = set(receipt.get("preloaded", [])) & deferred
    return selected | preloaded | successful_load_names(history)


def render_tools(manifest: ToolManifest) -> str:
    """One deterministic, schema-complete section; native schemas stay native."""

    deferred = [
        item for item in manifest.definitions if item.representation == "deferred"
    ]
    if not deferred:
        return ""
    loaded = [
        {
            "name": item.name,
            "source": item.source_id,
            "description": item.description,
            "input_schema": item.input_schema,
        }
        for item in deferred
        if item.schema_present
    ]
    directory = [
        {
            "name": item.name,
            "source": item.source_id,
            "purpose": " ".join(item.description.split())[:120],
        }
        for item in deferred
        if not item.schema_present
    ]
    return (
        SECTION_START
        + canonical_json(
            {
                "catalog": "Use tool_catalog.search to find tools, tool_catalog.load to load a schema, and tool_catalog.call to call a loaded tool. Loading grants no execution permission.",
                "available_not_loaded": directory,
                "loaded": loaded,
            }
        )
        + SECTION_END
    )


def render_hints(receipt: Mapping[str, Any], manifest: ToolManifest) -> str:
    absent = {item.name for item in manifest.definitions if not item.schema_present}
    suggested = sorted(set(receipt.get("suggested", [])) & absent)
    source_hints = list(
        dict.fromkeys(str(item) for item in receipt.get("source_hints", []))
    )
    sources = [
        {key: item[key] for key in ("id", "name", "description") if item.get(key)}
        for item in receipt.get("sources", [])
        if isinstance(item, Mapping)
    ]
    if not suggested and not source_hints and not sources:
        return ""
    return HINTS_START + canonical_json(
        {
            "suggested_not_loaded": suggested,
            "source_hints": source_hints,
            "source_context": sources,
        }
    )


def without_tools_section(instructions: str) -> str:
    """Replace the managed section when a recovery starts from an assembled request."""

    if SECTION_START in instructions:
        before, remainder = instructions.split(SECTION_START, 1)
        _, separator, after = remainder.partition(SECTION_END)
        if not separator:
            raise CorruptRecordError("provider tools context has no closing boundary")
        instructions = before + after
    if HINTS_START in instructions:
        before, remainder = instructions.split(HINTS_START, 1)
        _, separator, after = remainder.partition("\n")
        instructions = before + (separator + after if separator else "")
    return instructions


def apply_tools_context(
    request: ModelRequest,
    manifest: ToolManifest,
    *,
    receipt: Mapping[str, Any] | None = None,
) -> ModelRequest:
    """Shared by prepare, routing, synthesis, resume and recovery."""

    return request.model_copy(
        update={
            "instructions": without_tools_section(request.instructions or "")
            + render_tools(manifest)
            + render_hints(receipt or {}, manifest),
            "metadata": {
                **request.metadata,
                "provider_tool_manifest_revision": str(manifest.revision),
                "provider_tool_manifest_digest": manifest.digest,
            },
        }
    )


def managed_load_result(
    result: ModelToolResult, manifest: ToolManifest
) -> ModelToolResult:
    """Keep replayed load outcomes while the manifest owns their full schemas."""

    if (
        result.name != "tool_catalog.load"
        or result.is_error
        or not isinstance(result.output, dict)
    ):
        return result
    loaded = result.output.get("loaded")
    if not isinstance(loaded, list):
        return result
    present = manifest.present_digests
    names = [item.get("name") for item in loaded if isinstance(item, dict)]
    if not names or any(name not in present for name in names):
        return result
    return result.model_copy(
        update={
            "output": {
                "loaded": [
                    {"name": name, "definition_digest": present[name]} for name in names
                ],
                "note": "Complete definitions are in the managed tools section; call through tool_catalog.call.",
            }
        }
    )


async def read_manifest(
    session: AsyncSession,
    *,
    project_id: str,
    session_id: str,
    revision: int | None = None,
) -> ToolManifest:
    statement = select(ProviderToolManifestRow).where(
        ProviderToolManifestRow.project_id == project_id,
        ProviderToolManifestRow.session_id == session_id,
    )
    statement = (
        statement.where(ProviderToolManifestRow.revision == revision)
        if revision is not None
        else statement.order_by(ProviderToolManifestRow.revision.desc()).limit(1)
    )
    row = await session.scalar(statement)
    if row is None:
        if revision not in (None, 0):
            raise CorruptRecordError("provider tool manifest revision is missing")
        return ToolManifest(project_id=project_id, session_id=session_id)
    manifest = ToolManifest(
        project_id=row.project_id,
        session_id=row.session_id,
        revision=row.revision,
        definitions=[ToolKnowledge.model_validate(item) for item in row.definitions],
    )
    if manifest.digest != row.digest:
        raise CorruptRecordError("provider tool manifest digest does not match")
    return manifest


async def stage_manifest(
    transaction: StoreTransaction,
    owner: ChatSession,
    candidate: ToolManifest,
) -> ToolManifest:
    """Append once under the session lock, in the request's own transaction."""

    if owner.backend != ChatBackend.PROVIDER:
        raise ValueError("native harnesses do not use provider tool manifests")
    if owner.id != candidate.session_id or owner.engagement_id != candidate.project_id:
        raise ValueError("provider tool manifest owner differs")
    await transaction.session.flush()
    # SQLite's caller uses BEGIN IMMEDIATE. PostgreSQL serializes writers for
    # this session without preventing unrelated sessions from making progress.
    locked = await transaction.session.scalar(
        select(EntityRow)
        .where(
            EntityRow.id == owner.id,
            EntityRow.kind == ChatSession.entity_kind,
            EntityRow.engagement_id == owner.engagement_id,
        )
        .with_for_update()
    )
    if locked is None:
        raise ConflictError("provider tool manifest session is missing")
    current = await read_manifest(
        transaction.session, project_id=owner.engagement_id, session_id=owner.id
    )
    if current.digest == candidate.digest and current.revision:
        return current
    if candidate.revision != current.revision + 1:
        raise ConflictError(
            "provider tool knowledge changed before the request was saved"
        )
    transaction.session.add(
        ProviderToolManifestRow(
            session_id=owner.id,
            project_id=owner.engagement_id,
            revision=candidate.revision,
            definitions=[
                item.model_dump(mode="json") for item in candidate.definitions
            ],
            digest=candidate.digest,
            created_at=utc_now(),
        )
    )
    await transaction.session.flush()
    return candidate
