import json
import os
import tempfile
import threading
from contextlib import contextmanager

import tiktoken  # Ensure you have installed tiktoken (pip install tiktoken)
from filelock import FileLock

from . import constants
from .log_config import setup_logging

logger = setup_logging(log_file=f"{constants.SYSTEM_LOGS_DIR}/memory.log")

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for_path(file_path):
    key = os.path.abspath(file_path) if file_path else "<memory-only>"
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class ConversationMemory:
    def __init__(self, max_tokens=20000, file_path=None, encoding_name="cl100k_base"):

        self.max_tokens = max_tokens
        self.file_path = file_path
        self._thread_lock = _lock_for_path(file_path)
        self.history = (
            []
        )  # List of messages: each message is a dict with keys "role" and "content"
        # Initialize the tokenizer encoder from tiktoken for accurate token counting
        self.encoder = tiktoken.get_encoding(encoding_name)
        logger.info(
            f"Initializing ConversationMemory with max_tokens={max_tokens}, file_path={self.file_path}"
        )
        self.load()

    def estimate_tokens(self, text):
        """
        Use tiktoken's encoder to count the number of tokens in the provided text.
        """
        token_count = len(self.encoder.encode(text))
        logger.debug(f"Estimated {token_count} tokens for text: {text[:50]}...")
        return token_count

    def add_message(self, role, content):
        """
        Append a new message to the conversation history, trim if necessary, and persist the update.
        """
        self.add_messages([{"role": role, "content": content}])

    def add_messages(self, messages):
        """Atomically append one or more messages and persist the new history."""
        try:
            normalized = []
            for message in messages:
                role = message["role"]
                content = message["content"]
                if not isinstance(role, str) or not isinstance(content, str):
                    raise TypeError("Conversation roles and content must be strings")
                normalized.append({"role": role, "content": content})

            with self._file_lock():
                # Reload while holding the shared path lock so independent
                # workers append to the latest history instead of overwriting it.
                disk_history = self._read_unlocked()
                if disk_history is not None:
                    self.history = disk_history
                for message in normalized:
                    logger.info(
                        "Adding message: role=%s, content length=%d",
                        message["role"],
                        len(message["content"]),
                    )
                    self.history.append(message)
                self.trim_memory()
                self._save_unlocked()
        except Exception as e:
            logger.error(f"Error saving conversation memory: {e}")

    def trim_memory(self):
        """
        Automatically trims the conversation memory by removing the oldest messages until the total
        token count is below the max_tokens threshold.
        """
        total_tokens = sum(self.estimate_tokens(msg["content"]) for msg in self.history)
        logger.debug(f"Total tokens before trimming: {total_tokens}")
        # Remove the oldest messages until the total token count is within the allowed limit.
        while total_tokens > self.max_tokens and self.history:
            removed = self.history.pop(0)
            removed_tokens = self.estimate_tokens(removed["content"])
            logger.info(
                f"Trimming memory: removed message with role={removed['role']} containing {removed_tokens} tokens"
            )
            total_tokens = sum(
                self.estimate_tokens(msg["content"]) for msg in self.history
            )
            logger.debug(f"Total tokens after trimming: {total_tokens}")

    def save(self):
        """
        Persist the conversation history to disk.
        """
        try:
            with self._file_lock():
                self._save_unlocked()
            logger.info(f"Successfully saved conversation memory to {self.file_path}")
        except Exception as e:
            logger.error(f"Error saving conversation memory: {e}")

    def load(self):
        """
        Load the conversation history from disk if it exists.
        """
        if not self.file_path:
            return
        try:
            with self._file_lock():
                loaded = self._read_unlocked()
                if loaded is not None:
                    self.history = loaded
                    logger.info(
                        f"Successfully loaded conversation memory from {self.file_path}"
                    )
                else:
                    logger.info(
                        f"No existing conversation memory found at {self.file_path}. Starting fresh."
                    )
        except Exception as e:
            logger.error(f"Error loading conversation memory: {e}")
            self.history = []

    @contextmanager
    def _file_lock(self):
        """Serialize writers both in-process and across processes."""
        with self._thread_lock:
            if not self.file_path:
                yield
                return

            parent = os.path.dirname(os.path.abspath(self.file_path))
            os.makedirs(parent, exist_ok=True)
            with FileLock(self.file_path + ".lock"):
                yield

    def _read_unlocked(self):
        if not self.file_path or not os.path.exists(self.file_path):
            return None
        with open(self.file_path, "r", encoding="utf-8") as file:
            history = json.load(file)
        if not isinstance(history, list) or any(
            not isinstance(message, dict)
            or not isinstance(message.get("role"), str)
            or not isinstance(message.get("content"), str)
            for message in history
        ):
            raise ValueError("Conversation memory must be a list of role/content messages")
        return history

    def _save_unlocked(self):
        if not self.file_path:
            return

        parent = os.path.dirname(os.path.abspath(self.file_path))
        os.makedirs(parent, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(self.file_path)}.",
            suffix=".tmp",
            dir=parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(self.history, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.file_path)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
