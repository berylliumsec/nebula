#!/usr/bin/env python3
"""Capture full ChatTurn validation and trusted constructor factory provenance.

This pure model oracle opens no database, app, provider, helper or workspace.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import UUID

from fastapi.encoders import jsonable_encoder
import pydantic
from pydantic import ValidationError

import nebula.v3.domain as domain
from nebula.v3.domain import ChatTokenUsage, ChatTurn

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.capture_assistant_model_validation import BASE, NOW, model_metadata

ROOT = Path(__file__).resolve().parents[1]
BASE_FIELDS = ["id", "created_at", "updated_at", "revision"]


def base_input():
    return {
        "id": "fixture-turn",
        "created_at": BASE.isoformat(),
        "updated_at": BASE.isoformat(),
        "revision": 1,
        "engagement_id": "project",
        "session_id": "session",
        "provider_profile_id": "provider",
        "model": "fixture",
    }


def capture(
    name,
    supplied,
    *,
    origin="retained_json",
    constructor=False,
    clocks=None,
    typed_usage=False,
    boundary=None,
):
    supplied = deepcopy(supplied)
    if typed_usage:
        supplied["usage"] = ChatTokenUsage.model_validate(supplied["usage"])
    encoded = jsonable_encoder(supplied)
    trace = []
    clock_values = clocks or [NOW, NOW + timedelta(microseconds=123456)]
    generated_id = str(UUID(int=913))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            index = sum(item["kind"] == "model" for item in trace)
            assert index < len(clock_values), "unexpected default clock"
            value = clock_values[index]
            trace.append({"kind": "model", "value": value.isoformat()})
            return (
                value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
            )

    def uuid():
        trace.append({"kind": "uuid", "value": generated_id})
        return UUID(generated_id)

    item = {
        "name": name,
        "model": "ChatTurn",
        "input": encoded,
        "raw_input": json.dumps(encoded, ensure_ascii=False),
        "input_origin": origin,
        "typed_paths": [{"path": ["usage"], "model": "ChatTokenUsage"}]
        if typed_usage
        else [],
        "datetime_fields": [
            name for name, value in supplied.items() if isinstance(value, datetime)
        ]
        if isinstance(supplied, dict)
        else [],
    }
    if constructor or boundary:
        item["generated_ids"] = [generated_id]
        item["model_clock_values"] = [value.isoformat() for value in clock_values]
    if boundary:
        item["strict_retained_boundary"] = boundary
    with ExitStack() as stack:
        stack.enter_context(patch("nebula.v3.domain.datetime", FrozenDatetime))
        stack.enter_context(patch("nebula.v3.domain.uuid4", side_effect=uuid))
        try:
            value = (
                ChatTurn(**supplied)
                if constructor
                else ChatTurn.model_validate(supplied)
            )
            item["expected"] = {
                "accepted": True,
                "payload": value.model_dump(mode="json"),
            }
        except ValidationError as error:
            item["expected"] = {
                "accepted": False,
                "errors": jsonable_encoder(error.errors(include_url=False)),
                "exception_preview": str(error)[:300],
            }
    item["expected_factory_trace"] = trace
    return item


def collect_turn_validation():
    assert Path(domain.__file__).resolve().is_relative_to(ROOT / "src")
    vectors = []

    def add(name, changes=None, remove=(), **kwargs):
        value = base_input()
        value.update(changes or {})
        for key in remove:
            value.pop(key)
        vectors.append(capture(name, value, **kwargs))

    add("defaults")
    for name, field in ChatTurn.model_fields.items():
        if name not in BASE_FIELDS and field.is_required():
            add("missing-" + name, remove=[name])
        add("null-" + name, {name: None})
    for value in [None, [], "opaque", 1]:
        vectors.append(capture("root-" + type(value).__name__, value))
    add(
        "trimmed-identifiers-and-prose",
        {
            "engagement_id": " project ",
            "session_id": " session ",
            "provider_profile_id": " provider ",
            "model": " fixture ",
            "content": " \u00a0text\u00a0 ",
            "reasoning": " thought ",
            "tool_call_ids": [" a ", " b "],
        },
    )
    add(
        "unicode-separators-retained",
        {"content": "\u001ctext\u001f", "model": "\u001cfixture\u001f"},
    )
    add(
        "empty-unbounded-identifiers",
        {"engagement_id": " ", "session_id": " ", "model": " "},
    )
    add("empty-provider", {"provider_profile_id": " "})
    add(
        "claim-empty-but-present",
        {
            "execution_owner_id": " ",
            "execution_claim_id": " ",
            "execution_claimed_at": NOW.isoformat(),
        },
    )
    for status in [
        "routing",
        "waiting_approval",
        "waiting_callback",
        "finalizing",
        "complete",
        "failed",
        "cancelled",
        "interrupted",
        " ROUTING ",
        1,
    ]:
        add("status-" + str(status), {"status": status})
    add("harness-binding", {"backend": "harness", "provider_profile_id": None})
    add(
        "harness-no-harness-turn-required",
        {"backend": "harness", "provider_profile_id": None, "harness_turn_id": None},
    )
    add("harness-provider-rejected", {"backend": "harness"})
    add("backend-enum", {"backend": " provider "})
    for name, value in [
        ("true", True),
        ("float", 2.0),
        ("fraction", 1.5),
        ("padded", " +001 "),
        ("decimal", "1.0"),
        ("exponent", "1e3"),
        ("underscore", "1_000"),
        ("repeated-leading-zero-underscore", "0__3600"),
        ("unicode", "١٢"),
        ("huge", 10**100),
        ("huge-string", "9" * 400),
        ("float-positive-edge", float(2**63)),
        ("float-negative-edge", float(-(2**63))),
        ("float-inward", math.nextafter(float(2**63), 0)),
    ]:
        add("integer-" + name, {"next_step": value})
    add(
        "bounded-counters",
        {
            "max_tool_calls": -1,
            "max_artifact_queries": -1,
            "next_step": -1,
            "execution_tool_calls": -1,
            "artifact_queries": -1,
            "scope_revision": 0,
        },
    )
    add("large-revision", {"revision": 10**100, "scope_revision": "1"})
    for name, value in [
        ("yes", "YeS"),
        ("off", "off"),
        ("padded", " true "),
        ("int-one", 1),
        ("int-two", 2),
        ("int-max", 2**63 - 1),
        ("int-overflow", 2**63),
        ("float-min", float(-(2**63))),
    ]:
        add("bool-" + name, {"tools_enabled": value})
    add("lists-required", {"tool_call_ids": {}, "tool_history": "history"})
    add(
        "list-item-errors",
        {"tool_call_ids": ["valid", 1, None], "tool_history": [{}, None, True]},
    )
    add(
        "typed-map-order",
        {
            "request_snapshot": {" a ": 1, "a": 2, "nested": {" z ": 3}},
            "tool_history": [{" a ": 1, "a": 2, "nested": {" z ": 3}}],
        },
    )
    add(
        "typed-map-order-reversed",
        {"request_snapshot": {"a": 2, " a ": 1}, "tool_history": [{"a": 2, " a ": 1}]},
    )
    add(
        "opaque-numbers",
        {
            "request_snapshot": {
                "huge": 10**100,
                "negative_zero": -0.0,
                "float": 1e-100,
            },
            "tool_history": [{"inner": [None, True, " untouched "]}],
        },
    )
    add("usage-defaults", {"usage": {}})
    add(
        "usage-coercion",
        {"usage": {"input_tokens": "2", "output_tokens": True, "total_tokens": 3.0}},
    )
    add(
        "usage-multiple-errors",
        {
            "usage": {
                "z_extra": "opaque",
                "input_tokens": -1,
                "output_tokens": "bad",
                "a_extra": True,
            }
        },
    )
    add("usage-root", {"usage": []})
    add("partial-owner", {"execution_owner_id": "worker"})
    add("partial-claim", {"execution_claim_id": "claim"})
    add("partial-time", {"execution_claimed_at": NOW.isoformat()})
    add(
        "binding-before-ownership",
        {"provider_profile_id": None, "execution_owner_id": "worker"},
    )
    add(
        "base-before-binding",
        {
            "updated_at": "2019-01-01T00:00:00Z",
            "provider_profile_id": None,
            "execution_owner_id": "worker",
        },
    )
    add(
        "fields-before-after",
        {
            "next_step": -1,
            "updated_at": "2019-01-01T00:00:00Z",
            "provider_profile_id": None,
        },
    )
    add("ordered-extras", {"z_extra": "DO-NOT-LOG-TURN", "a_extra": {"z": 1, "a": 2}})
    add(
        "string-bounds",
        {
            "execution_owner_id": "w" * 201,
            "execution_claim_id": "c" * 201,
            "error": "e" * 1001,
        },
    )
    add("content-boundary", {"content": "c" * 200_000})
    add(
        "reasoning-and-content-too-long",
        {"reasoning": "r" * 200_001, "content": "c" * 200_001},
    )
    for name, value in [
        ("naive", "2030-01-01T12:00:00"),
        ("offset", "2030-01-01T13:00:00.1234567+01:00"),
        ("epoch", 1893499200),
        ("date", "2030-01-01"),
        ("invalid", "not-a-date"),
        ("bool", True),
    ]:
        add(
            "claim-time-" + name,
            {
                "execution_owner_id": "worker",
                "execution_claim_id": "claim",
                "execution_claimed_at": value,
            },
        )
    add("created-naive", {"created_at": "2020-01-01T00:00:00"})
    add("updated-naive", {"updated_at": "2020-01-01T00:00:00"})
    add(
        "base-offset-normalized",
        {
            "created_at": "2020-01-01T01:00:00+01:00",
            "updated_at": "2020-01-01T01:00:01+01:00",
        },
    )
    add(
        "writer-after-input",
        {
            "created_at": BASE,
            "updated_at": BASE - timedelta(seconds=1),
            "execution_owner_id": "worker",
            "execution_claim_id": "claim",
            "execution_claimed_at": NOW,
        },
        origin="writer_model_dump",
    )
    add(
        "writer-partial-ownership",
        {"created_at": BASE, "updated_at": BASE, "execution_claimed_at": NOW},
        origin="writer_model_dump",
    )
    constructors = []
    minimal = {
        key: value for key, value in base_input().items() if key not in BASE_FIELDS
    }
    for name, value, opts in [
        ("generated-base", minimal, {}),
        ("explicit-id", {"id": "explicit", **minimal}, {}),
        ("explicit-created", {**minimal, "created_at": BASE.isoformat()}, {}),
        ("explicit-base-wins", base_input(), {"clocks": [NOW, BASE]}),
        ("independent-invalid-fields-still-run-factories", {**minimal, "model": 1}, {}),
        (
            "backwards-factories-before-binding",
            {**minimal, "provider_profile_id": None},
            {"clocks": [NOW, BASE]},
        ),
        ("binding-original-kwargs", {**minimal, "provider_profile_id": None}, {}),
        (
            "typed-usage",
            {**minimal, "usage": {"input_tokens": 2}},
            {"typed_usage": True},
        ),
        (
            "typed-usage-root-error",
            {**minimal, "usage": {"input_tokens": 2}},
            {"typed_usage": True, "clocks": [NOW, BASE]},
        ),
        ("explicit-null-is-not-factory", {**minimal, "id": None}, {}),
    ]:
        constructors.append(capture(name, value, constructor=True, **opts))
    boundaries = []
    for field in BASE_FIELDS:
        value = base_input()
        del value[field]
        boundaries.append(capture("missing-retained-" + field, value, boundary=field))
    return {
        "format": "nebula-assistant-turn-validation.v1",
        "pydantic_version": pydantic.__version__,
        "models": {
            model.__name__: model_metadata(model)
            for model in [ChatTurn, ChatTokenUsage]
        },
        "vectors": vectors,
        "constructor_vectors": constructors,
        "strict_retained_boundary_vectors": boundaries,
        "source_sha256": {
            "src/nebula/v3/domain.py": sha256(
                (ROOT / "src/nebula/v3/domain.py").read_bytes()
            ).hexdigest()
        },
        "scope": "Pure source models only; strict retained identity/time/revision requirements remain distinct from Python defaults.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(
            collect_turn_validation(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
