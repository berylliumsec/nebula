"""Encrypted, short-lived diagnostic detail storage.

This store is intentionally separate from exportable diagnostic logs.  It accepts
only a bounded textual exception summary supplied by the diagnostics owner.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol, cast

import keyring
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SENSITIVE_DETAIL_SCHEMA = "nebula.diagnostic-sensitive-detail/v1"
SENSITIVE_DETAIL_SERVICE = "io.berylliumsec.nebula.diagnostic-details"
SENSITIVE_DETAIL_KEY_NAME = "aes-256-gcm-v1"
MAX_SENSITIVE_DETAIL_BYTES = 64 * 1024
MAX_SENSITIVE_DIRECTORY_BYTES = 32 * 1024 * 1024
SENSITIVE_DETAIL_TTL = timedelta(hours=24)
_ERROR_ID = re.compile(r"^err_[A-Za-z0-9._:-]{1,123}$")
_TRUSTED_VAULT_BACKEND_MODULES = frozenset(
    {"keyring.backends.SecretService", "keyring.backends.macOS"}
)


class SensitiveDetailError(RuntimeError):
    pass


class SensitiveDetailUnavailable(SensitiveDetailError):
    pass


class SensitiveDetailExpired(SensitiveDetailUnavailable):
    pass


class KeyringBackend(Protocol):
    @property
    def priority(self) -> float: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def get_password(self, service_name: str, username: str) -> str | None: ...


@dataclass(frozen=True)
class SensitiveDetailCapture:
    available: bool
    expires_at: str | None = None
    persistence: str = "disabled"


class SensitiveDiagnosticStore:
    """Own encrypted Core detail or memory-only fallback for one process."""

    def __init__(
        self,
        root: Path,
        *,
        enabled: bool,
        owner: str = "core",
        keyring_backend: KeyringBackend | None = None,
        trust_injected_backend: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if owner not in {"core", "desktop"}:
            raise ValueError("sensitive diagnostic owner must be core or desktop")
        self.root = root.expanduser().resolve() / owner
        self.enabled = enabled
        self.owner = owner
        self._now = now or (lambda: datetime.now(UTC))
        self._memory: dict[str, tuple[datetime, str]] = {}
        self._memory_key = AESGCM.generate_key(bit_length=256)
        self._lock = threading.RLock()
        self._trust_injected_backend = trust_injected_backend
        if keyring_backend is None:
            try:
                keyring_backend = cast(KeyringBackend, keyring.get_keyring())
            except (
                Exception
            ):  # diagnostic-expected: an unavailable vault selects memory-only capture
                keyring_backend = None
        self._keyring = keyring_backend
        self._key: bytes | None = None
        self._durable = False
        if self.enabled:
            self._initialize_key()
            if self._durable:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with self._suppress_os_error():
                    self.root.chmod(0o700)
            self.prune()

    class _suppress_os_error:
        def __enter__(self) -> None:
            return None

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return isinstance(exc, OSError)

    @property
    def persistence(self) -> str:
        if not self.enabled:
            return "disabled"
        return "encrypted-vault" if self._durable else "session-memory"

    def _vault_available(self) -> bool:
        if self._keyring is None:
            return False
        module = type(self._keyring).__module__
        if (
            not self._trust_injected_backend
            and module not in _TRUSTED_VAULT_BACKEND_MODULES
        ):
            return False
        try:
            return bool(self._keyring.priority > 0)
        except (
            Exception
        ):  # diagnostic-expected: an unusable vault selects memory-only capture
            return False

    def _initialize_key(self) -> None:
        if not self._vault_available() or self._keyring is None:
            self._key = self._memory_key
            return
        try:
            encoded = self._keyring.get_password(
                SENSITIVE_DETAIL_SERVICE, SENSITIVE_DETAIL_KEY_NAME
            )
            if encoded is None:
                key = AESGCM.generate_key(bit_length=256)
                encoded = base64.b64encode(key).decode("ascii")
                self._keyring.set_password(
                    SENSITIVE_DETAIL_SERVICE, SENSITIVE_DETAIL_KEY_NAME, encoded
                )
            key = base64.b64decode(encoded, validate=True)
            if len(key) != 32:
                raise ValueError("diagnostic detail key has invalid length")
        except (
            Exception
        ):  # diagnostic-expected: malformed vault data selects memory-only capture
            self._key = self._memory_key
            self._durable = False
            return
        self._key = key
        self._durable = True

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return (
            value.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)

    @staticmethod
    def _validate_error_id(error_id: str) -> None:
        if not _ERROR_ID.fullmatch(error_id):
            raise ValueError("invalid diagnostic error identifier")

    def capture(
        self,
        error_id: str,
        detail: str,
        *,
        source: str,
        application_version: str,
    ) -> SensitiveDetailCapture:
        self._validate_error_id(error_id)
        if not self.enabled or self._key is None:
            return SensitiveDetailCapture(False)
        encoded = detail.replace("\x00", "�").encode("utf-8")
        if not encoded:
            return SensitiveDetailCapture(False, persistence=self.persistence)
        if len(encoded) > MAX_SENSITIVE_DETAIL_BYTES:
            encoded = encoded[
                : MAX_SENSITIVE_DETAIL_BYTES - len("\n[TRUNCATED]".encode())
            ]
            while True:
                try:
                    encoded.decode("utf-8")
                    break
                except (
                    UnicodeDecodeError
                ):  # diagnostic-expected: back up to a UTF-8 boundary
                    encoded = encoded[:-1]
            encoded += b"\n[TRUNCATED]"
        now = self._now().astimezone(UTC)
        expires = now + SENSITIVE_DETAIL_TTL
        metadata = {
            "schema": SENSITIVE_DETAIL_SCHEMA,
            "error_id": error_id,
            "source": source[:128],
            "owner": self.owner,
            "application_version": application_version[:128],
            "created_at": self._timestamp(now),
            "expires_at": self._timestamp(expires),
        }
        if not self._durable:
            with self._lock:
                self._memory[error_id] = (expires, encoded.decode("utf-8"))
            return SensitiveDetailCapture(
                True, metadata["expires_at"], self.persistence
            )
        aad = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, encoded, aad)
        envelope = {
            **metadata,
            "algorithm": "AES-256-GCM",
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = self.root / f".{error_id}.{os.urandom(8).hex()}.tmp"
            destination = self.root / f"{error_id}.json"
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(envelope, handle, sort_keys=True, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                destination.chmod(0o600)
            finally:
                with self._suppress_os_error():
                    temporary.unlink()
            self.prune()
        return SensitiveDetailCapture(True, metadata["expires_at"], self.persistence)

    def reveal(self, error_id: str) -> str:
        self._validate_error_id(error_id)
        if not self.enabled or self._key is None:
            raise SensitiveDetailUnavailable("sensitive detail capture was not enabled")
        now = self._now().astimezone(UTC)
        with self._lock:
            if not self._durable:
                stored = self._memory.get(error_id)
                if stored is None:
                    raise SensitiveDetailUnavailable("sensitive detail is unavailable")
                expires, detail = stored
                if expires <= now:
                    self._memory.pop(error_id, None)
                    raise SensitiveDetailExpired("sensitive detail has expired")
                return detail
            path = self.root / f"{error_id}.json"
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as exc:
                raise SensitiveDetailUnavailable(
                    "sensitive detail is unavailable"
                ) from exc
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SensitiveDetailUnavailable(
                    "sensitive detail could not be read"
                ) from exc
            expires_at = envelope.get("expires_at")
            if (
                not isinstance(expires_at, str)
                or self._parse_timestamp(expires_at) <= now
            ):
                with self._suppress_os_error():
                    path.unlink()
                raise SensitiveDetailExpired("sensitive detail has expired")
            metadata = {
                key: envelope.get(key)
                for key in (
                    "schema",
                    "error_id",
                    "source",
                    "owner",
                    "application_version",
                    "created_at",
                    "expires_at",
                )
            }
            if (
                metadata["schema"] != SENSITIVE_DETAIL_SCHEMA
                or metadata["error_id"] != error_id
                or metadata["owner"] != self.owner
            ):
                raise SensitiveDetailUnavailable("sensitive detail metadata is invalid")
            try:
                nonce = base64.b64decode(envelope["nonce"], validate=True)
                ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
                aad = json.dumps(
                    metadata, sort_keys=True, separators=(",", ":")
                ).encode()
                cleartext = AESGCM(self._key).decrypt(nonce, ciphertext, aad)
                return cleartext.decode("utf-8")
            except Exception as exc:
                raise SensitiveDetailUnavailable(
                    "sensitive detail authentication failed"
                ) from exc

    def prune(self) -> None:
        if not self.enabled:
            return
        now = self._now().astimezone(UTC)
        with self._lock:
            self._memory = {
                error_id: item
                for error_id, item in self._memory.items()
                if item[0] > now
            }
            if not self._durable or not self.root.exists():
                return
            retained: list[tuple[Path, datetime, int]] = []
            for path in self.root.glob("err_*.json"):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    envelope = json.loads(path.read_text(encoding="utf-8"))
                    expires = self._parse_timestamp(str(envelope["expires_at"]))
                    size = path.stat().st_size
                    mode = stat.S_IMODE(path.stat().st_mode)
                    if mode != 0o600:
                        path.chmod(0o600)
                except (
                    Exception
                ):  # diagnostic-expected: malformed protected files are pruned
                    with self._suppress_os_error():
                        path.unlink()
                    continue
                if expires <= now:
                    with self._suppress_os_error():
                        path.unlink()
                else:
                    retained.append((path, expires, size))
            total = sum(item[2] for item in retained)
            for path, _, size in sorted(retained, key=lambda item: item[1]):
                if total <= MAX_SENSITIVE_DIRECTORY_BYTES:
                    break
                with self._suppress_os_error():
                    path.unlink()
                    total -= size


__all__ = [
    "MAX_SENSITIVE_DETAIL_BYTES",
    "MAX_SENSITIVE_DIRECTORY_BYTES",
    "SENSITIVE_DETAIL_SCHEMA",
    "SENSITIVE_DETAIL_TTL",
    "SensitiveDetailCapture",
    "SensitiveDetailError",
    "SensitiveDetailExpired",
    "SensitiveDetailUnavailable",
    "SensitiveDiagnosticStore",
]
