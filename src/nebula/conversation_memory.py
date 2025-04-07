import json
import os

import tiktoken  # Ensure you have installed tiktoken (pip install tiktoken)

from . import constants
from .log_config import setup_logging

logger = setup_logging(log_file=f"{constants.SYSTEM_LOGS_DIR}/memory.log")


class ConversationMemory:
    def __init__(self, max_tokens=20000, file_path=None, encoding_name="cl100k_base"):

        self.max_tokens = max_tokens
        self.file_path = file_path
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
        logger.info(f"Adding message: role={role}, content length={len(content)}")
        self.history.append({"role": role, "content": content})
        self.trim_memory()
        self.save()

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
            with open(self.file_path, "w") as f:
                json.dump(self.history, f)
            logger.info(f"Successfully saved conversation memory to {self.file_path}")
        except Exception as e:
            logger.error(f"Error saving conversation memory: {e}")

    def load(self):
        """
        Load the conversation history from disk if it exists.
        """
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r") as f:
                    self.history = json.load(f)
                logger.info(
                    f"Successfully loaded conversation memory from {self.file_path}"
                )
            except Exception as e:
                logger.error(f"Error loading conversation memory: {e}")
                self.history = []
        else:
            logger.info(
                f"No existing conversation memory found at {self.file_path}. Starting fresh."
            )
