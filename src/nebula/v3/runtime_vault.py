"""Host-private encrypted content with stable, keyed references for SQL receipts."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .runtime_ipc import RuntimeIpcError

MAX_CONTENT_BYTES = 16 * 1024**2


class RuntimeVault:
    def __init__(self, root: Path, *, maximum_bytes: int = MAX_CONTENT_BYTES):
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 64 * 1024**2:
            raise ValueError(
                "Runtime content bounds must be explicit and at most 64 MiB"
            )
        self.maximum_bytes = maximum_bytes
        if root.is_symlink():
            raise RuntimeIpcError("runtime payload storage cannot be a symlink")
        self.root = root.expanduser().absolute()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._private(self.root.stat(), directory=True)
        key_path = self.root / "key"
        if not key_path.exists():
            self._publish(key_path, secrets.token_bytes(32))
        self.key = self._read_file(key_path, maximum=32)
        if len(self.key) != 32:
            raise RuntimeIpcError("runtime payload storage key is invalid")
        self.index_key = hmac.digest(
            self.key, b"nebula.runtime-vault/index/v1", "sha256"
        )

    @staticmethod
    def _private(details: os.stat_result, *, directory=False):
        if (
            details.st_mode & 0o077
            or details.st_uid != os.getuid()
            or not (
                stat.S_ISDIR(details.st_mode)
                if directory
                else stat.S_ISREG(details.st_mode)
            )
        ):
            raise RuntimeIpcError(
                "runtime payload storage requires private, owned files"
            )

    @staticmethod
    def _purpose(value: str):
        if not isinstance(value, str) or not 1 <= len(value) <= 400 or "\x00" in value:
            raise RuntimeIpcError("invalid runtime payload purpose")

    def _publish(self, path: Path, raw: bytes):
        descriptor, name = tempfile.mkstemp(prefix=".payload-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            try:
                # Publish only a fully written file; concurrent initializers
                # can never observe a partially written key or ciphertext.
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                # diagnostic-expected: another publisher already installed the complete immutable key or ciphertext.
                pass
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_file(self, path: Path, *, maximum: int) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            self._private(os.fstat(descriptor))
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise RuntimeIpcError("runtime payload exceeds its storage bound")
        return raw

    def _reference(self, raw: bytes, purpose: str) -> str:
        return hmac.new(
            self.index_key, purpose.encode() + b"\x00" + raw, hashlib.sha256
        ).hexdigest()

    def put(self, raw: bytes, *, purpose: str) -> str:
        self._purpose(purpose)
        if not isinstance(raw, bytes) or len(raw) > self.maximum_bytes:
            raise RuntimeIpcError("runtime payload exceeds its storage bound")
        reference = self._reference(raw, purpose)
        nonce = secrets.token_bytes(12)
        aad = ("nebula.runtime-vault/v1\x00" + purpose + "\x00" + reference).encode()
        encoded = nonce + AESGCM(self.key).encrypt(nonce, raw, aad)
        self._publish(self.root / reference, encoded)
        # Also verify an existing file won by another process. Corruption must
        # hold admission rather than being mistaken for a duplicate submission.
        if self.get(reference, purpose=purpose) != raw:
            raise RuntimeIpcError("runtime payload failed integrity verification")
        return reference

    def get(self, reference: str, *, purpose: str) -> bytes:
        self._purpose(purpose)
        if not isinstance(reference, str) or not re.fullmatch(
            r"[a-f0-9]{64}", reference
        ):
            raise RuntimeIpcError("invalid runtime payload reference")
        encoded = self._read_file(
            self.root / reference, maximum=self.maximum_bytes + 28
        )
        aad = ("nebula.runtime-vault/v1\x00" + purpose + "\x00" + reference).encode()
        try:
            if len(encoded) < 28:
                raise InvalidTag()
            raw = AESGCM(self.key).decrypt(encoded[:12], encoded[12:], aad)
        except InvalidTag:
            raise RuntimeIpcError(
                "runtime payload failed integrity verification"
            ) from None
        if not hmac.compare_digest(reference, self._reference(raw, purpose)):
            raise RuntimeIpcError("runtime payload failed integrity verification")
        return raw

    def discard(self, reference: str, *, purpose: str) -> None:
        try:
            self.get(reference, purpose=purpose)
        except FileNotFoundError:
            # diagnostic-expected: discard is idempotent when an owned payload was already removed.
            return
        (self.root / reference).unlink(missing_ok=True)
