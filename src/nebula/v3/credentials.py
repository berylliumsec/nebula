"""Write-only provider credentials backed by the OS vault or process memory."""

from __future__ import annotations

from .diagnostics import record_caught_exception

import os
import re
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import uuid4

import keyring
from pydantic import SecretStr, field_validator

from .domain import NebulaModel

_REFERENCE = re.compile(
    r"^(?:env:[A-Za-z_][A-Za-z0-9_]*|(?:vault|session):[0-9a-f]{32})$"
)
_SERVICE_NAME = "io.berylliumsec.nebula.provider-credentials"
_TRUSTED_VAULT_BACKEND_MODULES = frozenset(
    {
        "keyring.backends.SecretService",
        "keyring.backends.macOS",
    }
)


class CredentialError(RuntimeError):
    pass


class CredentialUnavailableError(CredentialError):
    pass


class CredentialNotFoundError(CredentialError):
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
    persistence: Literal["environment", "vault", "session"]
    available: bool


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

    @property
    def vault_available(self) -> bool:
        if self.keyring_backend is None:
            return False
        # keyring can discover third-party fallback backends, including
        # plaintext files. Nebula only treats the two supported OS vault
        # integrations as durable credential storage and otherwise offers
        # session-only or env: references.
        backend_module = type(self.keyring_backend).__module__
        if backend_module not in _TRUSTED_VAULT_BACKEND_MODULES:
            return False
        try:
            priority = self.keyring_backend.priority
            return bool(priority and priority > 0)
        except Exception as caught_error:
            record_caught_exception(
                "providers",
                "providers.credentials.caught_failure_002",
                "A handled providers operation raised an exception.",
                caught_error,
                stage="credentials",
            )
            return False

    def create(self, request: CredentialCreateRequest) -> CredentialStatus:
        value = request.secret.get_secret_value()
        if not value or len(value) > 16_384:
            raise ValueError("credential secret must contain 1 to 16384 characters")
        identifier = uuid4().hex
        if request.persistence == "session":
            reference = f"session:{identifier}"
            self._session[reference] = SecretStr(value)
            return CredentialStatus(
                reference=reference, persistence="session", available=True
            )
        if not self.vault_available or self.keyring_backend is None:
            raise CredentialUnavailableError(
                "the operating-system credential vault is unavailable; use an "
                "environment reference or session-only credential"
            )
        try:
            self.keyring_backend.set_password(_SERVICE_NAME, identifier, value)
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
            reference=f"vault:{identifier}", persistence="vault", available=True
        )

    def status(self, reference: str) -> CredentialStatus:
        self._validate_reference(reference)
        if reference.startswith("env:"):
            name = reference.removeprefix("env:")
            return CredentialStatus(
                reference=reference,
                persistence="environment",
                available=bool(os.getenv(name)),
            )
        if reference.startswith("session:"):
            return CredentialStatus(
                reference=reference,
                persistence="session",
                available=reference in self._session,
            )
        return CredentialStatus(
            reference=reference,
            persistence="vault",
            available=self._vault_value(reference) is not None,
        )

    def resolve(self, reference: str) -> SecretStr:
        status = self.status(reference)
        if not status.available:
            raise CredentialNotFoundError(
                f"provider credential reference is unavailable: {reference}"
            )
        if reference.startswith("env:"):
            return SecretStr(os.environ[reference.removeprefix("env:")])
        if reference.startswith("session:"):
            return self._session[reference]
        value = self._vault_value(reference)
        if value is None:
            raise CredentialNotFoundError(
                f"provider credential reference is unavailable: {reference}"
            )
        return SecretStr(value)

    def delete(self, reference: str) -> None:
        self._validate_reference(reference)
        if reference.startswith("env:"):
            raise CredentialError("environment credentials are managed outside Nebula")
        if reference.startswith("session:"):
            self._session.pop(reference, None)
            return
        if self.keyring_backend is None or not self.vault_available:
            raise CredentialUnavailableError(
                "the operating-system credential vault is unavailable"
            )
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
        if self.keyring_backend is None or not self.vault_available:
            return None
        try:
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
    "valid_credential_reference",
]
