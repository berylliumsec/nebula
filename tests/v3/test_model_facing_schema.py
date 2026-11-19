"""The model reads a trimmed tool schema; Core still validates the full one."""

from types import SimpleNamespace

from nebula.v3.chat import _model_facing_schema, _routing_input_schema
from nebula.v3.chat_planning import PLAN_SUBMIT_SPEC


def test_noise_keywords_leave_and_guidance_stays():
    schema = {
        "title": "Request",
        "type": "object",
        "properties": {
            # Property names are data, never keywords.
            "title": {"type": "string", "title": "Title", "maxLength": 200},
            "default": {"type": "boolean", "default": False},
            "body": {"type": "string", "minLength": 1, "maxLength": 16384},
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": ["title", "default"]},
                "uniqueItems": True,
                "maxItems": 8,
            },
            "count": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["title", "default"],
        "additionalProperties": False,
    }

    assert _model_facing_schema(schema) == {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 200},
            "default": {"type": "boolean"},
            "body": {"type": "string", "minLength": 1},
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": ["title", "default"]},
                "maxItems": 8,
            },
            "count": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["title", "default"],
        "additionalProperties": False,
    }
    # The tool's own schema, which Core validates against, is untouched.
    assert schema["properties"]["body"]["maxLength"] == 16384


def test_plan_submit_reaches_the_model_without_pydantic_noise():
    full = PLAN_SUBMIT_SPEC.input_schema
    shown = _routing_input_schema(SimpleNamespace(input_schema=full, path_arguments=()))

    assert '"title"' in str(full).replace("'", '"')
    assert "title" not in str(shown)
    assert "default" not in str(shown)
    assert shown["required"] == full["required"]
    assert shown["$defs"]["PlanStep"]["required"] == ["action", "expected_outcome"]
    assert len(str(shown)) < len(str(full)) * 0.75
