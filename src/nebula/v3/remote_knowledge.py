"""Synchronous index adapter backed by the worker's async private host channel."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from concurrent.futures import Future
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from .execution_scope import current_execution_lease
from .knowledge_index import (
    IndexedKnowledgeChunk,
    KnowledgeIndexError,
    KnowledgeIndexStatus,
)
from .runtime_coordination import LeaseLost
from .runtime_ipc import RuntimeIpcError
from .runtime_vault import RuntimeVault
from .shared_knowledge_rpc import INDEX_CONTENT_BYTES, INDEX_OPERATION, input_purpose


class _RemoteReranker:
    def __init__(self, index):
        self.index = index

    @property
    def ready(self):
        return bool((self.index._metadata.get("reranker") or {}).get("ready"))

    def ensure_started(self):
        self.index._background("prepare_reranker")

    def score(self, pairs):
        return self.index._call("rerank", {"pairs": list(pairs)})


class RemoteKnowledgeIndex:
    def __init__(self, client, data_root: Path):
        self.client, self.loop = client, asyncio.get_running_loop()
        self.vault = RuntimeVault(
            data_root / "runtime" / "index-payloads", maximum_bytes=INDEX_CONTENT_BYTES
        )
        self._metadata: dict[str, Any] = {}
        self._pending: set[Future[Any]] = set()
        self._pending_lock = threading.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._reranker = _RemoteReranker(self)

    async def refresh(self):
        await self._rpc("metadata", {})

    @property
    def descriptor(self):
        return dict(self._metadata["descriptor"])

    @property
    def library_descriptor(self):
        return dict(self._metadata["library_descriptor"])

    @property
    def status(self):
        return KnowledgeIndexStatus.model_validate(self._metadata["status"])

    @property
    def reranker(self):
        return self._reranker if self._metadata.get("reranker") is not None else None

    async def _rpc(self, action, arguments, lease=None):
        lease = lease or current_execution_lease()
        if lease is None:
            raise LeaseLost("Shared indexing requires exact project ownership")
        result = await self.client.call(
            INDEX_OPERATION,
            {"lease": asdict(lease), "action": action, "arguments": arguments},
        )
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise KnowledgeIndexError("Invalid shared index result")
        if result["ok"]:
            if set(result) != {"ok", "value", "metadata"} or not isinstance(
                result["metadata"], dict
            ):
                raise KnowledgeIndexError("Invalid shared index metadata")
            self._metadata = result["metadata"]
            return result["value"]
        if result.get("error") == "lease_lost":
            raise LeaseLost("Project ownership changed or expired")
        raise KnowledgeIndexError(
            "The shared index rejected this request; read its current source and retry"
        )

    @staticmethod
    def _require_thread():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise KnowledgeIndexError(
            "Synchronous shared index calls require the index worker thread"
        )

    def _call(self, action, arguments):
        self._require_thread()
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost("Shared indexing requires exact project ownership")
        future = asyncio.run_coroutine_threadsafe(
            self._rpc(action, arguments, lease), self.loop
        )
        with self._pending_lock:
            self._pending.add(future)

        def completed(item):
            with self._pending_lock:
                self._pending.discard(item)
            if not item.cancelled():
                item.exception()

        future.add_done_callback(completed)
        try:
            return future.result(timeout=35)
        except RuntimeIpcError as error:
            raise KnowledgeIndexError(
                "The shared index is unavailable; saved source data remains intact"
            ) from error
        except TimeoutError as error:
            # The accepted host operation remains owned by that service and
            # cannot be cancelled by abandoning this thread's response view.
            raise KnowledgeIndexError(
                "The host index is still completing; read its saved source before retrying"
            ) from error

    def _background(self, action):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._call(action, {})
        else:
            if loop is not self.loop:
                raise KnowledgeIndexError("The index belongs to another worker loop")
            task = loop.create_task(self._rpc(action, {}))
            self._background_tasks.add(task)

            def completed(item):
                self._background_tasks.discard(item)
                if not item.cancelled():
                    item.exception()

            task.add_done_callback(completed)

    def _bulk(self, kind, value):
        self._require_thread()
        lease = current_execution_lease()
        if lease is None:
            raise LeaseLost("Shared indexing requires exact project ownership")
        nonce = str(uuid4())
        purpose = input_purpose(lease, nonce)
        raw = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        try:
            reference = self.vault.put(raw, purpose=purpose)
        except RuntimeIpcError as error:
            raise KnowledgeIndexError(
                "Index input exceeds its protected 32 MiB storage bound"
            ) from error
        try:
            self._call("bulk", {"kind": kind, "nonce": nonce, "reference": reference})
        finally:
            self.vault.discard(reference, purpose=purpose)

    def upsert_source(self, source, chunks):
        self._bulk(
            "engagement",
            {"source": source.model_dump(mode="json"), "chunks": list(chunks)},
        )

    def upsert_library_item(self, item, chunks):
        self._bulk(
            "library", {"source": item.model_dump(mode="json"), "chunks": list(chunks)}
        )

    def delete_source(self, source_id):
        if not self._call("delete", {"kind": "engagement", "source_id": source_id}):
            raise KnowledgeIndexError(
                "Legacy index cleanup is deferred; deleted SQL sources stay excluded from retrieval"
            )

    def delete_library_item(self, item_id):
        if not self._call("delete", {"kind": "library", "source_id": item_id}):
            raise KnowledgeIndexError(
                "Legacy index cleanup is deferred; deleted SQL items stay excluded from retrieval"
            )

    def query(self, engagement_id, queries, *, limit):
        lease = current_execution_lease()
        if lease is None or lease.project_id != engagement_id:
            raise LeaseLost("Knowledge retrieval requires its exact project context")
        return [
            IndexedKnowledgeChunk(**row)
            for row in self._call(
                "query",
                {"kind": "engagement", "queries": list(queries), "limit": limit},
            )
        ]

    def query_library(self, queries, *, limit):
        return [
            IndexedKnowledgeChunk(**row)
            for row in self._call(
                "query", {"kind": "library", "queries": list(queries), "limit": limit}
            )
        ]

    def embed(self, texts):
        return self._call("embed", {"texts": list(texts)})

    def prepare_model(self):
        self._call("prepare_model", {})

    def index_tools(self, documents):
        self._bulk("tools", dict(documents))

    def rank_tools(self, query, fingerprints, *, limit):
        return [
            tuple(item)
            for item in self._call(
                "rank_tools",
                {"query": query, "fingerprints": list(fingerprints), "limit": limit},
            )
        ]

    async def close(self):
        with self._pending_lock:
            pending = tuple(self._pending)
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(item) for item in pending), return_exceptions=True
            )
        if self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)
