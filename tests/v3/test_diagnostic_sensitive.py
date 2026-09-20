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
    detail = (
        "transport fd=9 Bearer top-secret-token-value password=canary-sensitive-detail"
    )

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


def test_sensitive_detail_falls_back_to_session_memory_without_vault(
    monkeypatch, tmp_path
) -> None:
    import keyring

    # keyring_backend=None means "ask the host", so a developer machine with an
    # unlocked vault must not quietly change what this test covers.
    monkeypatch.setattr(keyring, "get_keyring", lambda: None)
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


def test_locked_host_vault_selects_session_memory_without_prompting(
    monkeypatch, tmp_path
) -> None:
    """A locked collection must never reach keyring's blocking get_password."""

    import secretstorage
    from types import SimpleNamespace

    from keyring.backends.SecretService import Keyring

    class LockedVault(Keyring):
        # keyring's own priority probes the session bus; the reads and writes
        # below are the interactive calls that hang on a locked collection.
        __module__ = "keyring.backends.SecretService"
        priority = 5

        def get_password(self, *_: object) -> str | None:
            pytest.fail("a locked vault must not be read interactively")

        def set_password(self, *_: object) -> None:
            pytest.fail("a locked vault must not be written interactively")

    monkeypatch.setattr(
        secretstorage, "dbus_init", lambda: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(
        secretstorage,
        "get_collection_by_alias",
        lambda *_: SimpleNamespace(is_locked=lambda: True),
    )

    store = SensitiveDiagnosticStore(
        tmp_path, enabled=True, keyring_backend=LockedVault()
    )

    capture = store.capture(
        "err_locked_vault",
        "locked-vault detail",
        source="core",
        application_version="3.0.0-alpha.1",
    )

    assert capture.persistence == "session-memory"
    assert store.reveal("err_locked_vault") == "locked-vault detail"
    assert not (tmp_path / "core").exists()


def test_session_memory_detail_is_byte_capped_and_evicts_oldest_first(
    monkeypatch, tmp_path
) -> None:
    import keyring

    from nebula.v3.diagnostic_sensitive import MAX_SENSITIVE_MEMORY_BYTES

    monkeypatch.setattr(keyring, "get_keyring", lambda: None)
    store = SensitiveDiagnosticStore(tmp_path, enabled=True, keyring_backend=None)
    detail = "x" * MAX_SENSITIVE_DETAIL_BYTES
    fits = MAX_SENSITIVE_MEMORY_BYTES // MAX_SENSITIVE_DETAIL_BYTES
    count = fits + 8

    for index in range(count):
        capture = store.capture(
            f"err_mem{index}",
            detail,
            source="core",
            application_version="3.0.0-alpha.1",
        )
        assert capture.persistence == "session-memory"

    with pytest.raises(SensitiveDetailUnavailable):
        store.reveal("err_mem0")
    assert store.reveal(f"err_mem{count - 1}") == detail
    retained = 0
    for index in range(count):
        try:
            store.reveal(f"err_mem{index}")
        except SensitiveDetailUnavailable:
            continue
        retained += 1
    assert 0 < retained <= fits
    assert retained * MAX_SENSITIVE_DETAIL_BYTES <= MAX_SENSITIVE_MEMORY_BYTES


class TrustedVault(FakeVault):
    __module__ = "keyring.backends.SecretService"


class ChainedVault:
    """Stand in for keyring.backends.chainer.ChainerBackend."""

    __module__ = "keyring.backends.chainer"
    priority = 10.0

    def __init__(self, *backends) -> None:
        self.backends = list(backends)


def test_chained_keyring_selects_its_trusted_member_for_the_detail_key(
    tmp_path,
) -> None:
    plaintext = FakeVault()
    vault = TrustedVault()

    store = SensitiveDiagnosticStore(
        tmp_path, enabled=True, keyring_backend=ChainedVault(plaintext, vault)
    )

    assert store.persistence == "encrypted-vault"
    assert vault.values and not plaintext.values


def test_capture_preference_change_keeps_memory_only_detail(tmp_path) -> None:
    store = SensitiveDiagnosticStore(tmp_path, enabled=False, keyring_backend=None)
    store._keyring = None  # no host vault: memory-only capture

    store.set_enabled(True)
    assert store.persistence == "session-memory"
    capture = store.capture(
        "err_kept", "secret-ish detail", source="core", application_version="3.0.0"
    )
    assert capture.available is True

    store.set_enabled(True)
    assert store.reveal("err_kept") == "secret-ish detail"
    store.set_enabled(False)
    assert store.persistence == "disabled"
    store.set_enabled(True)
    assert store.reveal("err_kept") == "secret-ish detail"
