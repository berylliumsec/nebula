"""Shared, prompt-free view of the operating-system credential vault.

The credential store and the sensitive diagnostic store both keep secrets in
the OS vault, and both must decide whether that vault can be used *without*
raising a desktop unlock prompt. keyring's own ``get_password`` and
``set_password`` wait indefinitely on a locked Linux collection when no
prompter is running, which is the ordinary state for a headless Core, so the
lock state has to be read before any keyring call.

This module imports nothing from Nebula, so either store can use it; callers
that classify diagnostics pass ``on_caught``.
"""

from __future__ import annotations

from contextlib import closing
from typing import Any, Callable, Literal, Protocol

from keyring.backends.SecretService import Keyring

VaultState = Literal["available", "locked", "unavailable"]

# keyring can discover third-party fallback backends, including plaintext
# files. Nebula only treats the two supported OS vault integrations as durable
# credential storage and otherwise offers session-only or env: references.
TRUSTED_VAULT_BACKEND_MODULES = frozenset(
    {
        "keyring.backends.SecretService",
        "keyring.backends.macOS",
    }
)

# keyring selects its chainer when more than one backend is viable (a KDE
# desktop with kwallet and SecretService, say). The chain is not a vault
# Nebula supports, but one of its members may be.
CHAINER_BACKEND_MODULE = "keyring.backends.chainer"

CaughtHandler = Callable[[Exception], None]


class KeyringBackend(Protocol):
    @property
    def priority(self) -> float: ...


def resolve_vault_backend(backend: Any) -> Any:
    """The backend to talk to: a chainer's first trusted member, else ``backend``.

    Without this a host whose keyring picks ``ChainerBackend`` is reported as
    "vault unavailable" although its SecretService member works.
    """

    if backend is None or type(backend).__module__ != CHAINER_BACKEND_MODULE:
        return backend
    try:
        members = list(backend.backends)
    except Exception:  # diagnostic-expected: an unreadable chain fails the module check
        return backend
    for member in members:
        if type(member).__module__ in TRUSTED_VAULT_BACKEND_MODULES:
            return member
    return backend


def backend_usable(
    backend: Any,
    *,
    trust_backend: bool = False,
    on_caught: CaughtHandler | None = None,
) -> bool:
    """Whether this backend is a supported vault, without touching the vault."""

    backend = resolve_vault_backend(backend)
    if backend is None:
        return False
    if not trust_backend and type(backend).__module__ not in (
        TRUSTED_VAULT_BACKEND_MODULES
    ):
        return False
    try:
        priority = backend.priority
        return bool(priority and priority > 0)
    except Exception as caught_error:  # diagnostic-expected: reported by on_caught
        if on_caught is not None:
            on_caught(caught_error)
        return False


def secret_service_collection(backend: Any, connection: Any) -> Any:
    """The collection keyring itself would use, resolved without a prompt."""

    import secretstorage

    preferred = getattr(backend, "preferred_collection", None)
    if preferred is not None:
        return secretstorage.Collection(connection, preferred)
    return secretstorage.get_collection_by_alias(connection, "default")


def secret_service_state(
    backend: Any, *, on_caught: CaughtHandler | None = None
) -> VaultState:
    """Read the Linux collection's lock state without raising a prompt."""

    try:
        # secretstorage is a Linux-only dependency, so keyring installs it only
        # there. Importing it here keeps Core importable on macOS and Windows,
        # where this backend is never selected.
        import secretstorage

        with closing(secretstorage.dbus_init()) as connection:
            collection = secret_service_collection(backend, connection)
            return "locked" if collection.is_locked() else "available"
    except Exception as caught_error:  # diagnostic-expected: reported by on_caught
        if on_caught is not None:
            on_caught(caught_error)
        return "unavailable"


def vault_state(
    backend: Any,
    *,
    trust_backend: bool = False,
    on_caught: CaughtHandler | None = None,
) -> VaultState:
    """Whether the OS vault can hold a secret right now.

    A Linux vault is usually present but locked on a headless host: nothing
    unlocks the collection when Core runs as a service with no desktop login.
    Reporting that as available offers a save that always fails.
    """

    backend = resolve_vault_backend(backend)
    if not backend_usable(backend, trust_backend=trust_backend, on_caught=on_caught):
        return "unavailable"
    if isinstance(backend, Keyring):
        return secret_service_state(backend, on_caught=on_caught)
    return "available"


__all__ = [
    "CHAINER_BACKEND_MODULE",
    "TRUSTED_VAULT_BACKEND_MODULES",
    "VaultState",
    "backend_usable",
    "resolve_vault_backend",
    "secret_service_collection",
    "secret_service_state",
    "vault_state",
]
