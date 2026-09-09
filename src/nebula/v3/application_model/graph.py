"""Strict project graph transaction contracts. Missing properties stay absent."""

from typing import Annotated, Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
)
from .registry import TypeDefinition, RelationshipDefinition

Identifier = Annotated[
    str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")
]
Scalar = StrictStr | StrictBool | StrictInt | StrictFloat


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvidenceReference(Contract):
    kind: Literal[
        "browser_traffic",
        "browser_websocket_frames",
        "browser_actions",
        "browser_commands",
        "browser_repeater_results",
        "observations",
        "evidence",
    ]
    id: Identifier
    revision: int = Field(ge=1)
    role: Literal["supporting", "conflicting"] = "supporting"


class Claim(Contract):
    value: Scalar
    status: Literal["observed", "hypothesized", "disputed"] = "hypothesized"
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=2000)
    review: Literal["unreviewed", "accepted", "rejected"] = "unreviewed"


class PutObject(Contract):
    op: Literal["put_object"]
    id: Identifier
    label: str = Field(min_length=1, max_length=300)
    classification: Claim
    authentication_context: str = Field(min_length=1, max_length=200)
    properties: dict[str, Claim] = Field(default_factory=dict, max_length=100)


class PutRelationship(Contract):
    op: Literal["put_relationship"]
    id: Identifier
    type: str = Field(min_length=1, max_length=100)
    source: Identifier
    target: Identifier
    claim: Claim


class Dismiss(Contract):
    op: Literal["dismiss"]
    kind: Literal["object", "relationship", "property"]
    id: Identifier
    property: str | None = Field(default=None, max_length=80)
    reason: str = Field(min_length=1, max_length=2000)


class DefineType(Contract):
    op: Literal["define_type"]
    definition: TypeDefinition


class DefineRelationship(Contract):
    op: Literal["define_relationship"]
    definition: RelationshipDefinition


Operation = Annotated[
    PutObject | PutRelationship | Dismiss | DefineType | DefineRelationship,
    Field(discriminator="op"),
]


class GraphTransaction(Contract):
    expected_revision: int = Field(ge=0)
    idempotency_key: Identifier
    operations: list[Operation] = Field(min_length=1, max_length=100)
