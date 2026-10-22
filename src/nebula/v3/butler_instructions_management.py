"""Authenticated editing of the operator-owned Nebula-wide Butler mandate."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .butler_instructions import ButlerInstructionsError, MAX_INSTRUCTIONS_BYTES
from .butler_recovery import DEFAULT_CHECK_INTERVAL_MINUTES, GLOBAL_STATE_ID
from .database import EntityRow
from .domain import ButlerGlobalState
from .storage import NebulaStore


MAX_INSPECT_BYTES = 1024 * 1024
_WRITE_LOCK = Lock()


class ButlerInstructionsUpdate(BaseModel):
    content: str
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class ButlerCheckIntervalUpdate(BaseModel):
    check_interval_minutes: int = Field(ge=1, le=1440)
    expected_revision: int = Field(ge=0)


def butler_check_interval_snapshot(store: NebulaStore) -> dict[str, Any]:
    state = store.find_entity(ButlerGlobalState, "id", GLOBAL_STATE_ID)
    minutes = state.check_interval_minutes if state else DEFAULT_CHECK_INTERVAL_MINUTES
    return {
        "check_interval_minutes": minutes,
        "revision": state.revision if state else 0,
        "last_check_at": state.last_periodic_at if state else None,
        "next_check_at": (
            state.last_periodic_at + timedelta(minutes=minutes)
            if state and state.last_periodic_at
            else None
        ),
    }


def _source(data_root: Path) -> bytes | None:
    root_fd: int | None = None
    folder_fd: int | None = None
    file_fd: int | None = None
    try:
        root_fd = os.open(
            data_root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            folder_fd = os.open(
                ".butler",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            # diagnostic-expected: a missing instruction directory is reported as absent.
            return None
        try:
            file_fd = os.open(
                "BUTLER.md",
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=folder_fd,
            )
        except FileNotFoundError:
            # diagnostic-expected: a missing BUTLER.md is reported as absent.
            return None
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ButlerInstructionsError("BUTLER.md must be a regular file")
        content = os.read(file_fd, MAX_INSPECT_BYTES + 1)
        if len(content) > MAX_INSPECT_BYTES:
            raise ButlerInstructionsError(
                "BUTLER.md exceeds the 1 MiB editor inspection limit; repair it on the Core host"
            )
        return content
    except ButlerInstructionsError:
        raise
    except OSError as exc:
        raise ButlerInstructionsError(
            "Cannot safely read .butler/BUTLER.md in the Nebula data directory"
        ) from exc
    finally:
        for descriptor in (file_fd, folder_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def butler_instructions_snapshot(data_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(data_root / ".butler" / "BUTLER.md"),
        "status": "missing",
        "content": None,
        "sha256": None,
        "detail": None,
    }
    try:
        source = _source(data_root)
    except ButlerInstructionsError as exc:
        # diagnostic-expected: validation failure is returned as an invalid status.
        return {**result, "status": "invalid", "detail": str(exc)}
    if source is None:
        return result
    digest = hashlib.sha256(source).hexdigest()
    result.update(status="ready", sha256=digest)
    if len(source) > MAX_INSTRUCTIONS_BYTES:
        return {
            **result,
            "status": "invalid",
            "detail": "BUTLER.md exceeds the 32 KiB limit; upload a shorter file",
        }
    try:
        content = source.decode("utf-8")
    except UnicodeDecodeError:
        # diagnostic-expected: invalid text is returned as an invalid status.
        return {
            **result,
            "status": "invalid",
            "detail": "BUTLER.md must be UTF-8; upload a UTF-8 file",
        }
    if not content.strip():
        return {
            **result,
            "status": "invalid",
            "content": content,
            "detail": "BUTLER.md is empty; add instructions and save",
        }
    return {**result, "content": content}


def write_butler_instructions(
    data_root: Path, content: str, expected_sha256: str | None
) -> dict[str, Any]:
    encoded = content.encode("utf-8")
    if not content.strip():
        raise ButlerInstructionsError("BUTLER.md needs non-empty instructions")
    if len(encoded) > MAX_INSTRUCTIONS_BYTES:
        raise ButlerInstructionsError("BUTLER.md exceeds the 32 KiB limit")
    with _WRITE_LOCK:
        current = butler_instructions_snapshot(data_root)
        if current["status"] == "invalid" and current["sha256"] is None:
            raise ButlerInstructionsError(current["detail"])
        if current["sha256"] != expected_sha256:
            raise ButlerInstructionsConflict(
                "BUTLER.md changed since you loaded it; reload before saving"
            )
        root_fd: int | None = None
        folder_fd: int | None = None
        temporary: str | None = None
        try:
            root_fd = os.open(
                data_root,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.mkdir(".butler", mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                # diagnostic-expected: another writer created the directory first.
                pass
            folder_fd = os.open(
                ".butler",
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            temporary = f".BUTLER.md.{secrets.token_hex(12)}.tmp"
            file_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=folder_fd,
            )
            try:
                view = memoryview(encoded)
                while view:
                    view = view[os.write(file_fd, view) :]
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            latest = butler_instructions_snapshot(data_root)
            if latest["sha256"] != expected_sha256 or (
                latest["status"] == "invalid" and latest["sha256"] is None
            ):
                raise ButlerInstructionsConflict(
                    "BUTLER.md changed while saving; reload before trying again"
                )
            os.replace(
                temporary,
                "BUTLER.md",
                src_dir_fd=folder_fd,
                dst_dir_fd=folder_fd,
            )
            temporary = None
            os.fsync(folder_fd)
        except (OSError, ButlerInstructionsError) as exc:
            if isinstance(exc, ButlerInstructionsError):
                raise
            raise ButlerInstructionsError(
                "Cannot safely save .butler/BUTLER.md in the Nebula data directory"
            ) from exc
        finally:
            if temporary is not None and folder_fd is not None:
                os.unlink(temporary, dir_fd=folder_fd)
            for descriptor in (folder_fd, root_fd):
                if descriptor is not None:
                    os.close(descriptor)
        return butler_instructions_snapshot(data_root)


class ButlerInstructionsConflict(ValueError):
    """The operator's saved file changed after the caller loaded it."""


