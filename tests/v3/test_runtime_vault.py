import concurrent.futures
import os

import pytest

from nebula.v3.runtime_ipc import RuntimeIpcError
from nebula.v3.runtime_vault import MAX_CONTENT_BYTES, RuntimeVault


def test_ciphertext_references_survive_concurrent_process_initializers(tmp_path):
    root = tmp_path / "private"
    raw = b"synthetic operator message and private credential"

    def save(_):
        return RuntimeVault(root).put(raw, purpose="intent:operator")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        references = list(executor.map(save, range(16)))
    assert len(set(references)) == 1
    reopened = RuntimeVault(root)
    assert reopened.get(references[0], purpose="intent:operator") == raw
    assert all(raw not in path.read_bytes() for path in root.iterdir())
    assert all(path.stat().st_mode & 0o077 == 0 for path in root.iterdir())
    assert reopened.put(raw, purpose="intent:device:other") != references[0]


@pytest.mark.parametrize("damage", ["purpose", "ciphertext", "symlink", "mode"])
def test_payload_tampering_is_held_before_plaintext_is_returned(tmp_path, damage):
    vault = RuntimeVault(tmp_path / "private")
    reference = vault.put(b"synthetic", purpose="intent:operator")
    path = vault.root / reference
    purpose = "intent:operator"
    if damage == "purpose":
        purpose = "intent:device:other"
    elif damage == "ciphertext":
        path.write_bytes(path.read_bytes()[:-1] + b"!")
    elif damage == "symlink":
        target = tmp_path / "target"
        target.write_bytes(path.read_bytes())
        target.chmod(0o600)
        path.unlink()
        path.symlink_to(target)
    else:
        path.chmod(0o644)
    with pytest.raises((RuntimeIpcError, OSError)):
        vault.get(reference, purpose=purpose)


def test_references_paths_and_payload_sizes_are_bounded(tmp_path):
    vault = RuntimeVault(tmp_path / "private")
    for reference in ("../key", "key", "A" * 64, "f" * 65):
        with pytest.raises(RuntimeIpcError):
            vault.get(reference, purpose="intent:operator")
    with pytest.raises(RuntimeIpcError, match="bound"):
        vault.put(b"x" * (MAX_CONTENT_BYTES + 1), purpose="intent:operator")
    vault.root.chmod(0o755)
    with pytest.raises(RuntimeIpcError, match="private"):
        RuntimeVault(vault.root)


def test_empty_content_is_authenticated_and_old_ciphertext_cannot_replace_another_item(
    tmp_path,
):
    vault = RuntimeVault(tmp_path / "private")
    empty = vault.put(b"", purpose="end")
    other = vault.put(b"other", purpose="end")
    assert vault.get(empty, purpose="end") == b""
    os.replace(vault.root / empty, vault.root / other)
    with pytest.raises(RuntimeIpcError, match="integrity"):
        vault.get(other, purpose="end")
