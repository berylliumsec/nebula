"""Project notebook documents and persistent, mode-bound Jupyter workers."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid4, uuid5

from .automation_runtime import AutomationRuntimeManager, ContainerRuntimeSession
from .diagnostics import create_diagnostic_task, record_caught_exception
from .domain import AutomationProjectPolicy, ChatSession, Engagement, NotebookRun
from .notebook_file_index import FileIndexUnavailable, list_indexed_notebooks
from .storage import NebulaStore


MAX_NOTEBOOK_BYTES = 20_000_000
MAX_SOURCE_BYTES = 1_000_000
MAX_WARM_KERNELS = 4
NOTEBOOK_LIST_CACHE_SECONDS = 10


class NotebookError(ValueError):
    status_code = 422


class NotebookConflict(NotebookError):
    status_code = 409


class NotebookPermissionRequired(NotebookError):
    status_code = 403


class NotebookRuntimeUnavailable(NotebookError):
    status_code = 503


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _cell_source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def _source_hash(source: str) -> str:
    return _digest(source.encode("utf-8"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NotebookDocumentStore:
    def __init__(self, workspace_resolver: Callable[[str], Path]) -> None:
        self.workspace_resolver = workspace_resolver
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def path(self, engagement_id: str, relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if (
            not relative_path
            or relative.is_absolute()
            or relative.suffix.lower() != ".ipynb"
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in relative_path
            or "\x00" in relative_path
        ):
            raise NotebookError("choose a project-relative .ipynb path")
        root = self.workspace_resolver(engagement_id).expanduser().resolve(strict=True)
        candidate = root.joinpath(*relative.parts)
        if not candidate.resolve(strict=False).is_relative_to(root):
            raise NotebookError("notebook path escapes the project workspace")
        return candidate

    def lock(self, engagement_id: str, relative_path: str) -> asyncio.Lock:
        return self._locks.setdefault((engagement_id, relative_path), asyncio.Lock())

    def list(self, engagement_id: str) -> list[dict[str, Any]]:
        root = self.workspace_resolver(engagement_id).expanduser().resolve(strict=True)
        entries: list[dict[str, Any]] = []
        for candidate in root.rglob("*.ipynb"):
            if len(entries) >= 1000:
                break
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if not candidate.resolve(strict=True).is_relative_to(root):
                continue
            info = candidate.stat()
            entries.append(
                {
                    "path": candidate.relative_to(root).as_posix(),
                    "size": info.st_size,
                    "modified_at": datetime.fromtimestamp(
                        info.st_mtime, timezone.utc
                    ).isoformat(),
                }
            )
        return sorted(entries, key=lambda entry: entry["path"].lower())

    def read(self, engagement_id: str, relative_path: str) -> dict[str, Any]:
        path = self.path(engagement_id, relative_path)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise NotebookError(
                "notebook was moved or deleted; choose another file"
            ) from exc
        if len(data) > MAX_NOTEBOOK_BYTES:
            raise NotebookError("notebook exceeds the 20 MB editor limit")
        try:
            document = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise NotebookError("notebook is not valid UTF-8 JSON") from exc
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("cells"), list)
            or document.get("nbformat") != 4
        ):
            raise NotebookError("notebook must use Jupyter nbformat 4")
        used_ids = {
            cell.get("id")
            for cell in document["cells"]
            if isinstance(cell, dict)
            and isinstance(cell.get("id"), str)
            and cell.get("id")
        }
        seen_ids: set[str] = set()
        for index, cell in enumerate(document["cells"]):
            if not isinstance(cell, dict):
                raise NotebookError("notebook cells must be objects")
            cell_id = cell.get("id")
            if not isinstance(cell_id, str) or not cell_id or cell_id in seen_ids:
                # Older nbformat 4 files have no cell IDs. Supply stable IDs for
                # display without changing the workspace file on a read. A save
                # writes them alongside the requested change.
                seed = f"{relative_path}:{index}:{_source_hash(_cell_source(cell))}"
                suffix = 0
                while True:
                    generated = uuid5(NAMESPACE_URL, f"{seed}:{suffix}").hex
                    if generated not in used_ids:
                        break
                    suffix += 1
                cell["id"] = generated
                used_ids.add(generated)
                cell_id = generated
            seen_ids.add(cell_id)
        return {
            "path": relative_path,
            "revision": _digest(data),
            "cell_revisions": {
                cell["id"]: _source_hash(_cell_source(cell))
                for cell in document["cells"]
            },
            "document": document,
        }

    async def create(self, engagement_id: str, relative_path: str) -> dict[str, Any]:
        async with self.lock(engagement_id, relative_path):
            path = self.path(engagement_id, relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path(engagement_id, relative_path)
            if path.exists():
                raise NotebookConflict("notebook already exists; open it instead")
            document = {
                "cells": [],
                "metadata": {
                    "kernelspec": {
                        "display_name": "Python 3",
                        "language": "python",
                        "name": "python3",
                    },
                    "language_info": {"name": "python"},
                },
                "nbformat": 4,
                "nbformat_minor": 5,
            }
            self._atomic_write(path, document)
            return self.read(engagement_id, relative_path)

    async def upsert_cell(
        self,
        engagement_id: str,
        relative_path: str,
        *,
        cell_id: str | None,
        source: str,
        expected_revision: str | None,
        expected_source_hash: str | None,
        cell_type: str = "code",
    ) -> dict[str, Any]:
        if cell_type not in {"code", "markdown"}:
            raise NotebookError("cell type must be code or markdown")
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise NotebookError("cell source exceeds 1 MB")
        async with self.lock(engagement_id, relative_path):
            current = self.read(engagement_id, relative_path)
            document = current["document"]
            cells = document["cells"]
            target = next((cell for cell in cells if cell.get("id") == cell_id), None)
            if target is None:
                if cell_id is not None:
                    raise NotebookConflict("cell was deleted; reload the notebook")
                if expected_revision != current["revision"]:
                    raise NotebookConflict(
                        "notebook changed; reload before adding a cell"
                    )
                cell_id = uuid4().hex
                target = {
                    "id": cell_id,
                    "cell_type": cell_type,
                    "metadata": {},
                    "source": source,
                }
                if cell_type == "code":
                    target.update({"execution_count": None, "outputs": []})
                cells.append(target)
            else:
                if expected_source_hash != _source_hash(_cell_source(target)):
                    raise NotebookConflict(
                        "cell changed elsewhere; review it before saving"
                    )
                source_changed = _cell_source(target) != source
                target["source"] = source
                if target.get("cell_type") != cell_type:
                    target["cell_type"] = cell_type
                    target["outputs"] = [] if cell_type == "code" else None
                    target.pop("execution_count", None)
                    if cell_type == "markdown":
                        target.pop("outputs", None)
                elif source_changed and cell_type == "code":
                    target["outputs"] = []
                    target["execution_count"] = None
            self._atomic_write(self.path(engagement_id, relative_path), document)
            result = self.read(engagement_id, relative_path)
            result["cell_id"] = cell_id
            return result

    async def save_outputs(
        self, run: NotebookRun, events: builtins.list[dict[str, Any]]
    ) -> None:
        async with self.lock(run.engagement_id, run.notebook_path):
            current = self.read(run.engagement_id, run.notebook_path)
            cell = next(
                (
                    item
                    for item in current["document"]["cells"]
                    if item.get("id") == run.cell_id
                ),
                None,
            )
            if cell is None or _source_hash(_cell_source(cell)) != run.source_hash:
                return
            outputs: builtins.list[dict[str, Any]] = []
            for event in events:
                kind = event.get("kind")
                content = event.get("content", {})
                if kind == "clear_output":
                    outputs.clear()
                elif kind == "stream":
                    outputs.append(
                        {
                            "output_type": "stream",
                            "name": content.get("name", "stdout"),
                            "text": content.get("text", ""),
                        }
                    )
                elif kind in {"execute_result", "display_data"}:
                    output = {
                        "output_type": kind,
                        "data": content.get("data", {}),
                        "metadata": content.get("metadata", {}),
                    }
                    if kind == "execute_result":
                        output["execution_count"] = content.get("execution_count")
                        cell["execution_count"] = content.get("execution_count")
                    outputs.append(output)
                elif kind == "error":
                    outputs.append(
                        {
                            "output_type": "error",
                            "ename": content.get("ename", "Error"),
                            "evalue": content.get("evalue", ""),
                            "traceback": content.get("traceback", []),
                        }
                    )
            cell["outputs"] = outputs
            self._atomic_write(
                self.path(run.engagement_id, run.notebook_path), current["document"]
            )

    @staticmethod
    def _atomic_write(path: Path, document: dict[str, Any]) -> None:
        data = (json.dumps(document, ensure_ascii=False, indent=1) + "\n").encode(
            "utf-8"
        )
        if len(data) > MAX_NOTEBOOK_BYTES:
            raise NotebookError("notebook exceeds the 20 MB editor limit")
        descriptor, temporary = tempfile.mkstemp(
            prefix=".nebula-notebook-", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class NotebookWorkerConnection:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        on_event: Callable[[str, dict[str, Any]], None],
        *,
        mode: str,
        policy_revision: int,
    ) -> None:
        self.process = process
        self.on_event = on_event
        self.mode = mode
        self.policy_revision = policy_revision
        self.kernel_id = uuid4().hex
        self.restart_count = 0
        self.ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.runs: dict[str, asyncio.Future[str]] = {}
        self.requests: dict[str, asyncio.Future[None]] = {}
        self.active_run: str | None = None
        self.last_used = asyncio.get_running_loop().time()
        self.reader = create_diagnostic_task(
            self._read(),
            feature="runtime",
            event_code="runtime.notebook.worker_reader",
            failure_message="A notebook worker reader stopped unexpectedly.",
            name="nebula-notebook-worker-reader",
        )

    async def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                if len(line) > 2_100_000:
                    raise NotebookRuntimeUnavailable(
                        "Jupyter worker sent an oversized frame"
                    )
                frame = json.loads(line)
                kind = frame.get("type")
                if kind == "ready":
                    if not self.ready.done():
                        self.ready.set_result(str(frame.get("python", "Python")))
                elif kind == "event":
                    self.on_event(str(frame.get("run_id")), frame.get("event", {}))
                elif kind == "result":
                    run_id = str(frame.get("run_id"))
                    run_future = self.runs.pop(run_id, None)
                    self.active_run = None
                    if run_future is not None and not run_future.done():
                        run_future.set_result(str(frame.get("status", "failed")))
                elif kind in {"ack", "error"}:
                    request_id = frame.get("id")
                    request_future = self.requests.pop(str(request_id), None)
                    if request_future is not None and not request_future.done():
                        if kind == "error":
                            request_future.set_exception(
                                NotebookRuntimeUnavailable(
                                    str(frame.get("detail", "worker error"))
                                )
                            )
                        else:
                            request_future.set_result(None)
        except Exception as exc:
            record_caught_exception(
                "runtime",
                "runtime.notebook.worker_read_failed",
                "A notebook worker reader failed.",
                exc,
                stage="notebook-worker",
            )
            failure = (
                exc
                if isinstance(exc, NotebookRuntimeUnavailable)
                else NotebookRuntimeUnavailable(
                    "Jupyter worker failed; restart the kernel and retry"
                )
            )
        else:
            failure = NotebookRuntimeUnavailable(
                "Jupyter worker disconnected; restart the kernel"
            )
        if not self.ready.done():
            self.ready.set_exception(failure)
        for run_future in list(self.runs.values()):
            if not run_future.done():
                run_future.set_exception(failure)
        for request_future in list(self.requests.values()):
            if not request_future.done():
                request_future.set_exception(failure)

    async def send(self, frame: dict[str, Any]) -> None:
        if self.process.returncode is not None or self.process.stdin is None:
            raise NotebookRuntimeUnavailable("Jupyter worker stopped; retry the cell")
        self.process.stdin.write(
            (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
        )
        await self.process.stdin.drain()
        self.last_used = asyncio.get_running_loop().time()

    async def execute(self, run_id: str, code: str) -> str:
        if self.active_run is not None:
            raise NotebookConflict("kernel is busy; wait for the current cell")
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.runs[run_id] = future
        self.active_run = run_id
        try:
            await self.send({"op": "run", "run_id": run_id, "code": code})
            return await future
        finally:
            self.active_run = None
            self.runs.pop(run_id, None)

    async def command(self, operation: str) -> None:
        request_id = uuid4().hex
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.requests[request_id] = future
        await self.send({"op": operation, "id": request_id})
        await asyncio.wait_for(future, timeout=30)

    async def close(self) -> None:
        if self.process.returncode is None:
            try:
                await self.command("shutdown")
            except (NotebookRuntimeUnavailable, asyncio.TimeoutError, BrokenPipeError):
                # diagnostic-expected: force-stop an unresponsive worker during cleanup.
                pass
            if self.process.returncode is None:
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                # diagnostic-expected: a stuck worker is killed after the shutdown deadline.
                self.process.kill()
                await self.process.wait()
        if not self.reader.done():
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


class NotebookService:
    def __init__(
        self,
        *,
        store: NebulaStore,
        workspace_resolver: Callable[[str], Path],
        automation_runtime: AutomationRuntimeManager | None,
        data_root: Path,
    ) -> None:
        self.store = store
        self.documents = NotebookDocumentStore(workspace_resolver)
        self.automation_runtime = automation_runtime
        self.data_root = data_root.expanduser().resolve()
        self.workers: dict[tuple[str, str], NotebookWorkerConnection] = {}
        self._worker_lock = asyncio.Lock()
        self._queues: dict[tuple[str, str], asyncio.Queue[tuple[str, str]]] = {}
        self._drains: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._output_events: dict[str, list[dict[str, Any]]] = {}
        self._interrupted_runs: set[str] = set()
        self._run_done: dict[str, asyncio.Event] = {}
        self._sessions: dict[str, tuple[str, int]] = {}
        self._mode_changes: set[str] = set()
        self._list_locks: dict[str, asyncio.Lock] = {}
        self._list_cache: dict[str, tuple[float, builtins.list[dict[str, Any]]]] = {}
        self._index_errors: dict[str, str] = {}
        self._index_warnings: dict[str, str] = {}
        self._created_notebooks: dict[str, set[str]] = {}

    def startup(self) -> None:
        for run in self.store.find_entities(NotebookRun, {}, limit=None):
            if run.status in {"queued", "running"}:
                self.store.update(
                    NotebookRun,
                    run.id,
                    {
                        "status": "interrupted",
                        "detail": "Core restarted; run the cell again",
                        "completed_at": _now(),
                    },
                    expected_revision=run.revision,
                )
                self.store.append_event(
                    run.id,
                    "notebook.completed",
                    {"status": "interrupted", "detail": "Core restarted"},
                )

    def active_runs(self, engagement_id: str) -> list[NotebookRun]:
        self.store.get(Engagement, engagement_id)
        return self.store.find_entities(
            NotebookRun,
            {"engagement_id": engagement_id, "status": ["queued", "running"]},
            limit=None,
        )

    async def prepare_policy_change(self, engagement_id: str, next_mode: str) -> bool:
        async with self._worker_lock:
            if engagement_id in self._mode_changes:
                raise NotebookConflict("Project mode is changing; retry in a moment")
            if self.policy(engagement_id).execution_mode == next_mode:
                return False
            if self.active_runs(engagement_id):
                raise NotebookConflict(
                    "A notebook cell is running or queued; interrupt it before changing project mode"
                )
            affected = [
                (key, worker)
                for key, worker in self.workers.items()
                if key[0] == engagement_id
            ]
            if any(worker.active_run for _, worker in affected):
                raise NotebookConflict(
                    "A notebook cell is running; interrupt it before changing project mode"
                )
            self._mode_changes.add(engagement_id)
            try:
                for key, worker in affected:
                    await worker.close()
                    self.workers.pop(key, None)
                session = self._sessions.pop(engagement_id, None)
                if session is not None and self.automation_runtime is not None:
                    await self.automation_runtime.close_session(session[0])
            except BaseException:
                self._mode_changes.discard(engagement_id)
                raise
            return True

    def finish_policy_change(self, engagement_id: str) -> None:
        self._mode_changes.discard(engagement_id)

    def policy(self, engagement_id: str) -> AutomationProjectPolicy:
        if self.automation_runtime is None:
            raise NotebookRuntimeUnavailable("project command runtime is unavailable")
        return self.automation_runtime.project_policy(engagement_id)

    def status(self, engagement_id: str) -> dict[str, Any]:
        policy = self.policy(engagement_id)
        return {
            "project_id": engagement_id,
            "execution_mode": policy.execution_mode,
            "host_access_acknowledged": policy.host_access_acknowledged,
            "consent_required": policy.execution_mode == "host"
            and not policy.host_access_acknowledged,
            "runtime_ready": self.automation_runtime is not None,
        }

    def list(self, engagement_id: str) -> list[dict[str, Any]]:
        self.store.get(Engagement, engagement_id)
        return self.documents.list(engagement_id)

    def file_index_status(self, engagement_id: str) -> dict[str, Any]:
        engagement = self.store.get(Engagement, engagement_id)
        path = engagement.metadata.get("notebook_file_index")
        return {
            "path": path if isinstance(path, str) else None,
            "state": "fallback"
            if path and engagement_id in self._index_errors
            else (
                "partial"
                if path and engagement_id in self._index_warnings
                else ("connected" if path else "none")
            ),
            "detail": self._index_errors.get(engagement_id)
            or self._index_warnings.get(engagement_id),
        }

    async def set_file_index(
        self, engagement_id: str, path: str | None
    ) -> dict[str, Any]:
        engagement = self.store.get(Engagement, engagement_id)
        if path is not None:
            listing = await asyncio.to_thread(
                list_indexed_notebooks,
                path,
                self.documents.workspace_resolver(engagement_id),
            )
        metadata = dict(engagement.metadata)
        if path is None:
            metadata.pop("notebook_file_index", None)
        else:
            metadata["notebook_file_index"] = str(
                Path(path).expanduser().resolve(strict=True)
            )
        self.store.update(
            Engagement,
            engagement_id,
            {"metadata": metadata},
            expected_revision=engagement.revision,
        )
        self._list_cache.pop(engagement_id, None)
        self._index_errors.pop(engagement_id, None)
        if path is not None and listing.warning:
            self._index_warnings[engagement_id] = listing.warning
        else:
            self._index_warnings.pop(engagement_id, None)
        return self.file_index_status(engagement_id)

    async def list_async(
        self, engagement_id: str, *, refresh: bool = False
    ) -> builtins.list[dict[str, Any]]:
        self.store.get(Engagement, engagement_id)
        async with self._list_locks.setdefault(engagement_id, asyncio.Lock()):
            cached = self._list_cache.get(engagement_id)
            if not refresh and cached and cached[0] > time.monotonic():
                return cached[1]
            engagement = self.store.get(Engagement, engagement_id)
            index_path = engagement.metadata.get("notebook_file_index")
            if isinstance(index_path, str):
                try:
                    listing = await asyncio.to_thread(
                        list_indexed_notebooks,
                        index_path,
                        self.documents.workspace_resolver(engagement_id),
                    )
                    self._index_errors.pop(engagement_id, None)
                    if listing.warning:
                        self._index_warnings[engagement_id] = listing.warning
                    else:
                        self._index_warnings.pop(engagement_id, None)
                    if listing.entries or not listing.warning:
                        entries = listing.entries
                        indexed = {str(item["path"]) for item in entries}
                        created = self._created_notebooks.get(engagement_id, set())
                        created.difference_update(indexed)
                        for path in sorted(created):
                            try:
                                candidate = self.documents.path(engagement_id, path)
                                info = candidate.stat()
                            except (OSError, NotebookError):
                                # diagnostic-expected: a recently created file may be moved before indexing.
                                created.discard(path)
                                continue
                            entries.append(
                                {
                                    "path": path,
                                    "size": info.st_size,
                                    "modified_at": datetime.fromtimestamp(
                                        info.st_mtime, timezone.utc
                                    ).isoformat(),
                                }
                            )
                        entries = sorted(
                            entries, key=lambda item: str(item["path"]).lower()
                        )[:1000]
                        self._list_cache[engagement_id] = (
                            time.monotonic() + NOTEBOOK_LIST_CACHE_SECONDS,
                            entries,
                        )
                        return entries
                    self._index_errors[engagement_id] = (
                        "The index is updating and has no notebooks; scanning the project folder."
                    )
                    self._index_warnings.pop(engagement_id, None)
                except (FileIndexUnavailable, OSError, ValueError) as exc:
                    # diagnostic-expected: index failure is visible in status and falls back to the workspace scan.
                    self._index_errors[engagement_id] = str(exc)
                    self._index_warnings.pop(engagement_id, None)
            # A linked workspace can contain a very large tree. Never walk it
            # on the FastAPI event loop, and share the result between callers.
            entries = await asyncio.to_thread(self.documents.list, engagement_id)
            self._list_cache[engagement_id] = (
                time.monotonic() + NOTEBOOK_LIST_CACHE_SECONDS,
                entries,
            )
            return entries

    def read(self, engagement_id: str, path: str) -> dict[str, Any]:
        self.store.get(Engagement, engagement_id)
        return self.documents.read(engagement_id, path)

    async def create(self, engagement_id: str, path: str) -> dict[str, Any]:
        self.store.get(Engagement, engagement_id)
        result = await self.documents.create(engagement_id, path)
        self._created_notebooks.setdefault(engagement_id, set()).add(path)
        self._list_cache.pop(engagement_id, None)
        return result

    async def upsert_cell(
        self, engagement_id: str, path: str, **changes: Any
    ) -> dict[str, Any]:
        self.store.get(Engagement, engagement_id)
        return await self.documents.upsert_cell(engagement_id, path, **changes)

    def pin(
        self, session_id: str, path: str | None, *, expected_revision: int
    ) -> ChatSession:
        session = self.store.get(ChatSession, session_id)
        if path is not None:
            self.read(session.engagement_id, path)
        return self.store.update(
            ChatSession,
            session_id,
            {"notebook_path": path},
            expected_revision=expected_revision,
        )

    def run(
        self, run_id: str, *, engagement_id: str, after_sequence: int = 0
    ) -> dict[str, Any]:
        run = self.store.get(NotebookRun, run_id)
        if run.engagement_id != engagement_id:
            raise NotebookError("run is outside the selected project")
        events = self.store.replay_events(
            run_id, after_sequence=after_sequence, limit=1000
        )
        return {
            "run": run.model_dump(mode="json"),
            "events": [event.model_dump(mode="json") for event in events],
            "next_sequence": events[-1].sequence if events else after_sequence,
        }

    def recent_runs(
        self, engagement_id: str, path: str
    ) -> builtins.list[dict[str, Any]]:
        self.read(engagement_id, path)
        runs = self.store.find_entities(
            NotebookRun, {"engagement_id": engagement_id}, newest_first=True, limit=200
        )
        return [
            run.model_dump(mode="json") for run in runs if run.notebook_path == path
        ][:20]

    async def start_run(
        self,
        engagement_id: str,
        path: str,
        cell_id: str,
        *,
        expected_source_hash: str | None = None,
    ) -> NotebookRun:
        if engagement_id in self._mode_changes:
            raise NotebookConflict(
                "Project mode is changing; retry the cell in a moment"
            )
        policy = self.policy(engagement_id)
        if policy.execution_mode == "host" and not policy.host_access_acknowledged:
            raise NotebookPermissionRequired(
                "Acknowledge host filesystem and network access in Project settings"
            )
        document = self.read(engagement_id, path)["document"]
        cell = next(
            (item for item in document["cells"] if item.get("id") == cell_id), None
        )
        if cell is None or cell.get("cell_type") != "code":
            raise NotebookError("choose a saved code cell")
        source = _cell_source(cell)
        source_hash = _source_hash(source)
        if expected_source_hash is not None and source_hash != expected_source_hash:
            raise NotebookConflict(
                "saved code cell changed; read its current source hash before executing"
            )
        run = self.store.create(
            NotebookRun(
                engagement_id=engagement_id,
                notebook_path=path,
                cell_id=cell_id,
                source_hash=source_hash,
                execution_mode=policy.execution_mode,
            )
        )
        self.store.append_event(
            run.id, "notebook.queued", {"cell_id": cell_id, "path": path}
        )
        key = (engagement_id, path)
        queue = self._queues.setdefault(key, asyncio.Queue())
        queue.put_nowait((run.id, source))
        self._run_done[run.id] = asyncio.Event()
        if key not in self._drains or self._drains[key].done():
            self._drains[key] = create_diagnostic_task(
                self._drain(key),
                feature="runtime",
                event_code="runtime.notebook.run_queue",
                failure_message="A notebook run queue stopped unexpectedly.",
                name=f"nebula-notebook-drain-{engagement_id}",
            )
        return run

    async def _drain(self, key: tuple[str, str]) -> None:
        queue = self._queues[key]
        while not queue.empty():
            run_id, source = await queue.get()
            run = self.store.get(NotebookRun, run_id)
            if run.status == "interrupted":
                self._run_done[run_id].set()
                queue.task_done()
                continue
            try:
                worker = await self._worker(key[0], key[1])
                run = self.store.get(NotebookRun, run_id)
                if run.status == "interrupted":
                    self._run_done[run_id].set()
                    queue.task_done()
                    continue
                current_policy = self.policy(key[0])
                if current_policy.execution_mode != run.execution_mode:
                    raise NotebookConflict(
                        "project execution mode changed; run this cell again"
                    )
                run = self.store.update(
                    NotebookRun,
                    run_id,
                    {"status": "running", "started_at": _now()},
                    expected_revision=run.revision,
                )
                self.store.append_event(
                    run_id, "notebook.running", {"mode": run.execution_mode}
                )
                self._output_events[run_id] = []
                status = await worker.execute(run_id, source)
                if run_id in self._interrupted_runs:
                    status = "interrupted"
                await self.documents.save_outputs(run, self._output_events[run_id])
                detail = ""
            except Exception as exc:
                record_caught_exception(
                    "runtime",
                    "runtime.notebook.run_failed",
                    "A notebook cell failed before completion.",
                    exc,
                    stage="notebook-run",
                )
                status = "failed"
                detail = str(exc)[:2000]
            finally:
                self._output_events.pop(run_id, None)
                self._interrupted_runs.discard(run_id)
            latest = self.store.get(NotebookRun, run_id)
            self.store.update(
                NotebookRun,
                run_id,
                {"status": status, "detail": detail, "completed_at": _now()},
                expected_revision=latest.revision,
            )
            self.store.append_event(
                run_id, "notebook.completed", {"status": status, "detail": detail}
            )
            self._run_done[run_id].set()
            queue.task_done()

    def _on_event(self, run_id: str, event: dict[str, Any]) -> None:
        if run_id not in self._output_events:
            return
        self._output_events[run_id].append(event)
        self.store.append_event(run_id, "notebook.output", event)

    async def wait_run(
        self, run_id: str, *, engagement_id: str, timeout: float = 30
    ) -> dict[str, Any]:
        run = self.store.get(NotebookRun, run_id)
        if run.engagement_id != engagement_id:
            raise NotebookError("run is outside the selected project")
        event = self._run_done.get(run_id)
        if event is not None and run.status in {"queued", "running"}:
            try:
                await asyncio.wait_for(event.wait(), timeout=min(timeout, 60))
            except asyncio.TimeoutError:
                # diagnostic-expected: wait is bounded; the run remains available for replay.
                pass
        return self.run(run_id, engagement_id=engagement_id)

    async def interrupt(self, run_id: str, *, engagement_id: str) -> NotebookRun:
        run = self.store.get(NotebookRun, run_id)
        if run.engagement_id != engagement_id:
            raise NotebookError("run is outside the selected project")
        if run.status == "queued":
            updated = self.store.update(
                NotebookRun,
                run_id,
                {"status": "interrupted", "completed_at": _now()},
                expected_revision=run.revision,
            )
            self.store.append_event(
                run_id, "notebook.completed", {"status": "interrupted"}
            )
            return updated
        if run.status == "running":
            self._interrupted_runs.add(run_id)
            worker = self.workers.get((engagement_id, run.notebook_path))
            if worker is not None:
                await worker.command("interrupt")
        return self.store.get(NotebookRun, run_id)

    def kernel_state(self, engagement_id: str, path: str) -> dict[str, Any]:
        notebook = self.read(engagement_id, path)
        worker = self.workers.get((engagement_id, path))
        if worker is None or worker.process.returncode is not None:
            return {
                "path": path,
                "notebook_revision": notebook["revision"],
                "state": "absent",
                "kernel_id": None,
                "restart_count": None,
                "active_run": None,
            }
        return {
            "path": path,
            "notebook_revision": notebook["revision"],
            "state": "running" if worker.active_run else "idle",
            "kernel_id": worker.kernel_id,
            "restart_count": worker.restart_count,
            "active_run": worker.active_run,
        }

    async def restart(
        self,
        engagement_id: str,
        path: str,
        *,
        expected_notebook_revision: str | None = None,
        expected_kernel_id: str | None = None,
        expected_restart_count: int | None = None,
    ) -> dict[str, Any]:
        notebook = self.read(engagement_id, path)
        policy = self.policy(engagement_id)
        if policy.execution_mode == "host" and not policy.host_access_acknowledged:
            raise NotebookPermissionRequired(
                "Acknowledge Host access in Project settings before restarting a kernel"
            )
        if (
            expected_notebook_revision is not None
            and notebook["revision"] != expected_notebook_revision
        ):
            raise NotebookConflict("notebook changed; read it before restarting")
        worker = self.workers.get((engagement_id, path))
        if worker is None or worker.process.returncode is not None:
            if expected_kernel_id is not None:
                raise NotebookConflict(
                    "kernel changed or stopped; read its current state"
                )
            return {"restarted": False, **self.kernel_state(engagement_id, path)}
        if expected_kernel_id is not None and worker.kernel_id != expected_kernel_id:
            raise NotebookConflict("kernel changed; read its current state")
        if (
            expected_restart_count is not None
            and worker.restart_count != expected_restart_count
        ):
            raise NotebookConflict(
                "kernel was already restarted; read its current state"
            )
        if worker.active_run:
            raise NotebookConflict("cell is running; interrupt it before restarting")
        await worker.command("restart")
        if self.workers.get((engagement_id, path)) is not worker:
            raise NotebookConflict(
                "kernel changed during restart; read its current state"
            )
        worker.restart_count += 1
        return {"restarted": True, **self.kernel_state(engagement_id, path)}

    async def _worker(self, engagement_id: str, path: str) -> NotebookWorkerConnection:
        key = (engagement_id, path)
        async with self._worker_lock:
            policy = self.policy(engagement_id)
            existing = self.workers.get(key)
            if (
                existing is not None
                and existing.policy_revision == policy.revision
                and existing.process.returncode is None
            ):
                return existing
            if existing is not None:
                if existing.active_run:
                    raise NotebookConflict(
                        "cell is running; interrupt it before changing project mode"
                    )
                await existing.close()
                self.workers.pop(key, None)
            if len(self.workers) >= MAX_WARM_KERNELS:
                idle = [
                    (other_key, other)
                    for other_key, other in self.workers.items()
                    if other.active_run is None
                ]
                if not idle:
                    raise NotebookRuntimeUnavailable(
                        "all notebook kernels are busy; retry when one finishes"
                    )
                evict_key, evict = min(idle, key=lambda item: item[1].last_used)
                await evict.close()
                self.workers.pop(evict_key, None)
            command = await self._worker_command(engagement_id, policy)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.documents.workspace_resolver(engagement_id)),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=2_100_000,
            )
            worker = NotebookWorkerConnection(
                process,
                self._on_event,
                mode=policy.execution_mode,
                policy_revision=policy.revision,
            )
            try:
                await asyncio.wait_for(worker.ready, timeout=45)
            except Exception as exc:
                await worker.close()
                raise NotebookRuntimeUnavailable(
                    f"Jupyter kernel could not start: {exc}"
                ) from exc
            self.workers[key] = worker
            return worker

    async def _worker_command(
        self, engagement_id: str, policy: AutomationProjectPolicy
    ) -> builtins.list[str]:
        if self.automation_runtime is None:
            raise NotebookRuntimeUnavailable("project runtime is unavailable")
        if policy.execution_mode == "host":
            if not policy.host_access_acknowledged:
                raise NotebookPermissionRequired(
                    "Acknowledge host access in Project settings"
                )
            python, worker_script, _ = await self._host_environment(engagement_id)
            return [str(python), str(worker_script)]
        prior = self._sessions.get(engagement_id)
        if prior is not None and prior[1] != policy.revision:
            await self.automation_runtime.close_session(prior[0])
            self._sessions.pop(engagement_id, None)
        info = await self.automation_runtime.runtime_info(engagement_id=engagement_id)
        if not info.ready:
            await self.automation_runtime.prepare()
        session = await self.automation_runtime.session(
            engagement_id=engagement_id,
            owner_kind="notebook",
            owner_id="project-notebooks",
        )
        managed = self.automation_runtime._sessions[session.id]
        backend = managed.backend
        if not isinstance(backend, ContainerRuntimeSession):
            raise NotebookRuntimeUnavailable(
                "Docker notebook session did not start in Docker"
            )
        self._sessions[engagement_id] = (session.id, policy.revision)
        prefix = [*backend.runner._runtime_argv(), "exec", backend.container_name]
        environment = self.automation_runtime.notebook_data_directory(engagement_id)
        if not (environment / "pyvenv.cfg").exists():
            await self._capture(
                [
                    *prefix,
                    "/usr/bin/python3",
                    "-m",
                    "venv",
                    "--system-site-packages",
                    "/nebula-notebook-env",
                ]
            )
        return [
            *backend.runner._runtime_argv(),
            "exec",
            "--interactive",
            backend.container_name,
            "/nebula-notebook-env/bin/python",
            "/usr/local/lib/nebula-notebook-worker.py",
        ]

    async def _host_environment(
        self, engagement_id: str
    ) -> tuple[Path, Path, Path | None]:
        configured = os.environ.get("NEBULA_NOTEBOOK_RUNTIME_DIR")
        uv: Path | None
        if configured:
            runtime = await self._staged_runtime(Path(configured).resolve(strict=True))
            base_python = runtime / "python" / "bin" / "python3"
            worker = runtime / "worker.py"
            uv = runtime / "uv"
            if not base_python.is_file() or not worker.is_file() or not uv.is_file():
                raise NotebookRuntimeUnavailable(
                    "bundled notebook runtime is incomplete; repair the Nebula installation"
                )
        elif getattr(sys, "frozen", False):
            raise NotebookRuntimeUnavailable(
                "bundled notebook runtime is missing; repair the Nebula installation"
            )
        else:
            base_python = Path(sys.executable)
            worker = (
                Path(__file__).resolve().parents[3]
                / "packaging"
                / "notebook"
                / "worker.py"
            )
            uv_location = shutil.which("uv")
            uv = Path(uv_location) if uv_location else None
        key = hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()[:32]
        environment = self.data_root / "notebooks" / "envs" / key / "host"
        python = environment / "bin" / "python"
        if not python.is_file():
            environment.parent.mkdir(parents=True, exist_ok=True)
            await self._capture(
                [
                    str(base_python),
                    "-m",
                    "venv",
                    "--system-site-packages",
                    str(environment),
                ]
            )
        return python, worker, uv

    async def _staged_runtime(self, resource: Path) -> Path:
        manifest_path = resource / "nebula-notebook-runtime.json"
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest = json.loads(manifest_bytes)
            if manifest["schema"] != 1:
                raise ValueError("invalid notebook runtime manifest")
            identity = _digest(manifest_bytes)[:32]
        except (OSError, ValueError, KeyError) as exc:
            raise NotebookRuntimeUnavailable(
                "bundled notebook runtime manifest is invalid"
            ) from exc
        destination = self.data_root / "notebooks" / "runtimes" / identity
        if not destination.is_dir():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = destination.parent / f".{identity}-{uuid4().hex}"
            try:
                await asyncio.to_thread(shutil.copytree, resource, temporary)
                try:
                    os.replace(temporary, destination)
                except OSError:
                    if not destination.is_dir():
                        raise
                    # diagnostic-expected: another Core staged this exact manifest first.
                    shutil.rmtree(temporary)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        for relative, expected in manifest["executables"].items():
            file = (destination / relative).resolve(strict=True)
            if (
                not file.is_relative_to(destination)
                or _digest(file.read_bytes()) != expected
            ):
                raise NotebookRuntimeUnavailable(
                    "bundled notebook runtime failed verification"
                )
        worker = destination / "worker.py"
        if _digest(worker.read_bytes()) != manifest["worker_sha256"]:
            raise NotebookRuntimeUnavailable(
                "bundled notebook worker failed verification"
            )
        return destination

    @staticmethod
    async def _capture(command: builtins.list[str], timeout: float = 120) -> str:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise NotebookRuntimeUnavailable(
                "notebook environment preparation timed out"
            )
        if process.returncode != 0:
            raise NotebookRuntimeUnavailable(
                (stderr or stdout).decode("utf-8", errors="replace")[:2000]
                or "notebook environment preparation failed"
            )
        return stdout.decode("utf-8", errors="replace")

    async def install_package(
        self,
        engagement_id: str,
        path: str,
        requirement: str,
        *,
        expected_notebook_revision: str | None = None,
    ) -> dict[str, Any]:
        notebook = self.read(engagement_id, path)
        if (
            expected_notebook_revision is not None
            and notebook["revision"] != expected_notebook_revision
        ):
            raise NotebookConflict(
                "notebook changed; read it before installing a package"
            )
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9,._-]+\])?(?:==[A-Za-z0-9._+-]+)?",
            requirement,
        ):
            raise NotebookError(
                "use a package name, optional extras, and an optional exact version"
            )
        worker = self.workers.get((engagement_id, path))
        if worker is not None and worker.active_run:
            raise NotebookConflict("cell is running; wait before installing a package")
        policy = self.policy(engagement_id)
        if policy.execution_mode == "host" and not policy.host_access_acknowledged:
            raise NotebookPermissionRequired(
                "Acknowledge Host access in Project settings before installing packages"
            )
        if policy.execution_mode == "host":
            python, _, uv = await self._host_environment(engagement_id)
            if uv is None:
                raise NotebookRuntimeUnavailable(
                    "uv is unavailable in this development environment"
                )
            command = [
                str(uv),
                "pip",
                "install",
                "--no-python-downloads",
                "--python",
                str(python),
                requirement,
            ]
            if not policy.network_enabled:
                command.insert(-1, "--offline")
        else:
            if self.automation_runtime is None:
                raise NotebookRuntimeUnavailable("project runtime is unavailable")
            await self._worker_command(engagement_id, policy)
            session_id, _ = self._sessions[engagement_id]
            backend = self.automation_runtime._sessions[session_id].backend
            assert isinstance(backend, ContainerRuntimeSession)
            command = [
                *backend.runner._runtime_argv(),
                "exec",
                backend.container_name,
                "/usr/local/bin/uv",
                "pip",
                "install",
                "--no-python-downloads",
                "--python",
                "/nebula-notebook-env/bin/python",
                requirement,
            ]
            if not backend.network_enabled:
                command.insert(-1, "--offline")
        output = await self._capture(command, timeout=300)
        name = re.split(r"\[|==", requirement, maxsplit=1)[0]
        observed = await self.package_status(engagement_id, path, name)
        exact_version = requirement.partition("==")[2]
        if not observed["installed"] or (
            exact_version and observed["version"] != exact_version
        ):
            raise NotebookRuntimeUnavailable(
                "package command finished but the selected environment did not report the requested version; inspect package status before retrying"
            )
        return {
            "requirement": requirement,
            "installed": True,
            "name": observed["name"],
            "version": observed["version"],
            "detail": output[-2000:],
            "restart_recommended": True,
        }

    async def package_status(
        self, engagement_id: str, path: str, name: str
    ) -> dict[str, Any]:
        """Read distribution metadata from the selected Notebook environment."""

        self.read(engagement_id, path)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            raise NotebookError("enter an exact Python package name")
        policy = self.policy(engagement_id)
        if policy.execution_mode == "host":
            if not policy.host_access_acknowledged:
                raise NotebookPermissionRequired(
                    "Acknowledge Host access in Project settings before reading packages"
                )
            python, _, _ = await self._host_environment(engagement_id)
            command = [str(python)]
        else:
            if self.automation_runtime is None:
                raise NotebookRuntimeUnavailable("project runtime is unavailable")
            await self._worker_command(engagement_id, policy)
            session_id, _ = self._sessions[engagement_id]
            backend = self.automation_runtime._sessions[session_id].backend
            assert isinstance(backend, ContainerRuntimeSession)
            command = [
                *backend.runner._runtime_argv(),
                "exec",
                backend.container_name,
                "/nebula-notebook-env/bin/python",
            ]
        script = (
            "import importlib.metadata,json,sys\n"
            "try:\n"
            " dist=importlib.metadata.distribution(sys.argv[1])\n"
            " print(json.dumps({'installed':True,'name':dist.metadata['Name'],'version':dist.version}))\n"
            "except importlib.metadata.PackageNotFoundError:\n"
            " print(json.dumps({'installed':False,'name':sys.argv[1],'version':None}))\n"
        )
        output = await self._capture([*command, "-c", script, name], timeout=60)
        try:
            observed = json.loads(output)
            if (
                not isinstance(observed, dict)
                or type(observed.get("installed")) is not bool
            ):
                raise ValueError("invalid package status")
        except (ValueError, TypeError) as exc:
            raise NotebookRuntimeUnavailable(
                "selected Notebook environment returned invalid package status"
            ) from exc
        return {"path": path, "execution_mode": policy.execution_mode, **observed}

    async def search_package(
        self, engagement_id: str, path: str, name: str
    ) -> dict[str, Any]:
        """Resolve an exact package name through the selected runtime's network boundary."""

        self.read(engagement_id, path)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name):
            raise NotebookError("enter a Python package name to look up")
        policy = self.policy(engagement_id)
        if policy.execution_mode == "host" and not policy.host_access_acknowledged:
            raise NotebookPermissionRequired(
                "Acknowledge Host access in Project settings before searching packages"
            )
        if not policy.network_enabled:
            raise NotebookPermissionRequired(
                "Enable project network access to search packages; offline installs remain available"
            )
        script = (
            "import json,sys,urllib.request,urllib.error\n"
            "from urllib.parse import quote\n"
            "url='https://pypi.org/pypi/'+quote(sys.argv[1],safe='')+'/json'\n"
            "try:\n"
            " response=urllib.request.urlopen(url,timeout=10)\n"
            " data=json.load(response)['info']\n"
            " print(json.dumps({'name':data['name'],'version':data['version'],"
            "'summary':data.get('summary','')[:500],'url':data.get('project_url','')}))\n"
            "except urllib.error.HTTPError as error:\n"
            " sys.exit('Package was not found on PyPI' if error.code==404 else 'PyPI returned HTTP '+str(error.code))\n"
        )
        if policy.execution_mode == "host":
            python, _, _ = await self._host_environment(engagement_id)
            command = [str(python), "-c", script, name]
        else:
            if self.automation_runtime is None:
                raise NotebookRuntimeUnavailable("project runtime is unavailable")
            await self._worker_command(engagement_id, policy)
            session_id, _ = self._sessions[engagement_id]
            backend = self.automation_runtime._sessions[session_id].backend
            if (
                not isinstance(backend, ContainerRuntimeSession)
                or not backend.network_enabled
            ):
                raise NotebookPermissionRequired(
                    "Enable Docker project network access to search packages"
                )
            command = [
                *backend.runner._runtime_argv(),
                "exec",
                backend.container_name,
                "/nebula-notebook-env/bin/python",
                "-c",
                script,
                name,
            ]
        try:
            return json.loads(await self._capture(command, timeout=20))
        except (ValueError, NotebookRuntimeUnavailable) as exc:
            raise NotebookRuntimeUnavailable(f"Package lookup failed: {exc}") from exc

    async def shutdown(self) -> None:
        for worker in list(self.workers.values()):
            await worker.close()
        self.workers.clear()
        for task in self._drains.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self._drains.values(), return_exceptions=True)
