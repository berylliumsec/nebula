"""Versioned application knowledge contracts; no execution capabilities."""

from __future__ import annotations

import hashlib
import json
from typing import Any, ClassVar, Literal
from pydantic import ConfigDict, Field, model_validator
from ..domain import Entity, NebulaModel


def semantic_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class Value(NebulaModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    kind: Literal["concrete", "symbolic", "unknown", "conditional", "alias"]
    type: Literal["boolean", "integer", "string", "enum", "identity"]
    value: bool | int | str | None = None
    symbol: str | None = None
    reason: (
        Literal["absent", "redacted", "capture_unavailable", "unobserved"] | None
    ) = None
    domain: list[str] = Field(default_factory=list, max_length=100)
    condition: str | None = None
    reference: str | None = None

    @model_validator(mode="after")
    def valid_value(self):
        if self.domain and self.type not in {"string", "enum", "identity"}:
            raise ValueError("Finite domains require a string, enum, or identity type")
        if self.kind == "concrete":
            expected = {
                "boolean": bool,
                "integer": int,
                "string": str,
                "enum": str,
                "identity": str,
            }[self.type]
            if type(self.value) is not expected:
                raise ValueError("Concrete value must match its declared type")
            if self.domain and self.value not in self.domain:
                raise ValueError("Value is outside its enum domain")
        elif self.value is not None:
            raise ValueError("Only concrete values may contain a literal")
        if self.kind == "unknown" and not self.reason:
            raise ValueError("Unknown value requires a reason")
        if self.kind == "symbolic" and not self.symbol:
            raise ValueError("Symbolic value requires a symbol")
        if self.kind == "alias" and not self.reference:
            raise ValueError("Alias requires a reference")
        if self.kind == "conditional" and not self.condition:
            raise ValueError("Conditional value requires a condition")
        return self


class Formula(NebulaModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    version: Literal[1] = 1
    op: Literal[
        "and", "or", "not", "eq", "ne", "lt", "le", "gt", "ge", "in", "field", "literal"
    ]
    args: list[Formula] = Field(default_factory=list, max_length=100)
    field: str | None = Field(default=None, max_length=500)
    value: bool | int | str | None = None
    values: list[bool | int | str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def shape(self):
        if self.op == "field":
            if not self.field or self.args or self.value is not None or self.values:
                raise ValueError("field node requires only a field name")
        elif self.op == "literal":
            if self.value is None or self.args or self.field or self.values:
                raise ValueError("literal node requires only a literal value")
        else:
            count = len(self.args)
            expected = 1 if self.op in {"not", "in"} else 2
            if (self.op in {"and", "or"} and count < 1) or (
                self.op not in {"and", "or"} and count != expected
            ):
                raise ValueError("Incorrect formula arity")
            if (
                self.field
                or self.value is not None
                or (self.values and self.op != "in")
            ):
                raise ValueError("Unexpected formula properties")
            if self.op == "in" and not self.values:
                raise ValueError("Membership requires a finite nonempty set")
        return self


class ModelSession(Entity):
    entity_kind: ClassVar[str] = "application_model_sessions"
    engagement_id: str
    browser_session_id: str
    status: Literal["active", "paused"] = "active"
    adapter_version: str = "1"
    last_source_id: str | None = None
    processed_count: int = 0
    error: str | None = None


class Record(Entity):
    model_config = ConfigDict(extra="forbid", frozen=True)
    engagement_id: str
    model_session_id: str
    schema_version: int = 1


class Observation(Record):
    entity_kind: ClassVar[str] = "application_model_observations"
    source_kind: str
    source_id: str
    source_revision: int
    adapter_version: str = "1"
    branch_key: str
    browser_session_id: str | None = None
    identity_id: str | None = None
    tab_id: str | None = None
    command_id: str | None = None
    action_id: str | None = None
    exchange_id: str | None = None
    tool_call_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    facts: dict[str, Value] = Field(default_factory=dict)
    causal_status: Literal["explicit", "unknown"] = "unknown"
    occurred_at: str


class Assertion(Record):
    entity_kind: ClassVar[str] = "application_model_assertions"
    subject: str
    predicate: str
    value: Value
    support: Literal["observed", "inferred", "unknown", "contradicted"] = "inferred"
    lifecycle: Literal["proposed", "accepted", "superseded"] = "proposed"
    evidence_ids: list[str] = Field(min_length=1, max_length=100)
    producer: Literal["extractor", "agent", "operator"] = "agent"
    supersedes_id: str | None = None
    formula: Formula | None = None

    def semantic_content(self):
        return self.model_dump(
            mode="json", include={"subject", "predicate", "value", "support", "formula"}
        )


class Object(Record):
    entity_kind: ClassVar[str] = "application_model_objects"
    kind: str
    identity_key: str
    label: str


class ObjectVersion(Record):
    entity_kind: ClassVar[str] = "application_model_object_versions"
    object_id: str
    properties: dict[str, Value]
    observation_ids: list[str]
    semantic_hash: str


class KnowledgeState(Record):
    entity_kind: ClassVar[str] = "application_model_states"
    branch_key: str
    parent_state_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    object_version_ids: list[str] = Field(default_factory=list)
    assertion_ids: list[str] = Field(default_factory=list)
    semantic_hash: str
    interpretation: bool = False


class ObservedTransition(Record):
    entity_kind: ClassVar[str] = "application_model_transitions"
    source_state_id: str | None
    destination_state_id: str
    observation_id: str
    effects: Literal["partial", "observed"] = "partial"


class Constraint(Record):
    entity_kind: ClassVar[str] = "application_model_constraints"
    formula: Formula
    assertion_ids: list[str] = Field(default_factory=list)


class SolverQuery(Record):
    entity_kind: ClassVar[str] = "application_model_queries"
    state_id: str
    formula: Formula
    assertion_ids: list[str] = Field(default_factory=list)
    timeout_ms: int = Field(default=5000, ge=1, le=5000)
    status: Literal[
        "queued", "running", "complete", "invalid", "cancelled", "failed"
    ] = "queued"
    result: Literal["SAT", "UNSAT", "UNKNOWN"] | None = None
    base_result: Literal["SAT", "UNSAT", "UNKNOWN"] | None = None
    assignments: dict[str, Any] = Field(default_factory=dict)
    assignment_types: dict[str, str] = Field(default_factory=dict)
    unsat_core: list[str] = Field(default_factory=list)
    error: str | None = None
    engine_version: str | None = None
    formula_hash: str


MODEL_TYPES = (
    ModelSession,
    Observation,
    Assertion,
    Object,
    ObjectVersion,
    KnowledgeState,
    ObservedTransition,
    Constraint,
    SolverQuery,
)
