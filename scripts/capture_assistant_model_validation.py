#!/usr/bin/env python3
"""Capture ordered Entity/Schedule validation, without a database or execution.

Schemas describe structural fields; ordered metadata and real Pydantic reports
retain field/model validators, original inputs and writer datetime provenance.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import types
from typing import Literal, Union, get_args, get_origin
from unittest.mock import patch
from uuid import UUID

from fastapi.encoders import jsonable_encoder
import pydantic
from pydantic import ValidationError

from nebula.v3.domain import ChatSchedule, Entity

ROOT = Path(__file__).resolve().parents[1]
BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
MODELS = {"Entity": Entity, "ChatSchedule": ChatSchedule}
RETAINED_FIELDS = ["id", "revision", "created_at", "updated_at"]


def annotation_spec(annotation):
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in {types.UnionType, Union}:
        return {"kind": "union", "members": [annotation_spec(item) for item in args]}
    if origin is Literal:
        return {"kind": "literal", "values": list(args)}
    if origin is not None:
        return {
            "kind": getattr(origin, "__name__", str(origin)),
            "members": [annotation_spec(item) for item in args],
        }
    if annotation is type(None):
        return {"kind": "null"}
    return {"kind": getattr(annotation, "__name__", str(annotation))}


def model_metadata(model):
    schema = model.model_json_schema()
    fields = []
    for name, field in model.model_fields.items():
        info = {
            "name": name,
            "required": field.is_required(),
            "annotation": annotation_spec(field.annotation),
            "schema": schema["properties"][name],
        }
        if field.is_required():
            info["default_kind"] = "required"
        elif field.default_factory is not None:
            info["default_kind"] = "factory"
            info["factory_name"] = field.default_factory.__name__
        else:
            info["default_kind"] = "literal"
            info["default"] = jsonable_encoder(field.default)
        fields.append(info)
    decorators = model.__pydantic_decorators__
    return {
        "python_model": model.__name__,
        "schema": schema,
        "fields": fields,
        "config": {
            key: model.model_config.get(key)
            for key in [
                "extra",
                "str_strip_whitespace",
                "validate_assignment",
                "populate_by_name",
            ]
        },
        "field_validators": [
            {"name": name, "fields": list(item.info.fields), "mode": item.info.mode}
            for name, item in decorators.field_validators.items()
        ],
        "model_validators": [
            {"name": name, "mode": item.info.mode}
            for name, item in decorators.model_validators.items()
        ],
    }


def base_input(model="ChatSchedule"):
    value = {
        "id": "fixture-schedule" if model == "ChatSchedule" else "fixture-entity",
        "created_at": BASE.isoformat(),
        "updated_at": BASE.isoformat(),
        "revision": 1,
    }
    if model == "ChatSchedule":
        value.update(
            engagement_id="project",
            session_id="session",
            provider_profile_id="provider",
            model="fixture",
            interval_seconds=3600,
            next_run_at=NOW.isoformat(),
        )
    return value


def vector(name, model, value, *, origin="retained_json", boundary=None):
    encoded = jsonable_encoder(value)
    result = {
        "name": name,
        "model": model,
        "input": encoded,
        "raw_input": json.dumps(encoded, ensure_ascii=False),
        "input_origin": origin,
    }
    dates = (
        [key for key, item in value.items() if isinstance(item, datetime)]
        if isinstance(value, dict)
        else []
    )
    if dates:
        result["datetime_fields"] = dates
    if boundary is not None:
        result["strict_retained_boundary"] = boundary
    try:
        validated = MODELS[model].model_validate(deepcopy(value))
        result["expected"] = {
            "accepted": True,
            "payload": validated.model_dump(mode="json"),
        }
    except ValidationError as error:
        result["expected"] = {
            "accepted": False,
            "errors": jsonable_encoder(error.errors(include_url=False)),
            "exception_preview": str(error)[:300],
        }
    return result


def validation_vectors():
    vectors, factory_vectors = [], []

    def add(name, changes=None, *, model="ChatSchedule", remove=()):
        value = base_input(model)
        value.update(changes or {})
        for field in remove:
            value.pop(field)
        vectors.append(vector(name, model, value))

    for model in MODELS:
        add(model + "-canonical", model=model)
        add(model + "-trimmed-id", {"id": "  fixture β  "}, model=model)
        add(model + "-empty-id", {"id": " \t "}, model=model)
        add(model + "-long-id", {"id": "😀" * 201}, model=model)
        add(model + "-id-number", {"id": 1}, model=model)
        add(model + "-extra-order", {"z_extra": 1, "a_extra": ["opaque"]}, model=model)
        add(
            model + "-updated-before-created",
            {"updated_at": "2019-01-01T00:00:00Z"},
            model=model,
        )
        add(
            model + "-created-naive", {"created_at": "2020-01-01T00:00:00"}, model=model
        )
        add(
            model + "-updated-naive", {"updated_at": "2020-01-01T00:00:00"}, model=model
        )
        add(
            model + "-base-times-normalized",
            {
                "created_at": "2020-01-01T01:00:00+01:00",
                "updated_at": "2019-12-31T19:00:01-05:00",
            },
            model=model,
        )
        for name, value in [
            ("zero", 0),
            ("bool", True),
            ("float", 1.0),
            ("fraction", 1.5),
            ("string", " +01 "),
            ("decimal-string", "1.0"),
            ("exponent-string", "1e3"),
            ("huge-string", "9" * 100),
            ("float-overflow", 1e20),
            ("float-i64-positive", float(2**63)),
            ("float-i64-negative", float(-(2**63))),
            ("float-below-i64", math.nextafter(float(2**63), 0)),
            ("null", None),
            ("list", []),
        ]:
            add(model + "-revision-" + name, {"revision": value}, model=model)
        # Successful factories/defaults are a separate Python observation. The
        # strict retained decoder never invents identity/revision/time fields.
        for field in RETAINED_FIELDS:
            value = base_input(model)
            value.pop(field)
            factory_vectors.append(
                vector(
                    model + "-missing-" + field,
                    model,
                    value,
                    boundary="missing_canonical_fields",
                )
            )
        for name, value in [
            ("array", []),
            ("null", None),
            ("string", "fixture"),
            ("number", 1),
        ]:
            vectors.append(vector(model + "-container-" + name, model, value))

    for field in [
        "engagement_id",
        "session_id",
        "provider_profile_id",
        "model",
        "interval_seconds",
        "next_run_at",
    ]:
        add("required-" + field, remove=[field])
    add("required-multiple", remove=["model", "interval_seconds", "next_run_at"])
    add(
        "field-order-not-input-order",
        {
            "enabled": "invalid",
            "interval_seconds": 0,
            "revision": 0,
            "id": None,
            "z_extra": 1,
            "a_extra": 2,
        },
    )
    add(
        "field-errors-suppress-model-after",
        {
            "updated_at": "2019-01-01T00:00:00Z",
            "enabled": "invalid",
            "interval_seconds": 0,
        },
    )
    add(
        "extra-suppresses-model-after",
        {"updated_at": "2019-01-01T00:00:00Z", "opaque": True},
    )
    add(
        "many-field-validator-errors",
        {
            "created_at": "2020-01-01T00:00:00",
            "updated_at": "2020-01-01T00:00:00",
            "next_run_at": "2030-01-01T12:00:00",
            "last_run_at": "2020-01-01T00:00:00",
        },
    )
    add(
        "model-after-original-input",
        {
            "id": "  source id  ",
            "revision": "1",
            "interval_seconds": "3600",
            "enabled": "yes",
            "updated_at": "2019-01-01T00:00:00Z",
        },
    )
    add(
        "nullable-defaults",
        {
            "paused_by": None,
            "last_run_at": None,
            "last_turn_id": None,
            "last_status": None,
            "skip_reason": None,
        },
    )
    add(
        "string-trimming",
        {
            "engagement_id": " project ",
            "session_id": " session ",
            "provider_profile_id": " provider ",
            "model": " fixture ",
            "last_turn_id": " turn ",
            "last_status": " done ",
            "skip_reason": " reason ",
        },
    )
    add(
        "empty-unbounded-identities",
        {
            "engagement_id": " ",
            "session_id": " ",
            "provider_profile_id": " ",
            "model": " ",
        },
    )
    for name, padding in [
        ("file-separator", "\u001c"),
        ("unit-separator", "\u001f"),
        ("nonbreaking-space", "\u00a0"),
    ]:
        add(
            "string-padding-" + name,
            {"id": padding + "fixture" + padding, "model": padding + "model" + padding},
        )
    add(
        "nullable-string-types",
        {"last_turn_id": 3, "last_status": [], "skip_reason": {}},
    )
    add(
        "nullable-string-limits",
        {"last_turn_id": "x" * 201, "last_status": "x" * 41, "skip_reason": "x" * 1001},
    )
    for name, value in [
        ("archive", "archive"),
        ("spaced", " archive "),
        ("unknown", "operator"),
        ("number", 1),
        ("bool", True),
        ("list", ["archive"]),
        ("conflict", "conflict"),
        ("rate-limit-middle", "x" * 120 + "rate limit" + "y" * 120),
        ("rate-limit-tail", "x" * 240 + "rate limit"),
    ]:
        add("literal-" + name, {"paused_by": value})
    for name, value in [
        ("false", False),
        ("int", 1),
        ("float", 0.0),
        ("yes", "YeS"),
        ("off", "off"),
        ("spaced", " true "),
        ("integer-other", 2),
        ("float-other", 1.5),
        ("null", None),
        ("dict", {}),
        ("conflict", "conflict"),
        ("rate-limit", "rate limit"),
    ]:
        add("enabled-" + name, {"enabled": value})
    for name, value in [
        ("below", 3599),
        ("maximum", 2592000),
        ("above", 2592001),
        ("bool", True),
        ("string", " 3_600.0 "),
        ("fraction", 3600.5),
        ("exponent", "3.6e3"),
        ("huge", "9" * 100),
        ("null", None),
        ("dict", {}),
    ]:
        add("interval-" + name, {"interval_seconds": value})
    for name, value in [
        ("zero-double-underscore", "0__3600"),
        ("zeros-double-underscore", "00__3600"),
        ("inner-double-underscore", "1__2"),
        ("arabic-digits", "٣٦٠٠"),
        ("fullwidth-digits", "３６００"),
    ]:
        for field in ("revision", "interval_seconds"):
            add(field + "-" + name, {field: value})
    for name, value in [
        ("naive", "2030-01-01T12:00:00"),
        ("date-only", "2030-01-01"),
        ("offset", "2030-01-01T13:00:00+01:00"),
        ("fraction", "2030-01-01T12:00:00.123456789Z"),
        ("space-separator", "2030-01-01 12:00:00Z"),
        ("lowercase", "2030-01-01t12:00:00z"),
        ("epoch-seconds", 1893499200),
        ("epoch-millis", 1893499200000),
        ("epoch-fraction", 1893499200.25),
        ("epoch-string", "1893499200"),
        ("epoch-bool", True),
        ("epoch-huge", 10**30),
        ("invalid", "not-a-date"),
        ("invalid-day", "2030-02-30T12:00:00Z"),
        ("invalid-month", "2030-13-01T12:00:00Z"),
        ("invalid-offset", "2030-01-01T12:00:00+25:00"),
        ("null", None),
        ("list", []),
        ("dict", {}),
    ]:
        add("schedule-time-" + name, {"next_run_at": value})
    add("last-time-naive", {"last_run_at": "2030-01-01T12:00:00"})
    add("last-time-offset", {"last_run_at": "2030-01-01T13:00:00+01:00"})
    add(
        "later-field-error-before-model",
        {"updated_at": "2019-01-01T00:00:00Z", "skip_reason": ["rate limit"]},
    )

    for model in MODELS:
        current = MODELS[model].model_validate(base_input(model))
        writer = current.model_dump(mode="python")
        writer["updated_at"] = BASE - timedelta(seconds=1)
        writer["revision"] = 2
        vectors.append(
            vector(
                model + "-writer-model-after", model, writer, origin="writer_model_dump"
            )
        )
        writer_naive = deepcopy(writer)
        writer_naive["updated_at"] = BASE.replace(tzinfo=None)
        vectors.append(
            vector(
                model + "-writer-naive-time",
                model,
                writer_naive,
                origin="writer_model_dump",
            )
        )
        writer_good = current.model_dump(mode="python")
        writer_good["updated_at"] = NOW
        writer_good["revision"] = 2
        vectors.append(
            vector(
                model + "-writer-valid", model, writer_good, origin="writer_model_dump"
            )
        )
    return vectors, factory_vectors


def collect_model_validation():
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    with ExitStack() as stack:
        stack.enter_context(patch("nebula.v3.domain.datetime", FrozenDatetime))
        stack.enter_context(patch("nebula.v3.domain.uuid4", return_value=UUID(int=41)))
        vectors, factory_vectors = validation_vectors()
    return {
        "format": "nebula.assistant-model-validation-oracle/v1",
        "pydantic_version": pydantic.__version__,
        "clock": NOW.isoformat(),
        "normalization": "No diagnostic field normalization. errors exclude only Pydantic url; FastAPI jsonable_encoder encodes exceptions as {} and Python datetime input as ISO strings. raw_input preserves input dictionary order. datetime_fields identifies writer Python datetime provenance. Missing canonical retained fields are separated from supported vectors.",
        "retained_required_fields": RETAINED_FIELDS,
        "models": {name: model_metadata(model) for name, model in MODELS.items()},
        "vectors": vectors,
        "factory_default_vectors": factory_vectors,
        "source_sha256": {
            f"src/nebula/v3/{name}.py": sha256(
                (ROOT / f"src/nebula/v3/{name}.py").read_bytes()
            ).hexdigest()
            for name in ["domain", "storage", "database", "api", "chat_schedules"]
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect_model_validation()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "vectors": len(result["vectors"]),
                "factory_default_vectors": len(result["factory_default_vectors"]),
                "sha256": sha256(args.output.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
