"""Shared secret redaction and terminal-safe display encoding."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_KNOWN_TOKEN = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,})\b"
)
_LABELED_SECRET = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|"
    r"passwd|secret)\b\s*[:=]\s*[\"']?)[^\s\"',;]{8,}"
)
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}


def redact_text(value: str) -> str:
    redacted = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", value)
    redacted = _BEARER_TOKEN.sub("Bearer [REDACTED]", redacted)
    redacted = _JWT.sub("[REDACTED JWT]", redacted)
    redacted = _KNOWN_TOKEN.sub("[REDACTED TOKEN]", redacted)
    return _LABELED_SECRET.sub(r"\1[REDACTED]", redacted)


def sanitize_display_text(value: str) -> str:
    """Make control sequences visible without changing stored raw bytes."""

    safe: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character in {"\n", "\t"}:
            safe.append(character)
        elif character == "\r":
            safe.append("\n")
        elif character in _BIDI_CONTROLS:
            safe.append(f"<U+{codepoint:04X}>")
        elif codepoint < 32 or codepoint == 127:
            safe.append(f"<0x{codepoint:02X}>")
        else:
            safe.append(character)
    return "".join(safe)


@dataclass
class StatefulRedactor:
    """Redact arbitrary chunks without leaking tokens split at boundaries."""

    carry_characters: int = 4096
    _buffer: str = ""

    def feed(self, value: str) -> str:
        self._buffer += value
        if len(self._buffer) <= self.carry_characters:
            return ""
        cut = len(self._buffer) - self.carry_characters
        prefix = self._buffer[:cut]
        match = _PRIVATE_KEY_BEGIN.search(prefix)
        if match and "-----END " not in self._buffer[match.start() :]:
            cut = match.start()
            prefix = self._buffer[:cut]
        self._buffer = self._buffer[cut:]
        return sanitize_display_text(redact_text(prefix))

    def finish(self) -> str:
        value = sanitize_display_text(redact_text(self._buffer))
        self._buffer = ""
        return value


def redacted_display(value: str) -> str:
    return sanitize_display_text(redact_text(value))


__all__ = [
    "StatefulRedactor",
    "redact_text",
    "redacted_display",
    "sanitize_display_text",
]
