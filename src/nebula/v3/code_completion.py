"""Bounded, local-only editor completion helpers."""

from __future__ import annotations

import re
from typing import Any

import jedi  # type: ignore[import-untyped]

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BUILTINS = {
    "and",
    "as",
    "assert",
    "async",
    "await",
    "break",
    "class",
    "continue",
    "def",
    "del",
    "elif",
    "else",
    "except",
    "False",
    "finally",
    "for",
    "from",
    "if",
    "import",
    "in",
    "is",
    "lambda",
    "None",
    "not",
    "or",
    "pass",
    "raise",
    "return",
    "True",
    "try",
    "while",
    "with",
    "yield",
    "print",
    "len",
    "range",
    "str",
    "int",
    "list",
    "dict",
    "set",
}


def complete(
    source: str, path: str, offset: int, limit: int = 30
) -> list[dict[str, Any]]:
    """Return bounded completion items without network or filesystem access."""
    if not 0 <= offset <= len(source) or len(source.encode()) > 1_048_576:
        return []
    if path.lower().endswith(".py"):
        try:
            line = source.count("\n", 0, offset) + 1
            column = offset - (source.rfind("\n", 0, offset) + 1)
            names = jedi.Script(code=source, path=path).complete(line, column)
            return [
                {"label": item.name, "type": item.type, "detail": item.description}
                for item in names[:limit]
            ]
        except (OSError, ValueError, SyntaxError, TypeError):
            # diagnostic-expected: optional Jedi completion falls back to lexical names.
            pass
    prefix = re.search(r"[A-Za-z_][A-Za-z0-9_]*$", source[:offset])
    needle = prefix.group(0) if prefix else ""
    names = sorted({match.group(0) for match in _TOKEN.finditer(source)} | _BUILTINS)
    return [
        {"label": name, "type": "keyword" if name in _BUILTINS else "variable"}
        for name in names
        if name.startswith(needle) and name != needle
    ][:limit]
