import json
from pathlib import Path

from scripts.capture_assistant_contract import assistant_openapi, assistant_path


def test_assistant_routes_include_generated_crud_but_not_other_areas():
    for path in (
        "/api/v1/chat/completions",
        "/api/v1/chat-sessions",
        "/api/v1/chat-messages/{id}",
    ):
        assert assistant_path(path)
    for path in (
        "/api/v1/missions",
        "/api/v1/browser/sessions",
        "/api/v1/reports",
        "/api/v1/engagements",
    ):
        assert not assistant_path(path)


def test_schema_projection_keeps_transitive_references_and_security():
    schema = {
        "openapi": "3.1.0",
        "info": {"title": "test"},
        "paths": {
            "/api/v1/chat-sessions": {
                "get": {"schema": {"$ref": "#/components/schemas/Session"}}
            },
            "/api/v1/reports": {
                "get": {"schema": {"$ref": "#/components/schemas/Report"}}
            },
        },
        "components": {
            "schemas": {
                "Session": {
                    "properties": {"turn": {"$ref": "#/components/schemas/Turn"}}
                },
                "Turn": {
                    "properties": {"session": {"$ref": "#/components/schemas/Session"}}
                },
                "Report": {"type": "object"},
            },
            "securitySchemes": {"Bearer": {"type": "http", "scheme": "bearer"}},
        },
    }
    selected = assistant_openapi(schema)
    assert set(selected["paths"]) == {"/api/v1/chat-sessions"}
    assert set(selected["components"]["schemas"]) == {"Session", "Turn"}
    assert (
        selected["components"]["securitySchemes"]
        == schema["components"]["securitySchemes"]
    )


def test_saved_inventory_is_scoped_and_does_not_claim_parity():
    path = (
        Path(__file__).parents[1] / "assistant-rs/compatibility/python-assistant.json"
    )
    contract = json.loads(path.read_text())
    assert contract["format"] == "nebula.assistant-compatibility/v1"
    assert contract["baseline_commit"] == "b5bab4372871a0fa3fe91968c95d73903796ab56"
    assert contract["rust_parity_verified"] is False
    assert contract["rust_implemented_routes"] == []
    assert all(assistant_path(route["path"]) for route in contract["routes"])
    paths = {r["path"] for r in contract["routes"]}
    assert "/api/v1/chat/completions" in paths
    assert "/api/v1/chat-sessions" in paths
    assert all(kind.startswith("chat_") for kind in contract["entities"])


def test_stored_record_oracle_matches_current_python_models():
    from scripts.capture_assistant_records import collect_records

    path = Path(__file__).parents[1] / "assistant-rs/compatibility/python-records.json"
    assert collect_records() == json.loads(path.read_text())
