"""Authenticated, bounded LSP intelligence for editor-owned source bytes.

The built-in Python provider deliberately analyzes only text supplied by the
editor. It never points Jedi or Ruff at the operator's host workspace. This
keeps the protocol useful on linked projects without allowing imports, project
configuration, plugins, or symlinks to expand Core's filesystem authority.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import jedi  # type: ignore[import-untyped]
from pydantic import Field, field_validator

from .diagnostics import record_caught_exception
from .domain import NebulaModel


MAX_DOCUMENT_BYTES = 1_048_576
MAX_MESSAGE_BYTES = 2_097_152
MAX_OPEN_DOCUMENTS = 20
MAX_BATCH_DOCUMENTS = 20
MAX_DIAGNOSTICS = 500
ANALYSIS_TIMEOUT_SECONDS = 4
_WORKSPACE_URI = "file:///workspace"
_VIRTUAL_ROOT = PurePosixPath("/__nebula_open_buffer__")
_PATH_SEGMENT = re.compile(r"[^/\\\x00]+")


class LanguageDocument(NebulaModel):
    path: str = Field(min_length=1, max_length=4096)
    language_id: str = Field(default="python", min_length=1, max_length=64)
    source: str = Field(max_length=MAX_DOCUMENT_BYTES)
    version: int = Field(default=0, ge=0, le=2_147_483_647, strict=True)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        return normalized_path(value)

    @field_validator("source")
    @classmethod
    def bounded_source(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("source cannot contain NUL bytes")
        if len(value.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            raise ValueError("source exceeds the 1 MiB language-service limit")
        return value


class LanguageDiagnosticsRequest(NebulaModel):
    engagement_id: str = Field(min_length=1, max_length=200)
    documents: list[LanguageDocument] = Field(
        min_length=1, max_length=MAX_BATCH_DOCUMENTS
    )


class LanguageDocumentDiagnostics(NebulaModel):
    path: str
    version: int
    diagnostics: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_DIAGNOSTICS
    )


class LanguageDiagnosticsResponse(NebulaModel):
    engine: str = "jedi+ruff"
    documents: list[LanguageDocumentDiagnostics]


@dataclass
class _OpenDocument:
    uri: str
    path: str
    language_id: str
    version: int
    source: str


def normalized_path(value: str) -> str:
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("language document path must be workspace-relative")
    parts = value.split("/")
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or any(_PATH_SEGMENT.fullmatch(part) is None for part in parts)
    ):
        raise ValueError("language document path is invalid")
    return PurePosixPath(*parts).as_posix()


def path_from_uri(uri: str) -> str:
    prefix = _WORKSPACE_URI + "/"
    if not uri.startswith(prefix):
        raise ValueError("language document URI is outside /workspace")
    # LSP clients must percent-encode reserved URI characters. Refuse encoded
    # separators rather than decoding them into a different path authority.
    relative = uri[len(prefix) :]
    if "%2f" in relative.casefold() or "%5c" in relative.casefold():
        raise ValueError("encoded path separators are forbidden")
    from urllib.parse import unquote

    return normalized_path(unquote(relative))


def uri_for_path(path: str) -> str:
    from urllib.parse import quote

    return f"{_WORKSPACE_URI}/{quote(normalized_path(path), safe='/')}"


def _virtual_path(path: str) -> str:
    return str(_VIRTUAL_ROOT.joinpath(*normalized_path(path).split("/")))


def _project() -> jedi.Project:
    return jedi.Project(
        str(_VIRTUAL_ROOT),
        load_unsafe_extensions=False,
        sys_path=[],
        added_sys_path=(),
        smart_sys_path=False,
    )


def _script(document: _OpenDocument) -> jedi.Script:
    return jedi.Script(
        code=document.source,
        path=_virtual_path(document.path),
        project=_project(),
    )


def _line_text(source: str, zero_line: int) -> str:
    # LSP treats an empty document and the text after a trailing newline as a
    # real line. splitlines() drops both, so split explicitly on LF.
    lines = source.split("\n")
    if zero_line < 0 or zero_line >= len(lines):
        raise ValueError("position line is outside the document")
    return lines[zero_line].removesuffix("\r")


def _codepoint_column(line: str, utf16_column: int) -> int:
    if utf16_column < 0:
        raise ValueError("position character cannot be negative")
    units = 0
    for index, character in enumerate(line):
        if units == utf16_column:
            return index
        units += 2 if ord(character) > 0xFFFF else 1
        if units > utf16_column:
            raise ValueError("position splits a UTF-16 surrogate pair")
    if units == utf16_column:
        return len(line)
    raise ValueError("position character is outside the line")


def _utf16_column(line: str, codepoint_column: int) -> int:
    bounded = min(max(codepoint_column, 0), len(line))
    return len(line[:bounded].encode("utf-16-le")) // 2


def _jedi_position(source: str, position: Any) -> tuple[int, int]:
    if not isinstance(position, dict):
        raise ValueError("position must be an object")
    line = position.get("line")
    character = position.get("character")
    if isinstance(line, bool) or not isinstance(line, int):
        raise ValueError("position line must be an integer")
    if isinstance(character, bool) or not isinstance(character, int):
        raise ValueError("position character must be an integer")
    text = _line_text(source, line)
    return line + 1, _codepoint_column(text, character)


def _document_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("language document version must be an integer")
    return value


def _lsp_position(source: str, line: int, column: int) -> dict[str, int]:
    text = _line_text(source, max(line - 1, 0))
    return {"line": max(line - 1, 0), "character": _utf16_column(text, column)}


def _range(
    source: str, line: int, column: int, until_line: int, until_column: int
) -> dict[str, Any]:
    return {
        "start": _lsp_position(source, line, column),
        "end": _lsp_position(source, until_line, until_column),
    }


def _name_range(document: _OpenDocument, line: int, column: int) -> dict[str, Any]:
    text = _line_text(document.source, line - 1)
    start = min(column, len(text))
    end = start
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
        start -= 1
    while end < len(text) and (text[end].isalnum() or text[end] == "_"):
        end += 1
    return _range(document.source, line, start, line, end)


def _completion_kind(kind: str) -> int:
    return {
        "module": 9,
        "class": 7,
        "instance": 6,
        "function": 3,
        "param": 6,
        "path": 17,
        "property": 10,
        "keyword": 14,
        "statement": 6,
    }.get(kind, 6)


def complete_document(
    document: _OpenDocument, position: dict[str, Any], limit: int = 50
) -> list[dict[str, Any]]:
    if document.language_id not in {"python", "py"} and not document.path.endswith(
        ".py"
    ):
        return []
    line, column = _jedi_position(document.source, position)
    return [
        {
            "label": item.name,
            "kind": _completion_kind(item.type),
            "detail": item.description[:500],
            "sortText": f"{index:03d}:{item.name}",
        }
        for index, item in enumerate(_script(document).complete(line, column)[:limit])
    ]


def hover_document(
    document: _OpenDocument, position: dict[str, Any]
) -> dict[str, Any] | None:
    if not document.path.endswith(".py"):
        return None
    line, column = _jedi_position(document.source, position)
    names = _script(document).infer(line, column)
    if not names:
        names = _script(document).goto(line, column, follow_imports=False)
    if not names:
        return None
    name = names[0]
    detail = name.description
    try:
        documentation = name.docstring(raw=True).strip()
    except (AttributeError, TypeError, ValueError):
        # diagnostic-expected: not every Jedi name exposes documentation.
        documentation = ""
    value = detail if not documentation else f"{detail}\n\n{documentation[:3_500]}"
    return {
        "contents": {"kind": "plaintext", "value": value[:4_000]},
        "range": _name_range(document, line, column),
    }


def signatures_document(
    document: _OpenDocument, position: dict[str, Any]
) -> dict[str, Any] | None:
    if not document.path.endswith(".py"):
        return None
    line, column = _jedi_position(document.source, position)
    signatures = _script(document).get_signatures(line, column)
    if not signatures:
        return None
    return {
        "signatures": [
            {
                "label": item.to_string(),
                "documentation": {
                    "kind": "plaintext",
                    "value": item.docstring(raw=True)[:2_000],
                },
                "parameters": [{"label": parameter.name} for parameter in item.params],
                "activeParameter": item.index or 0,
            }
            for item in signatures[:10]
        ],
        "activeSignature": 0,
        "activeParameter": signatures[0].index or 0,
    }


def _locations(
    document: _OpenDocument, names: list[Any], *, limit: int = 500
) -> list[dict[str, Any]]:
    virtual = _virtual_path(document.path)
    locations: list[dict[str, Any]] = []
    for name in names:
        if str(name.module_path or "") != virtual or not name.line:
            continue
        locations.append(
            {
                "uri": document.uri,
                "range": _name_range(document, name.line, name.column or 0),
            }
        )
        if len(locations) >= limit:
            break
    return locations


def definitions_document(
    document: _OpenDocument, position: dict[str, Any]
) -> list[dict[str, Any]]:
    if not document.path.endswith(".py"):
        return []
    line, column = _jedi_position(document.source, position)
    return _locations(
        document,
        _script(document).goto(
            line, column, follow_imports=True, follow_builtin_imports=False
        ),
    )


def references_document(
    document: _OpenDocument, position: dict[str, Any]
) -> list[dict[str, Any]]:
    if not document.path.endswith(".py"):
        return []
    line, column = _jedi_position(document.source, position)
    return _locations(document, _script(document).get_references(line, column))


def _jedi_diagnostics(document: _OpenDocument) -> list[dict[str, Any]]:
    if not document.path.endswith(".py"):
        return []
    return [
        {
            "range": _range(
                document.source,
                issue.line,
                issue.column,
                issue.until_line,
                issue.until_column,
            ),
            "severity": 1,
            "source": "Jedi",
            "code": "syntax-error",
            "message": issue.get_message()[:2_000],
        }
        for issue in _script(document).get_syntax_errors()[:MAX_DIAGNOSTICS]
    ]


def _ruff_diagnostics(document: _OpenDocument) -> list[dict[str, Any]]:
    if not document.path.endswith(".py"):
        return []
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--no-cache",
                "--output-format=json",
                "--stdin-filename",
                f"/workspace/{document.path}",
                "-",
            ],
            input=document.source.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
            check=False,
            env={"HOME": "/nonexistent", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        record_caught_exception(
            "language_server",
            "language_server.ruff.failed",
            "Ruff could not analyze editor-supplied source.",
            exc,
            stage="diagnostics",
        )
        return []
    if len(completed.stdout) > 1_048_576:
        return []
    try:
        records = json.loads(completed.stdout or b"[]")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        record_caught_exception(
            "language_server",
            "language_server.ruff.invalid_output",
            "Ruff returned invalid bounded diagnostics.",
            exc,
            stage="diagnostics",
        )
        return []
    if not isinstance(records, list):
        return []
    diagnostics: list[dict[str, Any]] = []
    for record in records[:MAX_DIAGNOSTICS]:
        if not isinstance(record, dict):
            continue
        start = record.get("location")
        end = record.get("end_location")
        code = record.get("code")
        message = record.get("message")
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        try:
            start_line = int(start["row"])
            start_column = max(0, int(start["column"]) - 1)
            end_line = int(end["row"])
            end_column = max(0, int(end["column"]) - 1)
            diagnostic_range = _range(
                document.source,
                start_line,
                start_column,
                end_line,
                end_column,
            )
        except (KeyError, TypeError, ValueError):
            continue
        diagnostic: dict[str, Any] = {
            "range": diagnostic_range,
            "severity": 1
            if code in {"F821", "E999"} or str(code).startswith("E9")
            else 2,
            "source": "Ruff",
            "code": str(code or "ruff"),
            "message": str(message or "Python diagnostic")[:2_000],
        }
        if isinstance(record.get("url"), str):
            diagnostic["codeDescription"] = {"href": record["url"][:2_000]}
        diagnostics.append(diagnostic)
    return diagnostics


def diagnostics_document(document: _OpenDocument) -> list[dict[str, Any]]:
    combined = _ruff_diagnostics(document) + _jedi_diagnostics(document)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in combined:
        identity = json.dumps(item, sort_keys=True)
        if identity not in seen:
            seen.add(identity)
            unique.append(item)
    return unique[:MAX_DIAGNOSTICS]


async def analyze_documents(
    request: LanguageDiagnosticsRequest,
) -> LanguageDiagnosticsResponse:
    documents: list[LanguageDocumentDiagnostics] = []
    for item in request.documents:
        document = _OpenDocument(
            uri=uri_for_path(item.path),
            path=item.path,
            language_id=item.language_id,
            version=item.version,
            source=item.source,
        )
        diagnostics = await asyncio.wait_for(
            asyncio.to_thread(diagnostics_document, document),
            timeout=ANALYSIS_TIMEOUT_SECONDS,
        )
        documents.append(
            LanguageDocumentDiagnostics(
                path=item.path,
                version=item.version,
                diagnostics=diagnostics,
            )
        )
    return LanguageDiagnosticsResponse(documents=documents)


class LanguageServerSession:
    """One LSP JSON-RPC peer with no durable state or filesystem authority."""

    def __init__(self, engagement_id: str) -> None:
        self.engagement_id = engagement_id
        self.documents: dict[str, _OpenDocument] = {}
        self.initialized = False
        self.shutdown_requested = False

    async def handle(self, payload: Any) -> list[dict[str, Any]]:
        notification = isinstance(payload, dict) and "id" not in payload
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            return (
                []
                if notification
                else [
                    self._error(
                        payload.get("id") if isinstance(payload, dict) else None,
                        -32600,
                        "Invalid JSON-RPC request",
                    )
                ]
            )
        method = payload.get("method")
        request_id = payload.get("id")
        params = payload.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return [
                self._error(request_id, -32600, "Invalid JSON-RPC method or params")
            ]
        try:
            if method == "initialize":
                if params.get("rootUri") not in {None, _WORKSPACE_URI}:
                    raise ValueError(
                        "language server rootUri must be file:///workspace"
                    )
                self.initialized = True
                return [
                    self._result(
                        request_id,
                        {
                            "capabilities": {
                                "positionEncoding": "utf-16",
                                "textDocumentSync": {"openClose": True, "change": 1},
                                "completionProvider": {
                                    "triggerCharacters": ["."],
                                    "resolveProvider": False,
                                },
                                "hoverProvider": True,
                                "signatureHelpProvider": {
                                    "triggerCharacters": ["(", ","]
                                },
                                "definitionProvider": True,
                                "referencesProvider": True,
                            },
                            "serverInfo": {
                                "name": "Nebula bounded Python intelligence",
                                "version": "1",
                            },
                        },
                    )
                ]
            if not self.initialized:
                return [
                    self._error(
                        request_id, -32002, "Language server is not initialized"
                    )
                ]
            if method == "initialized":
                return []
            if method == "shutdown":
                self.shutdown_requested = True
                return [self._result(request_id, None)]
            if method == "exit":
                self.shutdown_requested = True
                return []
            if method == "$/cancelRequest":
                return []
            if method == "textDocument/didOpen":
                document = params.get("textDocument")
                if not isinstance(document, dict):
                    raise ValueError("didOpen requires a textDocument")
                uri = str(document.get("uri", ""))
                if (
                    len(self.documents) >= MAX_OPEN_DOCUMENTS
                    and uri not in self.documents
                ):
                    raise ValueError("too many open language documents")
                source = str(document.get("text", ""))
                item = LanguageDocument(
                    path=path_from_uri(uri),
                    language_id=str(document.get("languageId", "python")),
                    version=_document_version(document.get("version", 0)),
                    source=source,
                )
                opened = _OpenDocument(
                    uri, item.path, item.language_id, item.version, item.source
                )
                self.documents[uri] = opened
                return [await self._publish(opened)]
            if method == "textDocument/didChange":
                identifier = params.get("textDocument")
                changes = params.get("contentChanges")
                if (
                    not isinstance(identifier, dict)
                    or not isinstance(changes, list)
                    or len(changes) != 1
                ):
                    raise ValueError("didChange requires one full-document change")
                uri = str(identifier.get("uri", ""))
                current = self._document(uri)
                change = changes[0]
                if (
                    not isinstance(change, dict)
                    or "range" in change
                    or not isinstance(change.get("text"), str)
                ):
                    raise ValueError(
                        "language server accepts full-document synchronization only"
                    )
                version = _document_version(
                    identifier.get("version", current.version + 1)
                )
                if version <= current.version:
                    raise ValueError("language document version must increase")
                item = LanguageDocument(
                    path=current.path,
                    language_id=current.language_id,
                    version=version,
                    source=change["text"],
                )
                changed = _OpenDocument(
                    uri, item.path, item.language_id, item.version, item.source
                )
                self.documents[uri] = changed
                return [await self._publish(changed)]
            if method == "textDocument/didClose":
                identifier = params.get("textDocument")
                uri = (
                    str(identifier.get("uri", ""))
                    if isinstance(identifier, dict)
                    else ""
                )
                document = self.documents.pop(uri, None)
                return [
                    self._notification(
                        "textDocument/publishDiagnostics",
                        {
                            "uri": uri,
                            "version": document.version if document else None,
                            "diagnostics": [],
                        },
                    )
                ]

            identifier = params.get("textDocument")
            uri = str(identifier.get("uri", "")) if isinstance(identifier, dict) else ""
            document = self._document(uri)
            position = params.get("position")
            if method == "textDocument/completion":
                value = await self._analyze(complete_document, document, position)
            elif method == "textDocument/hover":
                value = await self._analyze(hover_document, document, position)
            elif method == "textDocument/signatureHelp":
                value = await self._analyze(signatures_document, document, position)
            elif method == "textDocument/definition":
                value = await self._analyze(definitions_document, document, position)
            elif method == "textDocument/references":
                value = await self._analyze(references_document, document, position)
            else:
                return [
                    self._error(
                        request_id, -32601, f"Unsupported language method: {method}"
                    )
                ]
            return [self._result(request_id, value)]
        except (TypeError, ValueError) as exc:
            return (
                []
                if notification
                else [self._error(request_id, -32602, str(exc)[:1_000])]
            )
        except asyncio.TimeoutError:
            return (
                []
                if notification
                else [self._error(request_id, -32001, "Language analysis timed out")]
            )
        except Exception as exc:
            record_caught_exception(
                "language_server",
                "language_server.request.failed",
                "A bounded language-server request failed.",
                exc,
                stage="request",
            )
            return (
                []
                if notification
                else [self._error(request_id, -32603, "Language analysis failed")]
            )

    async def _publish(self, document: _OpenDocument) -> dict[str, Any]:
        diagnostics = await self._analyze(diagnostics_document, document)
        return self._notification(
            "textDocument/publishDiagnostics",
            {
                "uri": document.uri,
                "version": document.version,
                "diagnostics": diagnostics,
            },
        )

    async def _analyze(self, function: Any, *arguments: Any) -> Any:
        return await asyncio.wait_for(
            asyncio.to_thread(function, *arguments),
            timeout=ANALYSIS_TIMEOUT_SECONDS,
        )

    def _document(self, uri: str) -> _OpenDocument:
        path_from_uri(uri)
        try:
            return self.documents[uri]
        except KeyError as exc:
            raise ValueError("language document is not open") from exc

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    @staticmethod
    def _notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "method": method, "params": params}


__all__ = [
    "LanguageDiagnosticsRequest",
    "LanguageDiagnosticsResponse",
    "LanguageDocument",
    "LanguageServerSession",
    "MAX_MESSAGE_BYTES",
    "analyze_documents",
    "diagnostics_document",
    "normalized_path",
    "path_from_uri",
    "uri_for_path",
]
