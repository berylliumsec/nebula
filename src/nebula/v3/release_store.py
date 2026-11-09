"""Verify and retain immutable worker assets outside a replaceable desktop."""

from __future__ import annotations

import hashlib
import errno
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .deployment_protocol import ReleaseManifest

MANIFEST_NAME = "nebula-runtime.json"


class ReleaseIntegrityError(ValueError):
    pass


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ReleaseStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def verify(directory: Path, *, expected_manifest_sha256: str) -> ReleaseManifest:
        if directory.is_symlink() or not directory.is_dir():
            raise ReleaseIntegrityError(
                "runtime bundle root must be a retained directory, not a symlink"
            )
        manifest_path = directory / MANIFEST_NAME
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > 16 * 1024**2
        ):
            raise ReleaseIntegrityError("runtime bundle manifest is missing or invalid")
        if _hash_file(manifest_path) != expected_manifest_sha256:
            raise ReleaseIntegrityError(
                "runtime bundle manifest checksum differs from the accepted artifact"
            )
        manifest = ReleaseManifest.model_validate_json(manifest_path.read_bytes())
        for relative, expected in manifest.files.items():
            selected = directory / relative
            current = selected
            while current != directory:
                if current.is_symlink():
                    raise ReleaseIntegrityError(
                        "runtime bundles cannot contain symlinked assets"
                    )
                current = current.parent
            if (
                not selected.is_file()
                or not stat.S_ISREG(selected.stat().st_mode)
                or _hash_file(selected) != expected
            ):
                raise ReleaseIntegrityError(
                    "runtime bundle asset checksum failed: " + relative
                )
        return manifest

    def install(
        self, source: Path, *, expected_manifest_sha256: str
    ) -> tuple[ReleaseManifest, Path]:
        source = source.expanduser().resolve(strict=True)
        if self.root == source or self.root.is_relative_to(source):
            raise ReleaseIntegrityError(
                "retained runtimes must live outside the replaceable application"
            )
        manifest = self.verify(
            source, expected_manifest_sha256=expected_manifest_sha256
        )
        destination = self.root / manifest.version
        if destination.exists():
            installed = self.verify(
                destination, expected_manifest_sha256=expected_manifest_sha256
            )
            if installed != manifest:
                raise ReleaseIntegrityError(
                    "a resident runtime version cannot be replaced in place"
                )
            return installed, destination
        temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.root))
        try:
            for relative in [*manifest.files, MANIFEST_NAME]:
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, target)
                executable = bool((source / relative).stat().st_mode & 0o111)
                target.chmod(0o555 if executable else 0o444)
            self.verify(temporary, expected_manifest_sha256=expected_manifest_sha256)
            for directory, _children, _files in os.walk(temporary, topdown=False):
                Path(directory).chmod(0o555)
            try:
                temporary.rename(destination)
            except OSError as error:
                if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise
                # diagnostic-expected: concurrent staging accepts identical immutable assets.
                self.verify(
                    destination, expected_manifest_sha256=expected_manifest_sha256
                )
            return manifest, destination
        finally:
            if temporary.exists():
                for directory, _children, _files in os.walk(temporary):
                    Path(directory).chmod(0o700)
                shutil.rmtree(temporary)

    def retained(self) -> list[tuple[ReleaseManifest, Path]]:
        result = []
        for directory in sorted(self.root.iterdir()):
            path = directory / MANIFEST_NAME
            if directory.is_dir() and not directory.is_symlink() and path.is_file():
                result.append(
                    (
                        self.verify(
                            directory, expected_manifest_sha256=_hash_file(path)
                        ),
                        directory,
                    )
                )
        return result
