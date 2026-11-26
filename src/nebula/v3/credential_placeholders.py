"""``{{credential:<name>}}`` placeholders in tool arguments.

A model writes the placeholder where a saved password belongs. Core replaces it
with the value only in the copy sent to the tool, and replaces any echo of the
value in the tool's result with the placeholder again. Persisted arguments,
approval cards, hooks, and provider history keep the placeholder.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

PLACEHOLDER = re.compile(r"\{\{\s*credential:([a-z0-9][a-z0-9-]{0,63})\s*\}\}")
_MAX_DEPTH = 32


def placeholder(name: str) -> str:
    return "{{credential:" + name + "}}"


def find_placeholders(value: Any, *, _depth: int = 0) -> set[str]:
    """Return every credential name a JSON-like value refers to."""

    if _depth > _MAX_DEPTH:
        return set()
    if isinstance(value, str):
        return set(PLACEHOLDER.findall(value))
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                found |= set(PLACEHOLDER.findall(key))
            found |= find_placeholders(item, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= find_placeholders(item, _depth=_depth + 1)
    return found


def substitute(value: Any, secrets: Mapping[str, str], *, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with each known placeholder replaced."""

    if _depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda match: secrets.get(match[1], match[0]), value)
    if isinstance(value, Mapping):
        return {
            key: substitute(item, secrets, _depth=_depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [substitute(item, secrets, _depth=_depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(substitute(item, secrets, _depth=_depth + 1) for item in value)
    return value


def scrub(value: Any, secrets: Mapping[str, str], *, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with every secret value put back as a placeholder.

    Longer values are replaced first so a secret that contains another one is
    not left partly visible.
    """

    pairs = sorted(
        ((secret, placeholder(name)) for name, secret in secrets.items() if secret),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    if not pairs:
        return value
    return _scrub(value, pairs, _depth)


def _scrub(value: Any, pairs: list[tuple[str, str]], depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        for secret, marker in pairs:
            value = value.replace(secret, marker)
        return value
    if isinstance(value, Mapping):
        return {
            _scrub(key, pairs, depth + 1): _scrub(item, pairs, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, pairs, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, pairs, depth + 1) for item in value)
    return value


def scrub_bytes(value: bytes, secrets: Mapping[str, str]) -> bytes:
    """Captured process output with every secret put back as a placeholder."""

    pairs = sorted(
        (
            (secret.encode("utf-8"), placeholder(name).encode("utf-8"))
            for name, secret in secrets.items()
            if secret
        ),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for secret, marker in pairs:
        value = value.replace(secret, marker)
    return value


def environment_name(name: str) -> str:
    """The variable a run_command placeholder becomes, e.g. NEBULA_CREDENTIAL_MAC_LOGIN."""

    return "NEBULA_CREDENTIAL_" + name.upper().replace("-", "_")


def to_environment_references(command: str) -> str:
    """Rewrite each placeholder in a shell command as its variable reference."""

    return PLACEHOLDER.sub(
        lambda match: "${" + environment_name(match[1]) + "}", command
    )


# Values substituted into the tool call running in this task. Diagnostics read
# it so an exception message that quotes an argument never records a value.
_ACTIVE: ContextVar[Mapping[str, str]] = ContextVar(
    "nebula_saved_credentials_active", default={}
)


@contextmanager
def substituted(secrets: Mapping[str, str]) -> Iterator[None]:
    """Mark ``secrets`` as in use by the tool call running in this task."""

    token = _ACTIVE.set({**_ACTIVE.get(), **secrets})
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def scrub_active(text: str) -> str:
    """``text`` with any value substituted into the running tool call put back."""

    active = _ACTIVE.get()
    return scrub(text, active) if active else text


__all__ = [
    "PLACEHOLDER",
    "scrub_active",
    "substituted",
    "environment_name",
    "find_placeholders",
    "placeholder",
    "scrub",
    "scrub_bytes",
    "substitute",
    "to_environment_references",
]
