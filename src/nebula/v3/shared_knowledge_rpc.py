"""Host-owned knowledge index and authenticated operations for retained workers."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import select

from .async_utils import finish_cleanup
from .database import EntityRow, RuntimeIndexStateRow
from .domain import KnowledgeSource, LibraryItem, NebulaModel
from .index_calls import index_digest
from .knowledge_index import KnowledgeIndexError
from .runtime_coordination import CoordinationConflict, LeaseLost, ProjectLease
from .runtime_ipc import RuntimeIpcError
from .runtime_vault import RuntimeVault
from .shared_memory_rpc import EmptyArguments, MemoryAuthority
from .storage import _row_to_entity

INDEX_OPERATION = "host.knowledge/v1"
INDEX_CONTENT_BYTES = 32 * 1024**2
_KEY = Annotated[str, Field(min_length=1, max_length=200)]
_QUERY = Annotated[str, Field(min_length=1, max_length=4000)]


def input_purpose(lease, nonce):
    digest = hashlib.sha256(
        json.dumps(asdict(lease), sort_keys=True).encode()
    ).hexdigest()
    return f"host-knowledge/v1:{digest}:{nonce}"


class BulkArguments(NebulaModel):
    reference: str = Field(pattern=r"^[a-f0-9]{64}$")
    nonce: str = Field(min_length=36, max_length=36)
    kind: Literal["engagement", "library", "tools"]


class QueryArguments(NebulaModel):
    kind: Literal["engagement", "library"]
    queries: list[_QUERY] = Field(min_length=1, max_length=8)
    limit: int = Field(ge=1, le=64, strict=True)


class DeleteArguments(NebulaModel):
    kind: Literal["engagement", "library"]
    source_id: _KEY


class EmbedArguments(NebulaModel):
    texts: list[Annotated[str, Field(max_length=8192)]] = Field(max_length=128)


class RankArguments(NebulaModel):
    query: _QUERY
    fingerprints: list[_KEY] = Field(max_length=4096)
    limit: int = Field(ge=1, le=64, strict=True)


class ScoreArguments(NebulaModel):
    pairs: list[tuple[_QUERY, Annotated[str, Field(max_length=8192)]]] = Field(
        max_length=128
    )


class SharedKnowledge:
    def __init__(self, host, index):
        self.host, self.index = host, index
        self.coordination = host.coordinator.coordination
        self.vault = RuntimeVault(
            host.data_root / "runtime" / "index-payloads",
            maximum_bytes=INDEX_CONTENT_BYTES,
        )
        self._writes = asyncio.Lock()

    def metadata(self):
        reranker = getattr(self.index, "reranker", None)
        return {
            "descriptor": dict(self.index.descriptor),
            "library_descriptor": dict(self.index.library_descriptor),
            "status": self.index.status.model_dump(mode="json"),
            "reranker": {"ready": bool(reranker.ready)}
            if reranker is not None
            else None,
        }

    @staticmethod
    def _source(row, lease, kind):
        expected = "knowledge" if kind == "engagement" else "library_items"
        if (
            row is None
            or row.kind != expected
            or (kind == "engagement" and row.engagement_id != lease.project_id)
        ):
            raise CoordinationConflict("Index source is unavailable in this project")
        return _row_to_entity(
            row, KnowledgeSource if kind == "engagement" else LibraryItem
        )

    async def _upsert(self, lease, body, kind):
        if not isinstance(body, dict) or set(body) != {"source", "chunks"}:
            raise ValueError("Invalid index content")
        source = (
            KnowledgeSource if kind == "engagement" else LibraryItem
        ).model_validate(body["source"])
        chunks = body["chunks"]
        if not isinstance(chunks, list) or not 1 <= len(chunks) <= 10000:
            raise ValueError("Invalid index chunk count")
        for chunk in chunks:
            if (
                not isinstance(chunk, dict)
                or not {"id", "text"} <= set(chunk)
                or not set(chunk) <= {"id", "text", "artifact_id", "page", "location"}
                or not isinstance(chunk["id"], str)
                or not 1 <= len(chunk["id"]) <= 200
                or not isinstance(chunk["text"], str)
                or not 1 <= len(chunk["text"]) <= 2000
            ):
                raise ValueError("Invalid index chunk")
        digest, request = (
            index_digest(chunks),
            source.metadata.get("runtime_index_request"),
        )
        if (
            source.metadata.get("runtime_index_digest") != digest
            or not isinstance(request, str)
            or str(UUID(request)) != request
        ):
            raise ValueError("Index content does not match its committed request")
        async with self._writes:
            async with self.coordination.fenced(lease) as session:
                current = self._source(
                    await session.get(EntityRow, source.id), lease, kind
                )
                if (
                    current.revision != source.revision
                    or current.metadata.get("runtime_index_request") != request
                    or current.metadata.get("runtime_index_digest") != digest
                ):
                    raise CoordinationConflict(
                        "Index request changed; use the current source revision"
                    )
                state = await session.get(RuntimeIndexStateRow, source.id)
                values = dict(
                    source_kind=kind,
                    project_id=lease.project_id,
                    runtime_id=lease.runtime_id,
                    fencing_token=lease.fencing_token,
                    digest=digest,
                    request_id=request,
                    state="indexing",
                    updated_at_ms=await self.coordination._clock_ms(session),
                )
                if state is None:
                    session.add(RuntimeIndexStateRow(source_id=source.id, **values))
                else:
                    for name, value in values.items():
                        setattr(state, name, value)
            try:
                method = (
                    self.index.upsert_source
                    if kind == "engagement"
                    else self.index.upsert_library_item
                )
                await finish_cleanup(asyncio.to_thread(method, source, chunks))
                async with self.coordination.fenced(lease) as session:
                    current = self._source(
                        await session.get(EntityRow, source.id), lease, kind
                    )
                    if (
                        current.metadata.get("runtime_index_request") != request
                        or current.metadata.get("runtime_index_digest") != digest
                    ):
                        raise CoordinationConflict(
                            "A newer index request replaced this attempt"
                        )
                    state = await session.get(RuntimeIndexStateRow, source.id)
                    if state is None or (
                        state.runtime_id,
                        state.fencing_token,
                        state.request_id,
                    ) != (lease.runtime_id, lease.fencing_token, request):
                        raise CoordinationConflict(
                            "Index publication belongs to another request"
                        )
                    state.state, state.updated_at_ms = (
                        "ready",
                        await self.coordination._clock_ms(session),
                    )
            except BaseException:

                async def failed():
                    # The physical host operation has ended. Close only its
                    # failed derived publication; never publish stale success.
                    async with self.coordination._write() as session:
                        state = await session.get(
                            RuntimeIndexStateRow, source.id, with_for_update=True
                        )
                        if state and (
                            state.runtime_id,
                            state.fencing_token,
                            state.request_id,
                            state.state,
                        ) == (
                            lease.runtime_id,
                            lease.fencing_token,
                            request,
                            "indexing",
                        ):
                            state.state = "failed"
                            state.updated_at_ms = await self.coordination._clock_ms(
                                session
                            )

                await finish_cleanup(failed())
                raise

    async def _query(self, lease, arguments):
        body = QueryArguments.model_validate(arguments)
        async with self.coordination.fenced(lease):
            pass
        method = (
            self.index.query if body.kind == "engagement" else self.index.query_library
        )
        args = (
            (lease.project_id, body.queries)
            if body.kind == "engagement"
            else (body.queries,)
        )
        matches = await finish_cleanup(
            asyncio.to_thread(method, *args, limit=body.limit)
        )
        async with self.coordination.fenced(lease) as session:
            rows = {
                row.id: row
                for row in await session.scalars(
                    select(EntityRow).where(
                        EntityRow.id.in_([item.source_id for item in matches])
                    )
                )
            }
            states = {
                row.source_id: row
                for row in await session.scalars(
                    select(RuntimeIndexStateRow).where(
                        RuntimeIndexStateRow.source_id.in_(tuple(rows))
                    )
                )
            }
            accepted = []
            for item in matches[: body.limit]:
                try:
                    current = self._source(rows.get(item.source_id), lease, body.kind)
                except CoordinationConflict:
                    # diagnostic-expected: stale or foreign index hits cannot authorize retrieval after SQL source changes.
                    continue
                digest, request = (
                    current.metadata.get("runtime_index_digest"),
                    current.metadata.get("runtime_index_request"),
                )
                state = states.get(current.id)
                if (
                    current.status != "ready"
                    or current.artifact_id != item.artifact_id
                    or item.index_digest != digest
                ):
                    continue
                if digest is not None and (
                    state is None
                    or state.state != "ready"
                    or state.digest != digest
                    or state.request_id != request
                ):
                    continue
                accepted.append(asdict(item))
            return accepted

    async def _delete(self, lease, arguments):
        body = DeleteArguments.model_validate(arguments)
        async with self._writes:
            async with self.coordination.fenced(lease) as session:
                if await session.get(EntityRow, body.source_id) is not None:
                    raise CoordinationConflict(
                        "Remove the SQL source before cleaning its derived index"
                    )
                state = await session.get(RuntimeIndexStateRow, body.source_id)
                if state is None:
                    return False
                if state.source_kind != body.kind or (
                    body.kind == "engagement" and state.project_id != lease.project_id
                ):
                    raise CoordinationConflict(
                        "Index cleanup belongs to another project"
                    )
            method = (
                self.index.delete_source
                if body.kind == "engagement"
                else self.index.delete_library_item
            )
            await finish_cleanup(asyncio.to_thread(method, body.source_id))
            async with self.coordination.fenced(lease) as session:
                state = await session.get(RuntimeIndexStateRow, body.source_id)
                if state is not None:
                    state.state, state.updated_at_ms = (
                        "deleted",
                        await self.coordination._clock_ms(session),
                    )
            return True

    async def dispatch(self, lease, action, arguments):
        if action == "query":
            return await self._query(lease, arguments)
        if action == "delete":
            return await self._delete(lease, arguments)
        async with self.coordination.fenced(lease):
            pass
        if action == "bulk":
            body = BulkArguments.model_validate(arguments)
            if str(UUID(body.nonce)) != body.nonce:
                raise ValueError("Invalid index input identity")
            raw = await asyncio.to_thread(
                self.vault.get, body.reference, purpose=input_purpose(lease, body.nonce)
            )
            value = json.loads(raw)
            if body.kind == "tools":
                if (
                    not isinstance(value, dict)
                    or len(value) > 4096
                    or any(
                        not isinstance(key, str)
                        or not 1 <= len(key) <= 200
                        or not isinstance(doc, str)
                        or len(doc) > 20000
                        for key, doc in value.items()
                    )
                ):
                    raise ValueError("Invalid tool index input")
                return await finish_cleanup(
                    asyncio.to_thread(self.index.index_tools, value)
                )
            return await self._upsert(lease, value, body.kind)
        if action == "embed":
            body = EmbedArguments.model_validate(arguments)
            return await finish_cleanup(asyncio.to_thread(self.index.embed, body.texts))
        if action == "rank_tools":
            body = RankArguments.model_validate(arguments)
            return await finish_cleanup(
                asyncio.to_thread(
                    self.index.rank_tools,
                    body.query,
                    body.fingerprints,
                    limit=body.limit,
                )
            )
        if action == "rerank":
            body = ScoreArguments.model_validate(arguments)
            if getattr(self.index, "reranker", None) is None:
                raise KnowledgeIndexError("The host relevance model is unavailable")
            return await finish_cleanup(
                asyncio.to_thread(self.index.reranker.score, body.pairs)
            )
        EmptyArguments.model_validate(arguments)
        if action == "metadata":
            return None
        if action == "prepare_model":
            return await finish_cleanup(asyncio.to_thread(self.index.prepare_model))
        if (
            action == "prepare_reranker"
            and getattr(self.index, "reranker", None) is not None
        ):
            self.index.reranker.ensure_started()
            return None
        raise ValueError("Unsupported shared-index operation")

    def register(self, server, runtime_id):
        async def dispatch(payload):
            try:
                if set(payload) != {"lease", "action", "arguments"}:
                    raise ValueError("Invalid index envelope")
                authority = MemoryAuthority.model_validate(payload["lease"])
                if authority.runtime_id != runtime_id:
                    raise LeaseLost("Shared index request belongs to another worker")
                value = await self.dispatch(
                    ProjectLease(**authority.model_dump()),
                    payload["action"],
                    payload["arguments"],
                )
                return {"ok": True, "value": value, "metadata": self.metadata()}
            except LeaseLost:
                # diagnostic-expected: expected lease loss crosses the private protocol as a typed rejection.
                return {
                    "ok": False,
                    "error": "lease_lost",
                    "detail": "Project ownership changed or expired",
                }
            except CoordinationConflict:
                # diagnostic-expected: expected source/revision conflicts cross the private protocol as a typed rejection.
                return {
                    "ok": False,
                    "error": "conflict",
                    "detail": "The index request changed or is unavailable in this project",
                }
            except (ValueError, ValidationError, FileNotFoundError):
                # diagnostic-expected: reject bounded input or absent encrypted payload without exposing private content.
                return {
                    "ok": False,
                    "error": "invalid",
                    "detail": "Shared index arguments or encrypted input exceed the supported bounds",
                }
            except (KnowledgeIndexError, RuntimeIpcError):
                # diagnostic-expected: domain index failures cross the private protocol as safe errors raised by the worker adapter.
                return {
                    "ok": False,
                    "error": "index",
                    "detail": "The host index could not complete this request",
                }

        async def invoke(payload):
            return await finish_cleanup(dispatch(payload))

        server.register(INDEX_OPERATION, invoke)
