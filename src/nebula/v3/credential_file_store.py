"""Encrypted credential files for hosts whose OS vault is locked or absent.

A headless Linux Core cannot unlock the desktop Secret Service, so the OS vault
refuses every save. This store keeps each secret as one AES-256-GCM file under
the Core data directory, keyed by a random 32-byte key file that only the Core
user can read. It protects secrets in database rows, backups, transcripts and
diagnostics; it does not protect them from root or the Core user on the host.

Every process of one Core (API host, API worker, execution workers) shares the
data directory and user, so each resolves ``store:`` references locally.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_AAD = b"nebula.credential-file/v1\x00"
_MAX_PLAINTEXT_BYTES = 16_384
_NONCE_BYTES = 12
_TAG_BYTES = 16


class CredentialFileError(RuntimeError):
    """The encrypted credential directory is unsafe or a file is corrupt."""


def default_credential_file_root() -> Path:
    data = os.getenv("NEBULA_V3_DATA_DIR")
    base = Path(data) if data else Path.home() / ".local" / "share" / "nebula" / "v3"
    return base.expanduser().absolute() / "credentials"


_configured_root: Path | None = None
_configured_lock = threading.Lock()


def configure_credential_file_root(data_root: str | Path) -> None:
    """Point this process's credential files at one Core's data directory."""

    global _configured_root
    with _configured_lock:
        _configured_root = Path(data_root).expanduser().absolute() / "credentials"


def credential_file_root() -> Path:
    return _configured_root or default_credential_file_root()


class EncryptedCredentialFiles:
    """Owner-only AES-256-GCM files, one per ``store:`` identifier."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink():
            raise CredentialFileError("credential storage cannot be a symlink")
        self.root = root.expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._private(self.root.stat(), directory=True)
        key_path = self.root / "key"
        if not key_path.exists():
            self._publish(key_path, secrets.token_bytes(32))
        self._key = self._read(key_path, maximum=32)
        if len(self._key) != 32:
            raise CredentialFileError("credential storage key is invalid")

    @staticmethod
    def _private(details: os.stat_result, *, directory: bool = False) -> None:
        if (
            details.st_mode & 0o077
            or details.st_uid != os.getuid()
            or not (
                stat.S_ISDIR(details.st_mode)
                if directory
                else stat.S_ISREG(details.st_mode)
            )
        ):
            raise CredentialFileError(
                "credential storage requires private files owned by the Core user"
            )

    @staticmethod
    def _identifier(identifier: str) -> str:
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            raise ValueError("invalid credential file identifier")
        return identifier

    def _publish(self, path: Path, raw: bytes) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".credential-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            try:
                # Only a fully written file becomes visible; a concurrent key
                # initializer keeps the first key and this copy is discarded.
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                # diagnostic-expected: another process already published this immutable file.
                pass
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, path: Path, *, maximum: int) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            self._private(os.fstat(descriptor))
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise CredentialFileError("credential file exceeds its storage bound")
        return raw

    def _aad(self, identifier: str) -> bytes:
        return _AAD + identifier.encode()

    def write(self, value: str) -> str:
        raw = value.encode("utf-8")
        if not 1 <= len(raw) <= _MAX_PLAINTEXT_BYTES:
            raise ValueError("credential secret must contain 1 to 16384 bytes")
        identifier = secrets.token_hex(16)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = nonce + AESGCM(self._key).encrypt(nonce, raw, self._aad(identifier))
        self._publish(self.root / identifier, sealed)
        if self.read(identifier) != value:
            raise CredentialFileError("credential file failed integrity verification")
        return identifier

    def exists(self, identifier: str) -> bool:
        return (self.root / self._identifier(identifier)).is_file()

    def read(self, identifier: str) -> str | None:
        path = self.root / self._identifier(identifier)
        try:
            sealed = self._read(
                path, maximum=_MAX_PLAINTEXT_BYTES + _NONCE_BYTES + _TAG_BYTES
            )
        except FileNotFoundError:
            # diagnostic-expected: a deleted or never-saved credential is missing.
            return None
        try:
            if len(sealed) < _NONCE_BYTES + _TAG_BYTES:
                raise InvalidTag()
            raw = AESGCM(self._key).decrypt(
                sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:], self._aad(identifier)
            )
        except InvalidTag:
            raise CredentialFileError(
                "credential file failed integrity verification"
            ) from None
        return raw.decode("utf-8")

    def delete(self, identifier: str) -> None:
        (self.root / self._identifier(identifier)).unlink(missing_ok=True)


__all__ = [
    "CredentialFileError",
    "EncryptedCredentialFiles",
    "configure_credential_file_root",
    "credential_file_root",
    "default_credential_file_root",
]
