"""Registry contract tests; these do not establish runtime/UI acceptance."""

import pytest

from nebula.v3.application_model.registry import (
    CategoryDefinition, PropertyDefinition, RelationshipDefinition,
    SchemaRegistry, TypeDefinition,
)
from nebula.v3.application_model.catalog import builtin_registry


def object_type(name="Asset", **kwargs):
    return TypeDefinition(
        name=name, label=name, category="Structure", description="A resource.",
        properties=(PropertyDefinition(name="url", description="Resource URL"),),
        identity_hints=("url",), evidence_examples=("Recorded resource URL",),
        **kwargs,
    )


def registry(*types):
    return SchemaRegistry(
        (CategoryDefinition(name="Structure", purpose="Resources"),),
        types or (object_type(),),
        (RelationshipDefinition(
            name="loads", label="Loads", description="Loads a resource",
            source_types=("Asset",), target_types=("Asset",),
            evidence_examples=("Recorded initiator",),
        ),),
    )


def test_inheritance_discovery_and_compatibility():
    child = object_type().model_copy(update={
        "name": "JavaScriptAsset", "extends": "Asset", "properties": (),
    })
    schema = registry(object_type(), child)
    assert schema.lineage("JavaScriptAsset") == ("JavaScriptAsset", "Asset")
    assert schema.compatible("loads", "JavaScriptAsset", "Asset")
    result = schema.discover(category="Structure", query="javascript")
    assert len(result["types"]) == 1
    assert result["types"][0]["properties"][0]["name"] == "url"
    assert result["types"][0]["outgoing_relationships"] == ["loads"]
    schema.validate_properties("JavaScriptAsset", {})


@pytest.mark.parametrize("update", [
    {"extends": "Missing"}, {"extends": "Asset"},
    {"identity_hints": ("missing",)}, {"identity_hints": ("url", "url")},
    {"category": "Missing"},
])
def test_invalid_definitions_rejected(update):
    with pytest.raises(ValueError):
        registry(object_type().model_copy(update=update))


def test_project_extensions_are_isolated():
    base = registry()
    custom = object_type().model_copy(update={
        "name": "custom.Document", "extends": "Asset", "properties": (),
    })
    project = base.extend("project-a", types=(custom,))
    assert "custom.Document" not in base.types
    assert project.compatible("loads", "Asset", "custom.Document")
    with pytest.raises(ValueError, match="one project"):
        project.extend("project-b")
    with pytest.raises(ValueError, match="namespace"):
        base.extend("project-a", types=(object_type(),))
    with pytest.raises(TypeError):
        base.types["Other"] = custom


@pytest.mark.parametrize("kind,value", [
    ("integer", True), ("number", False), ("number", float("nan")),
    ("number", float("inf")), ("boolean", 1), ("string", 3),
    ("string", "x" * 4001),
])
def test_property_validation_does_not_coerce(kind, value):
    definition = object_type().model_copy(update={"properties": (
        PropertyDefinition(name="url", kind=kind, description="Test value"),
    )})
    with pytest.raises(ValueError):
        registry(definition).validate_properties("Asset", {"url": value})


def test_unknown_fields_and_duplicate_definitions_rejected():
    with pytest.raises(ValueError, match="Unknown property"):
        registry().validate_properties("Asset", {"secret_value": "redacted"})
    with pytest.raises(ValueError, match="Duplicate"):
        registry(object_type(), object_type())


def test_builtin_catalog_is_complete_and_discoverable_by_category():
    schema = builtin_registry()
    expected = {
        "Structure": "Application",
        "APIs": "Endpoint",
        "Identity": "AuthenticationFlow",
        "Security": "WAF",
        "Infrastructure": "ReverseProxy",
        "Dependencies": "Database",
        "Client execution": "JavaScriptAsset",
        "Browser storage": "Cookie",
        "Backend processing": "BackgroundJob",
        "Data": "QueryOperation",
    }
    assert list(schema.categories) == list(expected)
    for category, representative in expected.items():
        result = schema.discover(category=category)
        assert representative in {item["name"] for item in result["types"]}
    assert len(schema.types) == 77
    assert set(schema.relationships) == {
        "contains", "links_to", "submits_to", "accepts_input", "loads", "executes",
        "calls", "authenticates_via", "requires_permission", "uses_cookie",
        "governed_by", "stores_in", "reads_from", "writes_to", "queries",
        "served_by", "depends_on", "protected_by",
    }


def test_canonical_specializations_inherit_identity_and_compatibility():
    schema = builtin_registry()
    assert schema.lineage("JavaScriptAsset") == ("JavaScriptAsset", "Asset")
    assert schema.lineage("Database") == ("Database", "Storage")
    assert schema.lineage("QueryOperation") == ("QueryOperation", "Operation")
    assert schema.properties("JavaScriptAsset")["url"].description
    assert schema.compatible("served_by", "JavaScriptAsset", "CDN")
    assert schema.compatible("stores_in", "Operation", "Database")
    assert schema.compatible("executes", "Page", "QueryOperation")


def test_secret_bearing_types_have_metadata_not_secret_values():
    schema = builtin_registry()
    for name in ("Cookie", "Token", "CSRFToken", "LocalStorage", "SessionStorage"):
        properties = schema.properties(name)
        assert "value" not in properties
        assert "secret" not in properties
    assert schema.properties("Cookie")["same_site"].choices == ("strict", "lax", "none")
