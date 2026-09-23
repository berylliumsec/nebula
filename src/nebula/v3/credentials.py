"""Write-only provider credentials backed by the OS vault or process memory."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import os
from contextlib import closing
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

import keyring
from keyring.backends.SecretService import Keyring
from pydantic import SecretStr, field_validator

from .domain import NebulaModel
from .vault_probe import (
    VaultState,
    backend_usable,
    resolve_vault_backend,
    secret_service_collection,
)
from .vault_probe import vault_state as probe_vault_state

_REFERENCE = re.compile(
    r"^(?:env:[A-Za-z_][A-Za-z0-9_]*|systemd:[A-Za-z0-9_.-]{1,128}|(?:vault|session):[0-9a-f]{32})$"
)
_SERVICE_NAME = "io.berylliumsec.nebula.provider-credentials"
_MAX_SECRET_BYTES = 16_384

_VAULT_LOCKED_DETAIL = (
    "the operating-system credential vault is locked; unlock it on the Nebula "
    "host, or use an environment reference or session-only credential"
)
_VAULT_UNAVAILABLE_DETAIL = (
    "the operating-system credential vault is unavailable; use an "
    "environment reference or session-only credential"
)


class CredentialError(RuntimeError):
    pass


class CredentialUnavailableError(CredentialError):
    pass


class CredentialNotFoundError(CredentialError):
    pass


class CredentialVaultLockedError(CredentialUnavailableError):
    pass


class KeyringBackend(Protocol):
    @property
    def priority(self) -> float: ...
    def set_password(self, service_name: str, username: str, password: str) -> None: ...
    def get_password(self, service_name: str, username: str) -> str | None: ...
    def delete_password(self, service_name: str, username: str) -> None: ...


class CredentialCreateRequest(NebulaModel):
    secret: SecretStr
    persistence: Literal["vault", "session"] = "vault"

    @field_validator("secret")
    @classmethod
    def _bounded_secret(cls, value: SecretStr) -> SecretStr:
        length = len(value.get_secret_value())
        if length < 1 or length > 16_384:
            raise ValueError("credential secret must contain 1 to 16384 characters")
        return value


class CredentialStatus(NebulaModel):
    reference: str
    persistence: Literal["environment", "systemd", "vault", "session"]
    available: bool
    state: Literal[
        "available",
        "locked",
        "missing",
        "backend_unavailable",
        "session_expired",
        "environment_missing",
        "service_credential_missing",
    ]


@dataclass
class CredentialStore:
    keyring_backend: KeyringBackend | None = None

    def __post_init__(self) -> None:
        self._session: dict[str, SecretStr] = {}
        if self.keyring_backend is None:
            try:
                self.keyring_backend = cast(KeyringBackend, keyring.get_keyring())
            except Exception as caught_error:
                # The package is a required Core dependency, but an OS vault
                # backend may still be absent, disabled, or fail to initialize.
                record_caught_exception(
                    "providers",
                    "providers.credentials.caught_failure_001",
                    "A handled providers operation raised an exception.",
                    caught_error,
                    stage="credentials",
                )
                self.keyring_backend = None
        # keyring hands out its chainer when several backends are viable;
        # Nebula talks to the trusted member, not the chain.
        self.keyring_backend = resolve_vault_backend(self.keyring_backend)

    @property
    def vault_state(self) -> VaultState:
        """Whether the OS vault can store a credential right now.

        A Linux vault is usually present but locked on a headless host: nothing
        unlocks the collection when Core runs as a service with no desktop
        login. Reporting that as available offers a save that always fails, so
        the lock state is part of the answer.
        """

        return probe_vault_state(
            self.keyring_backend, on_caught=self._record_vault_probe_failure
        )

    @property
    def vault_available(self) -> bool:
        return self.vault_state == "available"

    def _backend_usable(self) -> bool:
        return backend_usable(
            self.keyring_backend, on_caught=self._record_vault_probe_failure
        )

    @staticmethod
    def _record_vault_probe_failure(caught_error: Exception) -> None:
        record_caught_exception(
            "providers",
            "providers.credentials.caught_failure_002",
            "A handled providers operation raised an exception.",
            caught_error,
            stage="credentials",
        )

    def create(self, request: CredentialCreateRequest) -> CredentialStatus:
        value = request.secret.get_secret_value()
        if not value or len(value) > 16_384:
            raise ValueError("credential secret must contain 1 to 16384 characters")
        identifier = uuid4().hex
        if request.persistence == "session":
            reference = f"session:{identifier}"
            self._session[reference] = SecretStr(value)
            return CredentialStatus(
                reference=reference,
                persistence="session",
                available=True,
                state="available",
            )
        state = self.vault_state
        if state != "available" or self.keyring_backend is None:
            raise CredentialUnavailableError(
                _VAULT_LOCKED_DETAIL if state == "locked" else _VAULT_UNAVAILABLE_DETAIL
            )
        try:
            if isinstance(self.keyring_backend, Keyring):
                self._secret_service_write(identifier, value)
            else:
                self.keyring_backend.set_password(_SERVICE_NAME, identifier, value)
        except CredentialUnavailableError:
            raise
        except Exception as exc:
            record_caught_exception(
                "providers",
                "providers.credentials.caught_failure_003",
                "A handled providers operation raised an exception.",
                exc,
                stage="credentials",
            )
            raise CredentialUnavailableError(
                "the operating-system credential vault could not save the credential"
            ) from exc
        return CredentialStatus(
            reference=f"vault:{identifier}",
            persistence="vault",
            available=True,
            state="available",
        )

    def status(self, reference: str) -> CredentialStatus:
        self._validate_reference(reference)
        if reference.startswith("env:"):
            name = reference.removeprefix("env:")
            available = bool(os.getenv(name))
            return CredentialStatus(
                reference=reference,
                persistence="environment",
                available=available,
                state="available" if available else "environment_missing",
            )
        if reference.startswith("systemd:"):
            available = self._systemd_value(reference) is not None
            return CredentialStatus(
                reference=reference,
                persistence="systemd",
                available=available,
                state="available" if available else "service_credential_missing",
            )
        if reference.startswith("session:"):
            available = reference in self._session
            return CredentialStatus(
                reference=reference,
                persistence="session",
                available=available,
                state="available" if available else "session_expired",
            )
        vault_state = self.vault_state
        if vault_state == "locked":
            return CredentialStatus(
                reference=reference,
                persistence="vault",
                available=False,
                state="locked",
            )
        if vault_state != "available":
            return CredentialStatus(
                reference=reference,
                persistence="vault",
                available=False,
                state="backend_unavailable",
            )
        available = self._vault_value(reference) is not None
        return CredentialStatus(
            reference=reference,
            persistence="vault",
            available=available,
            state="available" if available else "missing",
        )

    def resolve(self, reference: str) -> SecretStr:
        status = self.status(reference)
        if not status.available:
            if status.state == "locked":
                raise CredentialVaultLockedError(
                    "the operating-system credential vault is locked; unlock it on "
                    "the Nebula host and retry"
                )
            raise CredentialNotFoundError(
                f"provider credential reference is unavailable: {reference}"
            )
        if reference.startswith("env:"):
            return SecretStr(os.environ[reference.removeprefix("env:")])
        if reference.startswith("session:"):
            return self._session[reference]
        if reference.startswith("systemd:"):
            value = self._systemd_value(reference)
            if value is None:
                raise CredentialNotFoundError(
                    f"provider service credential is unavailable: {reference}"
                )
            return SecretStr(value)
        value = self._vault_value(reference)
        if value is None:
            if self.vault_state == "locked":
                raise CredentialVaultLockedError(
                    "the operating-system credential vault is locked; unlock it on "
                    "the Nebula host and retry"
                )
            raise CredentialNotFoundError(
                f"provider credential reference is unavailable: {reference}"
            )
        return SecretStr(value)

    def delete(self, reference: str) -> None:
        self._validate_reference(reference)
        if reference.startswith(("env:", "systemd:")):
            raise CredentialError(
                "environment and service credentials are managed outside Nebula"
            )
        if reference.startswith("session:"):
            self._session.pop(reference, None)
            return
        state = self.vault_state
        if self.keyring_backend is None or state != "available":
            raise CredentialUnavailableError(
                _VAULT_LOCKED_DETAIL if state == "locked" else _VAULT_UNAVAILABLE_DETAIL
            )
        if isinstance(self.keyring_backend, Keyring):
            try:
                self._secret_service_delete(reference)
            except CredentialUnavailableError:
                raise
            except Exception as exc:
                raise CredentialUnavailableError(
                    "The credential could not be deleted from the host vault. "
                    "Unlock the vault on the Nebula host and retry."
                ) from exc
            return
        try:
            self.keyring_backend.delete_password(
                _SERVICE_NAME, reference.removeprefix("vault:")
            )
        except Exception as exc:
            # Deletion stays idempotent for backends that report a missing item.
            record_caught_exception(
                "providers",
                "providers.credentials.caught_failure_004",
                "A handled providers operation raised an exception.",
                exc,
                stage="credentials",
            )
            if self._vault_value(reference) is not None:
                raise CredentialUnavailableError(
                    "the operating-system credential vault could not delete the credential"
                ) from exc

    def _vault_value(self, reference: str) -> str | None:
        if self.keyring_backend is None or not self._backend_usable():
            return None
        try:
            if (
                type(self.keyring_backend).__module__
                == "keyring.backends.SecretService"
            ):
                if isinstance(self.keyring_backend, Keyring):
                    return self._secret_service_value(reference)
            return self.keyring_backend.get_password(
                _SERVICE_NAME, reference.removeprefix("vault:")
            )
        except Exception as caught_error:
            record_caught_exception(
                "providers",
                "providers.credentials.caught_failure_005",
                "A handled providers operation raised an exception.",
                caught_error,
                stage="credentials",
            )
            return None

    @staticmethod
    def _systemd_value(reference: str) -> str | None:
        """Read one systemd service credential without accepting a path."""

        name = reference.removeprefix("systemd:")
        directory = os.getenv("CREDENTIALS_DIRECTORY")
        if not directory:
            return None
        root = Path(directory)
        try:
            root_stat = root.stat()
            if not stat.S_ISDIR(root_stat.st_mode):
                return None
            descriptor = os.open(
                root / name,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except (FileNotFoundError, NotADirectoryError, OSError):
            return None
        try:
            item_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(item_stat.st_mode)
                or item_stat.st_size > _MAX_SECRET_BYTES
            ):
                return None
            chunks: list[bytes] = []
            remaining = _MAX_SECRET_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(descriptor)
        if not raw or len(raw) > _MAX_SECRET_BYTES:
            return None
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        if not raw:
            return None
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _secret_service_collection(self, connection: object) -> Any:
        return secret_service_collection(self.keyring_backend, connection)

    def _secret_service_write(self, identifier: str, value: str) -> None:
        """Save in an unlocked existing collection without running any prompt."""
        import secretstorage
        from secretstorage.collection import SS_PREFIX, format_secret, open_session

        backend = cast(Keyring, self.keyring_backend)
        with closing(secretstorage.dbus_init()) as connection:
            collection = self._secret_service_collection(connection)
            if collection.is_locked():
                raise CredentialUnavailableError(
                    "The host credential vault is locked. Unlock it on the Nebula host "
                    "and retry, or choose session-only storage."
                )
            session = collection.session or open_session(connection)
            properties = {
                SS_PREFIX + "Item.Label": ("s", f"Nebula credential {identifier}"),
                SS_PREFIX + "Item.Attributes": (
                    "a{ss}",
                    backend._query(
                        _SERVICE_NAME, identifier, application=backend.appid
                    ),
                ),
            }
            item_path, _prompt = collection._collection.call(
                "CreateItem",
                "a{sv}(oayays)b",
                properties,
                format_secret(session, value.encode("utf-8"), "text/plain"),
                True,
            )
            if len(item_path) <= 1:
                raise CredentialUnavailableError(
                    "The host vault requires confirmation. Unlock it on the Nebula host "
                    "and retry, or choose session-only storage."
                )

    def _secret_service_delete(self, reference: str) -> None:
        """Never invoke a desktop unlock or confirmation prompt from Core."""
        import secretstorage

        backend = self.keyring_backend
        with closing(secretstorage.dbus_init()) as connection:
            collection = self._secret_service_collection(connection)
            if collection.is_locked():
                raise CredentialUnavailableError(
                    "The host credential vault is locked. Unlock it on the Nebula host and retry."
                )
            query = backend._query(  # type: ignore[union-attr]
                _SERVICE_NAME, reference.removeprefix("vault:")
            )
            for item in collection.search_items(query):
                item.ensure_not_locked()
                # Item.delete() executes an interactive prompt if Delete returns
                # one. Submit only the D-Bus operation; never wait for that prompt.
                (prompt,) = item._item.call("Delete", "")
                if prompt != "/":
                    raise CredentialUnavailableError(
                        "The host vault requires confirmation. Delete this credential "
                        "in the host password manager, then retry in Nebula."
                    )

    def _secret_service_value(self, reference: str) -> str | None:
        """Read an existing Linux vault without ever prompting to unlock/create it.

        Status is also called by async API endpoints. SecretService's ordinary
        get_password can wait indefinitely for a desktop unlock prompt there.
        Locked collections/items must instead report unavailable.
        """
        import secretstorage

        backend = self.keyring_backend
        with closing(secretstorage.dbus_init()) as connection:
            collection = self._secret_service_collection(connection)
            if collection.is_locked():
                return None
            query = backend._query(  # type: ignore[union-attr]
                _SERVICE_NAME, reference.removeprefix("vault:")
            )
            for item in collection.search_items(query):
                if item.is_locked():
                    return None
                return item.get_secret().decode("utf-8")
        return None

    @staticmethod
    def _validate_reference(reference: str) -> None:
        if not _REFERENCE.fullmatch(reference):
            raise ValueError("invalid credential reference")


def valid_credential_reference(value: str) -> bool:
    return bool(_REFERENCE.fullmatch(value))


__all__ = [
    "CredentialCreateRequest",
    "CredentialError",
    "CredentialNotFoundError",
    "CredentialStatus",
    "CredentialStore",
    "CredentialUnavailableError",
    "CredentialVaultLockedError",
    "VaultState",
    "valid_credential_reference",
]