def butler_instructions_router(data_root: Path, store: NebulaStore) -> APIRouter:
    router = APIRouter(tags=["butler"])

    @router.get("/butler/instructions")
    def read_instructions() -> dict[str, Any]:
        return butler_instructions_snapshot(data_root)

    @router.put("/butler/instructions")
    def update_instructions(request: ButlerInstructionsUpdate) -> dict[str, Any]:
        try:
            return write_butler_instructions(
                data_root, request.content, request.expected_sha256
            )
        except ButlerInstructionsConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ButlerInstructionsError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/butler/check-interval")
    def read_check_interval() -> dict[str, Any]:
        return butler_check_interval_snapshot(store)

    @router.put("/butler/check-interval")
    def update_check_interval(request: ButlerCheckIntervalUpdate) -> dict[str, Any]:
        with store.transaction(immediate=True) as transaction:
            row = transaction.session.get(EntityRow, GLOBAL_STATE_ID)
            state = ButlerGlobalState.model_validate(row.payload) if row else None
            if (state.revision if state else 0) != request.expected_revision:
                raise HTTPException(
                    status_code=409,
                    detail="Butler's check interval changed; reload before saving",
                )
            if state is None:
                transaction.add(
                    ButlerGlobalState(
                        id=GLOBAL_STATE_ID,
                        check_interval_minutes=request.check_interval_minutes,
                    )
                )
            else:
                transaction.update(
                    ButlerGlobalState,
                    state.id,
                    {"check_interval_minutes": request.check_interval_minutes},
                    expected_revision=state.revision,
                )
        return butler_check_interval_snapshot(store)

    return router
