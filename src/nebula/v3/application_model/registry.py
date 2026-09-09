"""Discoverable vocabulary for agent-composed graphs, independent of capture.

This module does not create graph objects, infer facts, or access browser state.
Property values must be wrapped in evidence-bearing claims by the graph service.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CategoryDefinition(Definition):
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=500)


class PropertyDefinition(Definition):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    kind: Literal["string", "integer", "number", "boolean", "enum"] = "string"
    description: str = Field(min_length=1, max_length=500)
    choices: tuple[str, ...] = ()

    @model_validator(mode="after")
    def enum_choices(self):
        if (self.kind == "enum") != bool(self.choices):
            raise ValueError("Only enum properties require nonempty choices")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("Enum choices must be unique")
        return self


class TypeDefinition(Definition):
    name: str = Field(pattern=r"^(custom\.)?[A-Z][A-Za-z0-9]{0,79}$")
    label: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    extends: str | None = None
    legacy: bool = False
    properties: tuple[PropertyDefinition, ...] = ()
    identity_hints: tuple[str, ...] = Field(min_length=1)
    evidence_examples: tuple[str, ...] = Field(min_length=1)


class RelationshipDefinition(Definition):
    name: str = Field(pattern=r"^(custom\.)?[a-z][a-z0-9_]{0,79}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    source_types: tuple[str, ...] = Field(min_length=1)
    target_types: tuple[str, ...] = Field(min_length=1)
    evidence_examples: tuple[str, ...] = Field(min_length=1)


class SchemaRegistry:
    """Immutable registry snapshot; extend per project, never mutate globally.

    Identity hints guide evidence interpretation, not automatic merging. No field
    is required merely because it exists in the catalog: unknowns stay absent.
    """

    version = 2

    def __init__(
        self,
        categories: tuple[CategoryDefinition, ...],
        types: tuple[TypeDefinition, ...],
        relationships: tuple[RelationshipDefinition, ...],
        *,
        project_id: str | None = None,
    ):
        self.project_id = project_id
        self.categories = self._index(categories)
        self.types = self._index(types)
        self.relationships = self._index(relationships)
        for item in self.types.values():
            if item.category not in self.categories:
                raise ValueError(f"Unknown category: {item.category}")
            self.lineage(item.name)  # Validate cycles and missing parents eagerly.
            properties = self.properties(item.name)
            if len(set(item.identity_hints)) != len(item.identity_hints):
                raise ValueError(f"Duplicate identity hint: {item.name}")
            for hint in item.identity_hints:
                if hint not in properties:
                    raise ValueError(f"Unknown identity property: {item.name}.{hint}")
        for relation in self.relationships.values():
            for endpoint in (*relation.source_types, *relation.target_types):
                if endpoint != "*" and endpoint not in self.types:
                    raise ValueError(f"Unknown relationship endpoint type: {endpoint}")

    @staticmethod
    def _index(items):
        indexed = {item.name: item for item in items}
        if len(indexed) != len(items):
            raise ValueError("Duplicate schema definition")
        return MappingProxyType(indexed)

    def lineage(self, name: str) -> tuple[str, ...]:
        result: list[str] = []
        current: str | None = name
        while current:
            if current in result:
                raise ValueError(f"Cyclic type inheritance: {name}")
            if current not in self.types:
                raise ValueError(f"Unknown object type: {current}")
            result.append(current)
            current = self.types[current].extends
        return tuple(result)

    def properties(self, name: str) -> dict[str, PropertyDefinition]:
        resolved = {}
        for ancestor in reversed(self.lineage(name)):
            for prop in self.types[ancestor].properties:
                if prop.name in resolved:
                    raise ValueError(
                        f"Duplicate or overridden property: {name}.{prop.name}"
                    )
                resolved[prop.name] = prop
        return resolved

    def validate_properties(self, name: str, values: dict[str, object]) -> None:
        """Validate shape without coercion, defaults, or claims of evidential truth."""
        properties = self.properties(name)
        for key, value in values.items():
            prop = properties.get(key)
            if prop is None:
                raise ValueError(f"Unknown property: {name}.{key}")
            valid = {
                "string": type(value) is str,
                "enum": type(value) is str and value in prop.choices,
                "integer": type(value) is int,
                "number": type(value) in (int, float),
                "boolean": type(value) is bool,
            }[prop.kind]
            if not valid:
                raise ValueError(f"Invalid {prop.kind} value: {name}.{key}")
            if type(value) is float and not math.isfinite(value):
                raise ValueError("Numbers must be finite")
            if type(value) is str and len(value) > 4000:
                raise ValueError("Property strings must not exceed 4000 characters")

    def compatible(self, relationship: str, source: str, target: str) -> bool:
        if relationship not in self.relationships:
            raise ValueError(f"Unknown relationship: {relationship}")
        relation = self.relationships[relationship]
        source_lineage, target_lineage = self.lineage(source), self.lineage(target)
        return (
            "*" in relation.source_types
            or bool(set(source_lineage).intersection(relation.source_types))
        ) and (
            "*" in relation.target_types
            or bool(set(target_lineage).intersection(relation.target_types))
        )

    def discover(self, *, category: str | None = None, query: str = "") -> dict:
        """Return resolved fields and relationships for grouped agent/UI discovery."""
        if category is not None and category not in self.categories:
            raise ValueError(f"Unknown category: {category}")
        needle = query.strip().casefold()
        selected = [
            item
            for item in self.types.values()
            if (category is None or item.category == category)
            and needle in f"{item.name} {item.label} {item.description}".casefold()
        ]
        types = []
        used_relationships = set()
        for item in selected:
            lineage = set(self.lineage(item.name))
            outgoing, incoming = [], []
            for relation in self.relationships.values():
                if "*" in relation.source_types or lineage.intersection(
                    relation.source_types
                ):
                    outgoing.append(relation.name)
                    used_relationships.add(relation.name)
                if "*" in relation.target_types or lineage.intersection(
                    relation.target_types
                ):
                    incoming.append(relation.name)
                    used_relationships.add(relation.name)
            types.append(
                {
                    **item.model_dump(mode="json"),
                    "properties": [
                        p.model_dump(mode="json")
                        for p in self.properties(item.name).values()
                    ],
                    "outgoing_relationships": outgoing,
                    "incoming_relationships": incoming,
                }
            )
        return {
            "version": self.version,
            "project_id": self.project_id,
            "categories": [
                c.model_dump(mode="json")
                for c in self.categories.values()
                if category is None or c.name == category
            ],
            "types": types,
            "relationships": [
                r.model_dump(mode="json")
                for r in self.relationships.values()
                if r.name in used_relationships
            ],
        }

    def extend(
        self,
        project_id: str,
        *,
        types: tuple[TypeDefinition, ...] = (),
        relationships: tuple[RelationshipDefinition, ...] = (),
        categories: tuple[CategoryDefinition, ...] = (),
    ) -> SchemaRegistry:
        if not project_id.strip() or (
            self.project_id and self.project_id != project_id
        ):
            raise ValueError("Custom schema belongs to one project")
        for item in (*types, *relationships, *categories):
            if not item.name.startswith("custom."):
                raise ValueError("Custom definitions require the custom. namespace")
        return SchemaRegistry(
            (*self.categories.values(), *categories),
            (*self.types.values(), *types),
            (*self.relationships.values(), *relationships),
            project_id=project_id,
        )
