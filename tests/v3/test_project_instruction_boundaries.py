"""Instruction caps preserve complete text and hash the exact delivered body."""

from pathlib import Path
import hashlib
import json
import pytest
from nebula.v3.project_instructions import (
    MAX_PROJECT_INSTRUCTIONS_BYTES as CAP,
    load_project_instructions,
    project_instructions_text,
)


def load_counted(tmp_path, monkeypatch, raw):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(raw)
    reads = []
    original_open = Path.open

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, amount):
            reads.append(amount)
            return self.handle.read(amount)

    def open_path(candidate, *args, **kwargs):
        handle = original_open(candidate, *args, **kwargs)
        return Reader(handle) if candidate == path and args == ("rb",) else handle

    monkeypatch.setattr(Path, "open", open_path)
    loaded = load_project_instructions(tmp_path)
    assert reads == [CAP + 1]
    assert loaded.size_bytes == len(raw)
    body = json.loads(project_instructions_text(loaded).split("\n", 3)[-1])["content"]
    assert body == loaded.content
    assert loaded.sha256 == hashlib.sha256(body.encode()).hexdigest()
    return loaded


@pytest.mark.parametrize(
    "character,cut", [("é", 1), ("€", 1), ("€", 2), ("😸", 1), ("😸", 2), ("😸", 3)]
)
def test_each_incomplete_utf8_tail_is_withheld_and_digest_matches(
    tmp_path, monkeypatch, character, cut
):
    prefix = "\r\n  " + "a" * (CAP - cut - 4)
    loaded = load_counted(tmp_path, monkeypatch, (prefix + character + "tail").encode())
    assert loaded.truncated is True
    assert loaded.content == prefix


@pytest.mark.parametrize("character", ["é", "€", "😸", "\ufffd"])
def test_complete_multibyte_character_at_cap_is_never_dropped(
    tmp_path, monkeypatch, character
):
    prefix = "\r\n  " + "a" * (CAP - len(character.encode()) - 4)
    loaded = load_counted(tmp_path, monkeypatch, (prefix + character + "tail").encode())
    assert loaded.truncated is True
    assert loaded.content == prefix + character


def test_exact_cap_is_nontruncated_and_preserves_text(tmp_path, monkeypatch):
    body = "é" * (CAP // 2)
    loaded = load_counted(tmp_path, monkeypatch, body.encode())
    assert loaded.truncated is False
    assert loaded.content == body


@pytest.mark.parametrize("truncated", [False, True])
def test_malformed_interior_replacement_policy_remains(
    tmp_path, monkeypatch, truncated
):
    raw = b"  malformed \xff interior\r\n"
    expected = raw.decode("utf-8", errors="replace")
    if truncated:
        tail = b"a" * (CAP - len(raw) - 1)
        raw += tail + b"\xc3\xa9tail"
        expected += tail.decode()
    loaded = load_counted(tmp_path, monkeypatch, raw)
    assert loaded.truncated is truncated
    assert loaded.content == expected
