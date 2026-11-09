"""Bounded host folder selection shared by host and standalone Core APIs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import HTTPException
from pydantic import Field, model_validator

from .domain import NebulaModel
from .async_utils import finish_cleanup


class HostWorkspaceFolderCreateRequest(NebulaModel):
    parent_path: str = Field(min_length=1, max_length=4096)
    name: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def folder_name_is_one_portable_component(self):
        if (
            self.name in {".", ".."}
            or "/" in self.name
            or "\\" in self.name
            or any(ord(character) < 32 for character in self.name)
        ):
            raise ValueError("folder name must be one path component")
        if len(self.name.encode("utf-8")) > 255:
            raise ValueError("folder name is too long for the host filesystem")
        return self


def _folder(path: str | None, *, parent=False) -> Path:
    requested = Path(path).expanduser() if path else Path.home()
    if not requested.is_absolute():
        raise HTTPException(
            422,
            "parent folder path must be absolute"
            if parent
            else "folder path must be absolute",
        )
    try:
        current = requested.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise HTTPException(
            404, "parent folder is unavailable" if parent else "folder is unavailable"
        ) from error
    if not current.is_dir():
        raise HTTPException(
            422,
            "parent path is not a folder"
            if parent
            else "selected path is not a folder",
        )
    return current


def _list(path, offset, limit, filter, include_sqlite_files):
    current = _folder(path)
    name_filter = filter.strip().casefold()
    candidates = []
    try:
        with os.scandir(current) as entries:
            for entry in entries:
                try:
                    if name_filter and name_filter not in entry.name.casefold():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        kind = "directory"
                    elif (
                        include_sqlite_files
                        and entry.is_file(follow_symlinks=False)
                        and entry.name.lower().endswith((".db", ".sqlite", ".sqlite3"))
                    ):
                        kind = "file"
                    else:
                        continue
                    candidates.append((entry.name.casefold(), entry.name, kind))
                except OSError:
                    # diagnostic-expected: an unreadable entry is omitted from the host browser.
                    continue
    except OSError as error:
        raise HTTPException(403, "folder cannot be listed") from error
    candidates.sort()
    page = candidates[offset : offset + limit]
    more = offset + len(page) < len(candidates)
    return {
        "path": str(current),
        "parent": None if current.parent == current else str(current.parent),
        "directories": [
            {"name": name, "path": str(current / name)}
            for _, name, kind in page
            if kind == "directory"
        ],
        "files": [
            {"name": name, "path": str(current / name)}
            for _, name, kind in page
            if kind == "file"
        ],
        "truncated": more,
        "next_offset": offset + len(page) if more else None,
    }


async def list_host_workspace_folders(
    path: str | None = None,
    *,
    offset: int = 0,
    limit: int = 500,
    filter: str = "",
    include_sqlite_files: bool = False,
):
    return await asyncio.to_thread(
        _list, path, offset, limit, filter, include_sqlite_files
    )


def _create(request):
    parent = _folder(request.parent_path, parent=True)
    descriptor = None
    try:
        descriptor = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
        os.mkdir(request.name, mode=0o700, dir_fd=descriptor)
    except FileExistsError as error:
        raise HTTPException(409, "folder already exists") from error
    except PermissionError as error:
        raise HTTPException(403, "folder cannot be created") from error
    except OSError as error:
        raise HTTPException(422, "folder cannot be created") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _list(str(parent / request.name), 0, 500, "", False)


async def create_host_workspace_folder(request: HostWorkspaceFolderCreateRequest):
    # A cancelled response cannot release the exclusive host while its owned
    # filesystem mutation is still running in the worker thread.
    return await finish_cleanup(asyncio.to_thread(_create, request))
