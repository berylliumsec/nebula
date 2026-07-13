"""Exact fenced-code extraction for persisted assistant messages."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_OPENING_FENCE = re.compile(
    r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)"
    r"(?P<ending>\r?\n|$)"
)

LANGUAGE_ALIASES = {
    "bash": "bash",
    "shell": "bash",
    "sh": "sh",
    "python": "python",
    "python3": "python",
    "py": "python",
}


@dataclass(frozen=True)
class FencedCodeBlock:
    ordinal: int
    declared_language: str
    canonical_language: str | None
    source: str
    source_start: int
    source_end: int
    sha256: str


def parse_fenced_code_blocks(markdown: str) -> list[FencedCodeBlock]:
    """Return only closed fences, retaining exact source string offsets."""

    blocks: list[FencedCodeBlock] = []
    cursor = 0
    length = len(markdown)
    while cursor < length:
        line_end = markdown.find("\n", cursor)
        line_stop = length if line_end < 0 else line_end + 1
        line = markdown[cursor:line_stop]
        opening = _OPENING_FENCE.match(line)
        if opening is None:
            cursor = line_stop
            continue
        fence = opening.group("fence")
        marker = fence[0]
        info = opening.group("info").strip()
        declared = info.split(None, 1)[0].casefold() if info else ""
        source_start = cursor + opening.end()
        search = source_start
        close_start: int | None = None
        close_stop: int | None = None
        closing = re.compile(
            rf"^ {{0,3}}{re.escape(marker)}{{{len(fence)},}}[ \t]*(?:\r?\n|$)"
        )
        while search <= length:
            candidate_end = markdown.find("\n", search)
            candidate_stop = length if candidate_end < 0 else candidate_end + 1
            candidate = markdown[search:candidate_stop]
            if closing.fullmatch(candidate):
                close_start = search
                close_stop = candidate_stop
                break
            if candidate_end < 0:
                break
            search = candidate_stop
        if close_start is None or close_stop is None:
            # The unmatched region is deliberately inert. Earlier closed blocks
            # remain usable; nothing after this opener can be proven outside it.
            break
        source = markdown[source_start:close_start]
        blocks.append(
            FencedCodeBlock(
                ordinal=len(blocks),
                declared_language=declared,
                canonical_language=LANGUAGE_ALIASES.get(declared),
                source=source,
                source_start=source_start,
                source_end=close_start,
                sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            )
        )
        cursor = close_stop
    return blocks


def utf8_slice(value: str, start: int | None, end: int | None) -> str:
    encoded = value.encode("utf-8")
    if start is None and end is None:
        return value
    if start is None or end is None or end <= start or end > len(encoded):
        raise ValueError("selection byte offsets are outside the code block")
    try:
        return encoded[start:end].decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("selection offsets split a UTF-8 character") from exc


__all__ = [
    "FencedCodeBlock",
    "LANGUAGE_ALIASES",
    "parse_fenced_code_blocks",
    "utf8_slice",
]
