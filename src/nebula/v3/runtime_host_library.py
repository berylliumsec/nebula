"""Global Library files and derived index publication owned by the host."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import re
import stat
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Response
from pydantic import Field
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, StreamingResponse

from .artifacts import ArtifactStore, ArtifactStoreError
from .async_utils import finish_cleanup
from .database import EntityRow, RuntimeIndexStateRow
from .deployment_protocol import GLOBAL_EXECUTION_PROJECT
from .diagnostics import record_caught_exception, record_diagnostic
from .domain import Artifact, LibraryItem, NebulaModel
from .index_calls import index_digest
from .knowledge import (
    GLOBAL_LIBRARY_ARTIFACT_OWNER,
    INGESTION_VERSION,
    MAX_DOCUMENT_BYTES,
    DocumentTooLargeError,
    InvalidDocumentError,
    LibraryItemIndexError,
    UnsupportedDocumentError,
    ingest_library_item,
    library_item_summary,
    reindex_library_item,
)
from .knowledge_index import KnowledgeIndexError, KnowledgeIndexStatus
from .storage import ConflictError, NotFoundError, StoreTransaction, _row_to_entity

LIBRARY_INGEST_PATH = "/api/v1/library/items/ingest"
LIBRARY_ARTIFACT_CONTENT = re.compile(r"/api/v1/artifacts/([^/]{1,200})/content")
_ITEM = r"/api/v1/library/items/[^/]{1,200}"


def is_host_library_route(method, path):
    return bool(
        (method == "POST" and path == LIBRARY_INGEST_PATH)
        or (method == "POST" and re.fullmatch(_ITEM + "/reindex", path))
        or (method == "DELETE" and re.fullmatch(_ITEM, path))
        or (method == "GET" and path == "/api/v1/knowledge/index-status")
    )


class LibraryIngestRequest(NebulaModel):
    filename: str = Field(min_length=1, max_length=1024)
    media_type: str | None = Field(default=None, max_length=200)
    content_base64: str = Field(
        min_length=1, max_length=4 * ((MAX_DOCUMENT_BYTES + 2) // 3)
    )


class _LibraryIndex:
    """Bridge the existing ingestion helper's joined thread to host SQL.

    The existing SharedKnowledge index and write lock remain the only physical
    index owner. This adapter never opens a client or a collection.
    """

    def __init__(self, library, service):
        self.library, self.service = library, service
        self.loop = asyncio.get_running_loop()
        self.library_descriptor = service.index.library_descriptor

    def upsert_library_item(self, item, chunks):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # diagnostic-expected: index_call runs synchronous adapters in a thread.
            loop = None
        if loop is self.loop:
            raise KnowledgeIndexError(
                "Library publication requires an index worker thread"
            )
        return asyncio.run_coroutine_threadsafe(
            self.library._upsert(self.service, item, chunks), self.loop
        ).result()


class HostLibrary:
    def __init__(self, settings):
        self.settings = settings
        self.store = settings.store
        self.coordination = settings.host.coordinator.coordination
        self.artifacts = ArtifactStore(settings.host.data_root / "artifacts")
        self._without_index = asyncio.Lock()

    @property
    def knowledge(self):
        services = self.settings.host.shared_services
        return services.knowledge if services is not None else None

    def _index(self):
        service = self.knowledge
        return _LibraryIndex(self, service) if service is not None else None

    def index_status(self):
        service = self.knowledge
        if service is None:
            return KnowledgeIndexStatus(
                backend="disabled", state="disabled", model="none", total_bytes=0
            )
        return service.index.status

    @staticmethod
    def _current(row, submitted):
        if row is None or row.kind != LibraryItem.entity_kind:
            raise ConflictError("Library item was removed during indexing")
        current = _row_to_entity(row, LibraryItem)
        if (
            current.metadata.get("runtime_index_request")
            != submitted.metadata.get("runtime_index_request")
            or current.metadata.get("runtime_index_digest")
            != submitted.metadata.get("runtime_index_digest")
            or current.artifact_id != submitted.artifact_id
        ):
            raise ConflictError("A newer indexing request changed this Library item")
        return current

    async def _upsert(self, service, item, chunks):
        digest = index_digest(chunks)
        request = item.metadata.get("runtime_index_request")
        if (
            digest != item.metadata.get("runtime_index_digest")
            or not isinstance(request, str)
            or str(UUID(request)) != request
        ):
            raise ValueError("Library index content does not match its saved request")
        owner = self.settings.effect.owner_id
        async with service._writes:
            async with self.coordination._write() as session:
                current = self._current(await session.get(EntityRow, item.id), item)
                if current.revision != item.revision:
                    raise ConflictError("Library item changed before indexing")
                values = dict(
                    source_kind="library",
                    project_id=GLOBAL_EXECUTION_PROJECT,
                    runtime_id=owner,
                    fencing_token=0,
                    digest=digest,
                    request_id=request,
                    state="indexing",
                    updated_at_ms=await self.coordination._clock_ms(session),
                )
                state = await session.get(RuntimeIndexStateRow, item.id)
                if state is None:
                    session.add(RuntimeIndexStateRow(source_id=item.id, **values))
                else:
                    for name, value in values.items():
                        setattr(state, name, value)
            try:
                await finish_cleanup(
                    asyncio.to_thread(service.index.upsert_library_item, item, chunks)
                )
                async with self.coordination._write() as session:
                    self._current(await session.get(EntityRow, item.id), item)
                    state = await session.get(RuntimeIndexStateRow, item.id)
                    if state is None or (
                        state.source_kind,
                        state.runtime_id,
                        state.fencing_token,
                        state.request_id,
                        state.digest,
                        state.state,
                    ) != ("library", owner, 0, request, digest, "indexing"):
                        raise ConflictError("Library index publication was replaced")
                    state.state = "ready"
                    state.updated_at_ms = await self.coordination._clock_ms(session)
            except BaseException:  # diagnostic-expected: fail only this publication, then propagate the original failure.

                async def failed():
                    async with self.coordination._write() as session:
                        state = await session.get(
                            RuntimeIndexStateRow, item.id, with_for_update=True
                        )
                        if state is not None and (
                            state.runtime_id,
                            state.request_id,
                            state.digest,
                            state.state,
                        ) == (owner, request, digest, "indexing"):
                            state.state = "failed"
                            state.updated_at_ms = await self.coordination._clock_ms(
                                session
                            )

                await finish_cleanup(failed())
                raise

    async def owns_artifact(self, artifact_id):
        try:
            artifact = await self.store.get(Artifact, artifact_id)
        except NotFoundError:
            # diagnostic-expected: an absent artifact continues ordinary routing.
            return False
        return artifact.engagement_id == GLOBAL_LIBRARY_ARTIFACT_OWNER

    async def _artifact(self, artifact_id):
        artifact = await self.store.get(Artifact, artifact_id)
        if artifact.engagement_id != GLOBAL_LIBRARY_ARTIFACT_OWNER or isinstance(
            artifact.metadata.get("tool_call_id"), str
        ):
            raise HTTPException(404, "Library artifact is unavailable")
        recorded_upload = (
            artifact.source == "library-upload"
            and artifact.metadata.get("scope") == "library"
            and artifact.metadata.get("ingestion_version") == INGESTION_VERSION
        )
        if not recorded_upload:
            item = await self.store.find_entity(LibraryItem, "artifact_id", artifact.id)
            if (
                item is None
                or item.metadata.get("scope") != "library"
                or item.metadata.get("sha256") != artifact.sha256
                or item.metadata.get("size") != artifact.size
            ):
                raise HTTPException(404, "Library artifact is unavailable")
        return artifact

    async def artifact_content(self, artifact_id):
        await self.settings._authorized()
        artifact = await self._artifact(artifact_id)

        def verified_file():
            path = self.artifacts.path_for(artifact)
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            source = os.fdopen(descriptor, "rb")
            try:
                details = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_size != artifact.size
                    or hashlib.file_digest(source, "sha256").hexdigest()
                    != artifact.sha256
                ):
                    raise ArtifactStoreError(
                        "Library artifact failed integrity verification"
                    )
                source.seek(0)
                return path, source, details
            except BaseException:  # diagnostic-expected: close an owned descriptor before propagating verification failure.
                source.close()
                raise

        verified = None

        async def prepare():
            nonlocal verified
            verified = await asyncio.to_thread(verified_file)

        try:
            await finish_cleanup(prepare())
        except FileNotFoundError:
            raise HTTPException(
                404, "Library artifact content is unavailable"
            ) from None
        except (ArtifactStoreError, OSError) as error:
            record_caught_exception(
                "storage",
                "storage.library.artifact_verification_failed",
                "A Library artifact could not pass integrity verification.",
                error,
                stage="library-download",
            )
            raise HTTPException(
                409, "Library artifact failed integrity verification"
            ) from None
        except BaseException:  # diagnostic-expected: joined verification may finish after its viewer was cancelled.
            if verified is not None:
                verified[1].close()
            raise
        path, source, details = verified

        def content():
            try:
                while chunk := source.read(256 * 1024):
                    yield chunk
            finally:
                source.close()

        headers = FileResponse(
            path,
            media_type=artifact.media_type,
            filename=artifact.filename,
            stat_result=details,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "X-Nebula-Artifact-SHA256": artifact.sha256,
                "X-Nebula-Artifact-Bytes": str(artifact.size),
                "X-Nebula-Artifact-Truncated": "false",
            },
        ).headers
        del headers["accept-ranges"]
        return StreamingResponse(
            content(), headers=headers, background=BackgroundTask(source.close)
        )

    async def delete(self, item_id, *, expected_revision=None):
        service = self.knowledge
        lock = service._writes if service is not None else self._without_index
        async with lock:
            async with self.coordination._write() as session:
                await StoreTransaction(session).delete(
                    LibraryItem, item_id, expected_revision=expected_revision
                )
                state = await session.get(RuntimeIndexStateRow, item_id)
                if state is not None:
                    state.state = "deleted"
                    state.updated_at_ms = await self.coordination._clock_ms(session)
            if service is not None:
                try:
                    await finish_cleanup(
                        asyncio.to_thread(service.index.delete_library_item, item_id)
                    )
                except KnowledgeIndexError as error:
                    record_diagnostic(
                        "warning",
                        "knowledge",
                        "knowledge.library.delete_index_failed",
                        "A Library item was removed but its rebuildable index cleanup failed.",
                        outcome="degraded",
                        stage="knowledge-delete",
                        retryable=True,
                        safe_failure_cause="The local Chroma Library collection was unavailable.",
                        exception=error,
                    )

    def mount(self, app, authorized):
        auth = [Depends(authorized)]

        @app.get(
            "/api/v1/knowledge/index-status",
            response_model=KnowledgeIndexStatus,
            dependencies=auth,
        )
        async def index_status():
            return self.index_status()

        @app.post(
            LIBRARY_INGEST_PATH,
            response_model=LibraryItem,
            status_code=201,
            dependencies=auth,
        )
        async def ingest(request: LibraryIngestRequest):
            try:
                content = base64.b64decode(request.content_base64, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(
                    422, "content_base64 must be valid base64"
                ) from None
            if len(content) > MAX_DOCUMENT_BYTES:
                raise HTTPException(413, "document exceeds the 20 MiB limit")
            try:
                item = await ingest_library_item(
                    store=self.store,
                    artifact_store=self.artifacts,
                    filename=request.filename,
                    data=content,
                    media_type=request.media_type,
                    knowledge_index=self._index(),
                )
            except LibraryItemIndexError as error:
                record_diagnostic(
                    "warning",
                    "knowledge",
                    "knowledge.index.library_ingest_degraded",
                    "A Library item was recorded but could not be indexed; reindex it to retry.",
                    outcome="degraded",
                    stage="library-ingest",
                    retryable=True,
                    safe_failure_cause="The local Chroma index was unavailable.",
                    exception=error,
                )
                item = error.item
            except DocumentTooLargeError as error:
                raise HTTPException(413, str(error)) from None
            except UnsupportedDocumentError as error:
                raise HTTPException(415, str(error)) from None
            except InvalidDocumentError as error:
                raise HTTPException(422, str(error)) from None
            except ArtifactStoreError:
                raise HTTPException(
                    409, "Library artifact failed integrity verification"
                ) from None
            except KnowledgeIndexError:
                raise HTTPException(
                    503, "The local Library index is unavailable"
                ) from None
            return library_item_summary(item)

        @app.post(
            "/api/v1/library/items/{item_id}/reindex",
            response_model=LibraryItem,
            dependencies=auth,
        )
        async def reindex(
            item_id: str, expected_revision: int | None = Query(default=None, ge=1)
        ):
            item = await self.store.get(LibraryItem, item_id)
            if item.artifact_id is not None:
                await self._artifact(item.artifact_id)
            try:
                updated = await reindex_library_item(
                    store=self.store,
                    artifact_store=self.artifacts,
                    item_id=item_id,
                    knowledge_index=self._index(),
                    expected_revision=expected_revision,
                )
            except DocumentTooLargeError as error:
                raise HTTPException(413, str(error)) from None
            except UnsupportedDocumentError as error:
                raise HTTPException(415, str(error)) from None
            except InvalidDocumentError as error:
                raise HTTPException(422, str(error)) from None
            except ArtifactStoreError:
                raise HTTPException(
                    409, "Library artifact failed integrity verification"
                ) from None
            except KnowledgeIndexError:
                raise HTTPException(
                    503, "The local Library index is unavailable"
                ) from None
            return library_item_summary(updated)

        @app.delete(
            "/api/v1/library/items/{item_id}", status_code=204, dependencies=auth
        )
        async def delete(
            item_id: str, expected_revision: int | None = Query(default=None, ge=1)
        ):
            await self.delete(item_id, expected_revision=expected_revision)
            return Response(status_code=204)
