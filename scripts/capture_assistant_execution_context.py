#!/usr/bin/env python3
"""Pure source oracle for text preparation; no DB, app, provider or workspace."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import unicodedata

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
import pydantic

from nebula.v3 import chat, context, known_model_limits
from nebula.v3.chat import ChatRequestMessage
from nebula.v3.chat_turn_outcomes import join_consecutive_assistant_messages
from nebula.v3.domain import ChatMessage, ProviderProfile
from nebula.v3.providers import ModelMessage, ModelRequest

ROOT = Path(os.environ.get("NEBULA_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
AUTHORITATIVE_PYTHON = (3, 12)
AUTHORITATIVE_UNICODE = "15.0.0"


def require_authoritative_runtime():
    if (
        sys.implementation.name != "cpython"
        or sys.version_info[:2] != AUTHORITATIVE_PYTHON
        or unicodedata.unidata_version != AUTHORITATIVE_UNICODE
    ):
        raise RuntimeError(
            "The execution-context oracle requires CPython 3.12 / Unicode 15.0.0; "
            f"current runtime is {sys.implementation.name} "
            f"{sys.version_info.major}.{sys.version_info.minor} / "
            f"Unicode {unicodedata.unidata_version}. "
            "Run this capture and its contract test with the Python 3.12 CI interpreter; "
            "do not omit the Unicode table comparison."
        )


BASE = {
    "id": "fixture",
    "revision": 1,
    "created_at": "2020-01-01T00:00:00Z",
    "updated_at": "2020-01-01T00:00:00Z",
}


def observation(fn):
    try:
        return {"accepted": True, "value": jsonable_encoder(fn())}
    except ValidationError as exc:
        return {
            "accepted": False,
            "error": {
                "kind": "ValidationError",
                "errors": jsonable_encoder(exc.errors(include_url=False)),
            },
        }
    except (
        TypeError,
        ValueError,
        OverflowError,
        chat.ChatHistoryConflict,
        context.ContextCapacityError,
    ) as exc:
        return {
            "accepted": False,
            "error": {"kind": type(exc).__name__, "detail": str(exc)},
        }


def profile(metadata=None, *, local=True, provider_type="custom"):
    return ProviderProfile.model_validate(
        {
            **BASE,
            "name": "Fixture",
            "provider_type": provider_type,
            "endpoint": "http://127.0.0.1:9",
            "is_local": local,
            "metadata": metadata or {},
        }
    )


def message(identifier, sequence, role, content, **kwargs):
    return ChatMessage.model_validate(
        {
            **BASE,
            "id": identifier,
            "engagement_id": "project",
            "session_id": "session",
            "sequence": sequence,
            "role": role,
            "content": content,
            **kwargs,
        }
    )


def request(role, content, **kwargs):
    return ChatRequestMessage.model_validate(
        {"role": role, "content": content, **kwargs}
    )


def attachment(text="selected café 🌌", **kwargs):
    return {
        "source_kind": "file",
        "source_id": "fixture.txt",
        "source_label": "Fixture",
        "text": text,
        "sha256": sha256(text.encode()).hexdigest(),
        "truncated": False,
        **kwargs,
    }


def static_data():
    require_authoritative_runtime()
    # Bind the Unicode digit predicate to the same Python source runtime, including
    # digit-but-not-decimal code points whose int() raises instead of defaulting.
    digits = [n for n in range(0x110000) if chr(n).isdigit()]
    ranges = []
    for n in digits:
        if ranges and ranges[-1][1] + 1 == n:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    zeroes = [n for n in digits if unicodedata.decimal(chr(n), None) == 0]
    return {
        "known_model_revision": known_model_limits.KNOWN_MODEL_LIMITS_REVISION,
        "known_models": known_model_limits.KNOWN_MODEL_LIMITS,
        "unicode_version": unicodedata.unidata_version,
        "isdigit_ranges": ranges,
        "decimal_zeroes": zeroes,
    }


def collect_execution_context():
    require_authoritative_runtime()
    assert Path(chat.__file__).resolve().is_relative_to(ROOT.resolve())
    limits = []

    def limit(
        name,
        metadata=None,
        *,
        model="fixture",
        output=None,
        required=(),
        local=True,
        provider_type="custom",
    ):
        p = profile(metadata, local=local, provider_type=provider_type)
        limits.append(
            {
                "name": name,
                "profile": p.model_dump(mode="json"),
                "model": model,
                "requested_output_tokens": output,
                "required_parameters": list(required),
                "expected": observation(
                    lambda: context.resolve_context_limits(
                        p,
                        model=model,
                        requested_output_tokens=output,
                        required_parameters=set(required),
                    ).model_dump(mode="json")
                ),
            }
        )

    limit("fallback")
    limit("fixture", {"options": {"context_window": 32768, "max_output_tokens": 4096}})
    limit("options-not-object", {"options": [131072]})
    for name, value in [
        ("digits", "32768"),
        ("arabic-digits", "٣٢٧٦٨"),
        ("fullwidth", "３２７６８"),
        ("unicode15-kawi", "".join(chr(0x11F50 + n) for n in range(10))),
        (
            "unicode15-nag-mundari",
            "".join(chr(0x1E4F0 + n) for n in reversed(range(10))),
        ),
        ("superscript", "²"),
        ("padded", " 32768 "),
        ("underscores", "32_768"),
        ("bool", True),
        ("float", 32768.0),
        ("negative", -10),
        ("zero", "0"),
        ("one", 1),
        ("two", 2),
        ("above-f64-exact", 9007199254740993),
        ("arbitrary-int", 10**100 + 111),
        ("float-overflow", 10**309),
    ]:
        limit(
            "window-" + name,
            {"model_descriptors": [{"id": "fixture", "context_window": value}]},
        )
    limit(
        "requested-output-cap",
        {"options": {"context_window": 4096, "max_output_tokens": 3000}},
        output=999999,
    )
    limit("requested-output-small", {"options": {"context_window": 4096}}, output=1)
    limit("known-hosted", model="anthropic/claude-sonnet-4-5", local=False)
    limit("known-ignored-local", model="anthropic/claude-sonnet-4-5")
    limit(
        "known-output-only",
        {"model_descriptors": [{"id": "gpt-4o", "context_window": 10000}]},
        model="gpt-4o",
        local=False,
    )
    limit(
        "descriptor-cap",
        {
            "options": {"context_window": 8000, "max_output_tokens": 700},
            "model_descriptors": [
                {
                    "id": "fixture",
                    "context_window": 10000,
                    "max_output_tokens": 900,
                    "max_input_tokens": 5000,
                }
            ],
        },
    )
    limit(
        "descriptor-first",
        {
            "model_descriptors": [
                {"id": "fixture", "context_window": 10000},
                {"id": "fixture", "context_window": 20000},
            ]
        },
    )
    for value in (None, 7, True, {}, "fixture"):
        limit("descriptors-" + type(value).__name__, {"model_descriptors": value})
    limit(
        "revision-truthiness",
        {"route_catalog_revision": 7, "model_catalog_revision": "ignored"},
    )
    limit(
        "revision-fallback",
        {"route_catalog_revision": "", "model_catalog_revision": "catalog-2"},
    )
    limit(
        "router-unverified-floor",
        {"options": {"context_window": 100000, "max_output_tokens": 10000}},
        provider_type="openrouter",
    )
    limit(
        "router-primary",
        {
            "model_descriptors": [
                {
                    "id": "fixture",
                    "context_window": 20000,
                    "primary_route_context_window": 12000,
                    "max_output_tokens": 4000,
                }
            ]
        },
        provider_type="openrouter",
    )
    descriptor = {
        "id": "fixture",
        "route_limits_verified": True,
        "route_limits_source_model": "fixture",
        "route_limits": [
            {
                "status": 0,
                "context_window": 64000,
                "max_input_tokens": 60000,
                "max_output_tokens": 8000,
                "supported_parameters": ["tools"],
            },
            {
                "status": False,
                "context_window": 32000,
                "max_input_tokens": 30000,
                "max_output_tokens": 4000,
                "supported_parameters": [],
            },
            {
                "status": 1,
                "context_window": 100,
                "max_input_tokens": 90,
                "max_output_tokens": 10,
            },
        ],
    }
    limit(
        "router-verified",
        {"model_descriptors": [descriptor]},
        provider_type="openrouter",
    )
    limit(
        "router-required-tools",
        {"model_descriptors": [descriptor]},
        required=("tools",),
        provider_type="openrouter",
    )
    limit(
        "router-no-eligible",
        {"model_descriptors": [descriptor]},
        required=("z", "a"),
        provider_type="openrouter",
    )
    for name, patch in [
        ("stale-alias", {"alias_target": "new-target"}),
        ("truthy-not-true", {"route_limits_verified": 1}),
        ("invalid-source-slug", {"route_limits_source_model": " "}),
        ("long-alias-ignored", {"alias_target": "x" * 501}),
        ("incomplete", {"route_limits": [{"status": 0, "context_window": 10000}]}),
        ("none", {"route_limits": None}),
        ("dict", {"route_limits": {"fixture": 1}}),
        ("string", {"route_limits": "fixture"}),
    ]:
        limit(
            "routes-" + name,
            {"model_descriptors": [{**descriptor, **patch}]},
            provider_type="openrouter",
        )
    limit("verified-custom", {"model_descriptors": [descriptor]})

    known = [
        {"model": value, "expected": context.known_model_limits(value)}
        for value in (
            None,
            "",
            " fixture ",
            "gpt-4o-2024-08-06",
            "Gpt_4o",
            "openai/gpt-4.1-mini",
            "us.anthropic.claude-opus-5-v1:0",
            "claude-haiku-4-5@20251001",
            "models/gemini-2.5-pro",
            "global.amazon.nova-pro-v1:0",
            "anthropic/claude-sonnet-4-5-novel-suffix",
            "\x1cgpt-4o\x1f",
        )
    ]
    estimates = []
    for name, text, count in [
        ("empty", "", 0),
        ("ascii", "1234", 1),
        ("unicode", "café🌌汉字", 2),
        ("controls", "\r\n\x1c\u00a0", 1),
    ]:
        estimates.append(
            {
                "name": name,
                "operation": "tokens",
                "text": text,
                "message_count": count,
                "expected": context.estimate_tokens(text, message_count=count),
            }
        )
    for name, messages, instructions in [
        ("no-messages", [], ""),
        ("empty-message", [{"role": "user", "content": ""}], ""),
        (
            "unicode-messages",
            [
                {"role": "user", "content": "é🌌"},
                {"role": "assistant", "content": "汉字"},
            ],
            "instruct",
        ),
    ]:
        modeled = [ModelMessage(**m) for m in messages]
        estimates.append(
            {
                "name": name,
                "operation": "messages",
                "messages": [m.model_dump(mode="json") for m in modeled],
                "instructions": instructions,
                "expected": context.estimate_messages(modeled, instructions),
            }
        )
    for name, fields in [
        ("request", {}),
        ("continuation", {"metadata": {"continuation": 'a "quoted" 🌌\nline'}}),
    ]:
        r = ModelRequest(
            model="fixture",
            instructions="base",
            messages=[ModelMessage(role="user", content="Hello 🌌")],
            **fields,
        )
        estimates.append(
            {
                "name": name,
                "operation": "request",
                "request": r.model_dump(mode="json"),
                "expected": context.estimate_model_request(r),
            }
        )

    history = []

    def history_case(name, stored, incoming):
        fake = SimpleNamespace(
            store=SimpleNamespace(list_session_entities=lambda *a: stored)
        )
        visible = chat.ChatService._session_messages(
            fake, SimpleNamespace(id="session")
        )

        def merge():
            h, n = chat.ChatService._merge_history(visible, incoming)
            models = join_consecutive_assistant_messages(
                [ModelMessage(role=m.role.value, content=m.content) for m in h]
            )
            return {
                "history": [m.model_dump(mode="json") for m in h],
                "new_messages": [m.model_dump(mode="json") for m in n],
                "model_messages": [m.model_dump(mode="json") for m in models],
            }

        history.append(
            {
                "name": name,
                "stored": [m.model_dump(mode="json") for m in stored],
                "incoming": [m.model_dump(mode="json") for m in incoming],
                "visible_ids": [m.id for m in visible],
                "expected": observation(merge),
            }
        )

    user = message("u", 1, "user", "Earlier question")
    answer = message("a", 2, "assistant", "Earlier answer")
    newer = request("user", "Next question")
    history_case("empty-new", [], [newer])
    history_case("single-new", [answer, user], [newer])
    replay = [request("user", user.content), request("assistant", answer.content)]
    history_case("full-replay", [user, answer], [*replay, newer])
    history_case("no-new", [user, answer], replay)
    history_case(
        "divergent",
        [user, answer],
        [request("user", "Other"), request("assistant", answer.content), newer],
    )
    history_case(
        "two-new", [user, answer], [*replay, newer, request("user", "Another")]
    )
    history_case(
        "assistant-new", [user, answer], [*replay, request("assistant", "Another")]
    )
    note = message(
        "note", 2, "assistant", "Response stopped.", metadata={"kind": "turn_outcome"}
    )
    history_case(
        "outcome-omitted", [user, note], [request("user", user.content), newer]
    )
    history_case(
        "outcome-replayed",
        [user, note],
        [request("user", user.content), request("assistant", note.content), newer],
    )
    for label, marker in [
        ("truthy", "old"),
        ("false", 0),
        ("empty-list", []),
        ("object", {"x": 1}),
    ]:
        replaced = message(
            "old", 1, "user", "Old text", metadata={"retracted_at": marker}
        )
        history_case("replaced-" + label, [user, replaced, answer], [newer])
    tie = message(
        "b", 2, "assistant", "Tied later", created_at="2020-01-01T01:00:00+01:00"
    )
    history_case("ties-offsets-and-join", [tie, answer, user], [newer])
    later = message(
        "later",
        2,
        "assistant",
        "Later time",
        created_at="2020-01-01T00:00:00.000001Z",
        updated_at="2020-01-01T00:00:01Z",
    )
    history_case("ties-different-time", [later, answer, user], [newer])
    bigseq = message("big", 10**100, "assistant", "Big sequence")
    history_case("huge-sequence", [bigseq, user, answer], [newer])
    for name, attachments in [
        ("context", [attachment()]),
        ("context-spaces", [attachment(" line\n", source_label="  File  ")]),
        ("context-invalid-hash", [attachment(sha256="0" * 64)]),
        ("context-partially-invalid", [attachment(), {"text": "invalid"}]),
        ("context-nonlist", "opaque"),
        ("context-empty", []),
    ]:
        selected = message(
            "selected",
            1,
            "user",
            "Question",
            metadata={"context_attachments": attachments},
        )
        history_case(name, [selected], [request("user", selected.content), newer])
    peer = message(
        "peer",
        3,
        "system",
        "Completed analysis",
        metadata={
            "kind": "agent_message",
            "sender_title": "Analyst",
            "sender_session_id": "sender",
        },
    )
    history_case("peer-system", [user, answer, peer], [newer])
    history_case(
        "peer-fallback-label",
        [
            message(
                "peer",
                3,
                "system",
                "Report",
                metadata={
                    "kind": "agent_message",
                    "sender_title": [],
                    "sender_session_id": "",
                },
            )
        ],
        [newer],
    )
    history_case(
        "text-blocks",
        [
            message(
                "block",
                1,
                "user",
                "Visible",
                content_blocks=[{"type": "text", "text": "Ignored supplemental text"}],
            )
        ],
        [newer],
    )
    history_case(
        "oversized-saved-content",
        [message("large", 1, "assistant", "x" * 100001)],
        [newer],
    )
    history_case(
        "empty-saved-assistant", [message("empty", 1, "assistant", "")], [newer]
    )
    joined = []
    for name, messages in [
        (
            "alternating",
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        ),
        (
            "consecutive",
            [
                {"role": "assistant", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "assistant", "content": "c"},
            ],
        ),
        (
            "empty",
            [
                {"role": "assistant", "content": ""},
                {"role": "assistant", "content": "b"},
                {"role": "assistant", "content": ""},
            ],
        ),
    ]:
        result = join_consecutive_assistant_messages(
            [ModelMessage(**m) for m in messages]
        )
        joined.append(
            {
                "name": name,
                "messages": messages,
                "expected": [m.model_dump(mode="json") for m in result],
            }
        )
    result = {
        "format": "nebula.assistant.execution-context.v1",
        "source_runtime": {
            "implementation": "cpython",
            "python_minor": "3.12",
            "unicode_version": AUTHORITATIVE_UNICODE,
        },
        "pydantic_version": pydantic.__version__,
        "static_data": static_data(),
        "limit_vectors": limits,
        "known_model_vectors": known,
        "estimate_vectors": estimates,
        "history_vectors": history,
        "join_vectors": joined,
        "unsupported": [
            "Image resolution/estimation, raw tool messages/tool results/schema estimation, context compaction and effects are not implemented by this text-only pure module."
        ],
        "source_sha256": {
            name: sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "src/nebula/v3/context.py",
                "src/nebula/v3/chat.py",
                "src/nebula/v3/domain.py",
                "src/nebula/v3/model_catalog.py",
                "src/nebula/v3/known_model_limits.py",
                "src/nebula/v3/chat_turn_outcomes.py",
            )
        },
    }
    return jsonable_encoder(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--static-output", type=Path)
    args = parser.parse_args()
    result = collect_execution_context()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    if args.static_output:
        args.static_output.write_text(
            json.dumps(
                result["static_data"], ensure_ascii=False, sort_keys=True, indent=2
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
