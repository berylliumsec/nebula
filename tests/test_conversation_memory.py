import json
from pathlib import Path

import pytest

from nebula import conversation_memory


class FakeEncoder:
    def encode(self, text):
        return text.split()


@pytest.fixture(autouse=True)
def fake_encoding(monkeypatch):
    monkeypatch.setattr(
        conversation_memory.tiktoken, "get_encoding", lambda _: FakeEncoder()
    )


def test_conversation_memory_starts_empty_when_file_is_missing(tmp_path):
    memory_file = tmp_path / "memory.json"

    memory = conversation_memory.ConversationMemory(
        max_tokens=10, file_path=str(memory_file)
    )

    assert memory.history == []


def test_conversation_memory_loads_existing_history(tmp_path):
    memory_file = tmp_path / "memory.json"
    payload = [{"role": "user", "content": "hello there"}]
    memory_file.write_text(json.dumps(payload))

    memory = conversation_memory.ConversationMemory(
        max_tokens=10, file_path=str(memory_file)
    )

    assert memory.history == payload


def test_conversation_memory_resets_history_on_invalid_json(tmp_path, monkeypatch):
    messages = []
    memory_file = tmp_path / "memory.json"
    memory_file.write_text("{invalid json")

    monkeypatch.setattr(conversation_memory.logger, "error", messages.append)

    memory = conversation_memory.ConversationMemory(
        max_tokens=10, file_path=str(memory_file)
    )

    assert memory.history == []
    assert messages and "Error loading conversation memory" in messages[0]


def test_estimate_tokens_uses_encoder(tmp_path):
    memory = conversation_memory.ConversationMemory(
        max_tokens=10, file_path=str(tmp_path / "memory.json")
    )

    assert memory.estimate_tokens("one two three") == 3


def test_add_message_trims_history_and_persists_to_disk(tmp_path):
    memory_file = tmp_path / "memory.json"
    memory = conversation_memory.ConversationMemory(
        max_tokens=3, file_path=str(memory_file)
    )

    memory.add_message("user", "one two")
    memory.add_message("assistant", "three four")

    assert memory.history == [{"role": "assistant", "content": "three four"}]
    assert json.loads(memory_file.read_text()) == memory.history


def test_save_logs_error_when_write_fails(tmp_path, monkeypatch):
    messages = []
    memory = conversation_memory.ConversationMemory(
        max_tokens=10, file_path=str(tmp_path / "memory.json")
    )

    monkeypatch.setattr(conversation_memory.logger, "error", messages.append)
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    memory.save()

    assert messages and "Error saving conversation memory" in messages[0]
