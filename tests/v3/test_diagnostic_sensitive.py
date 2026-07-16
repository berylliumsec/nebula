from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from nebula.v3.diagnostic_sensitive import (
    MAX_SENSITIVE_DETAIL_BYTES,
    SensitiveDetailExpired,
    SensitiveDetailUnavailable,
    SensitiveDiagnosticStore,
)


class FakeVault:
    priority = 1.0

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))


def test_sensitive_detail_is_authenticated_encrypted_bounded_and_expires(
    tmp_path,
) -> None:
    vault = FakeVault()
    now = [datetime(2026, 7, 16, tzinfo=UTC)]
    store = SensitiveDiagnosticStore(
        tmp_path,
        enabled=True,
        keyring_backend=vault,
        trust_injected_backend=True,
        now=lambda: now[0],
    )
    detail = "transport fd=9 Bearer top-secret-token-value password=canary-sensitive-detail"

    capture = store.capture(
        "err_encrypted",
        detail,
        source="core",
        application_version="3.0.0-alpha.1",
    )

    assert capture.available is True
    assert capture.persistence == "encrypted-vault"
    encrypted = (tmp_path / "core" / "err_encrypted.json").read_bytes()
    assert detail.encode() not in encrypted
    assert store.reveal("err_encrypted") == detail

    oversized = "x" * (MAX_SENSITIVE_DETAIL_BYTES + 100)
    store.capture(
        "err_bounded",
        oversized,
        source="core",
        application_version="3.0.0-alpha.1",
    )
    bounded = store.reveal("err_bounded")
    assert len(bounded.encode()) <= MAX_SENSITIVE_DETAIL_BYTES
    assert bounded.endswith("[TRUNCATED]")

    envelope_path = tmp_path / "core" / "err_encrypted.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"]))
    ciphertext[-1] ^= 1
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(SensitiveDetailUnavailable, match="authentication"):
        store.reveal("err_encrypted")

    now[0] += timedelta(hours=25)
    with pytest.raises(SensitiveDetailExpired):
        store.reveal("err_bounded")
    store.prune()
    assert not list((tmp_path / "core").glob("err_*.json"))


def test_sensitive_detail_falls_back_to_session_memory_without_vault(tmp_path) -> None:
    store = SensitiveDiagnosticStore(
        tmp_path,
        enabled=True,
        keyring_backend=None,
    )

    capture = store.capture(
        "err_memory",
        "memory-only detail",
        source="core",
        application_version="3.0.0-alpha.1",
    )

    assert capture.persistence == "session-memory"
    assert store.reveal("err_memory") == "memory-only detail"
    assert not (tmp_path / "core").exists()
