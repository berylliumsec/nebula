"""Durable evidence-bearing project graph with serializable, retryable edits."""

from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import insert, select, update
from fastapi import HTTPException
from ..database import EntityRow
from ..domain import Engagement, utc_now
from ..storage import NotFoundError
from .catalog import builtin_registry
from .graph import GraphTransaction, Claim
from .registry import TypeDefinition, RelationshipDefinition
from .persistence import graphs, edits
from .ingestion import envelope, SOURCE_KINDS


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def empty(project):
    return dict(
        project_id=project,
        revision=0,
        objects={},
        relationships={},
        types=[],
        relationship_types=[],
        tombstones={},
    )


def nonsecret(value):
    """Reject recognizable credential material; never retain URL queries/userinfo."""
    if not isinstance(value, str):
        return
    if len(value) > 4000:
        raise ValueError("Text must not exceed 4000 characters")
    if re.search(
        r"(?i)(bearer\s+\S+|-----BEGIN .*PRIVATE KEY|(?:password|secret|access_token|api_key)\s*[:=]\s*\S+)",
        value,
    ):
        raise ValueError("Keep credential values out of the graph")
    if re.match(r"^https?://", value):
        url = urlsplit(value)
        if url.username or url.password or url.query or url.fragment:
            raise ValueError(
                "Use a URL without credentials, query values, or fragments"
            )


class ApplicationModelService:
    def __init__(self, store, artifact_store=None):
        self.store = store

    def project(self, project):
        try:
            return self.store.get(Engagement, project)
        except NotFoundError as exc:
            raise HTTPException(404, "Project unavailable") from exc

    def _read(self, connection, project):
        payload = connection.execute(
            select(graphs.c.payload).where(graphs.c.project_id == project)
        ).scalar()
        return deepcopy(payload) if payload else empty(project)

    def snapshot(self, project):
        self.project(project)
        with self.store.database.engine.connect() as connection:
            return self._read(connection, project)

    def registry(self, graph):
        return builtin_registry().extend(
            graph["project_id"],
            types=tuple(TypeDefinition.model_validate(t) for t in graph["types"]),
            relationships=tuple(
                RelationshipDefinition.model_validate(t)
                for t in graph["relationship_types"]
            ),
        )

    def schema(self, project, category=None, query=""):
        return self.registry(self.snapshot(project)).discover(
            category=category, query=query
        )

    def workspace(self, project):
        graph = self.snapshot(project)
        return {
            **graph,
            "objects": list(graph["objects"].values()),
            "relationships": list(graph["relationships"].values()),
            "schema": self.registry(graph).discover(),
        }

    def search(self, project, query="", category=None, type=None, offset=0, limit=100):
        graph = self.snapshot(project)
        registry = self.registry(graph)
        selected = [
            o
            for o in graph["objects"].values()
            if query.casefold()
            in (o["label"] + " " + json.dumps(o["properties"])).casefold()
            and (
                category is None
                or registry.types[o["classification"]["value"]].category == category
            )
            and (type is None or type in registry.lineage(o["classification"]["value"]))
        ]
        return {
            "revision": graph["revision"],
            "objects": selected[offset : offset + limit],
            "total": len(selected),
            "next_offset": offset + limit if offset + limit < len(selected) else None,
        }

    def neighborhood(self, project, object_id, depth=1, limit=100):
        graph = self.snapshot(project)
        if object_id not in graph["objects"]:
            raise HTTPException(404, "Object unavailable; return to the project model")
        selected = {object_id}
        for _ in range(min(max(depth, 0), 3)):
            neighbors = {
                endpoint
                for r in graph["relationships"].values()
                if r["source"] in selected or r["target"] in selected
                for endpoint in (r["source"], r["target"])
            }
            selected.update(
                sorted(neighbors - selected)[: max(0, limit - len(selected))]
            )
        relationships = [
            r
            for r in graph["relationships"].values()
            if r["source"] in selected and r["target"] in selected
        ]
        frontier = {
            endpoint
            for r in graph["relationships"].values()
            if r["source"] in selected or r["target"] in selected
            for endpoint in (r["source"], r["target"])
        } - selected
        return {
            "revision": graph["revision"],
            "objects": [graph["objects"][k] for k in sorted(selected)],
            "relationships": relationships[:limit],
            "frontier_count": len(frontier),
            "truncated": bool(frontier) or len(relationships) > limit,
        }

    def _evidence(self, connection, project, kind, identifier, revision=None):
        if kind not in SOURCE_KINDS:
            raise ValueError("Unsupported evidence source")
        payload = connection.execute(
            select(EntityRow.payload).where(
                EntityRow.kind == kind,
                EntityRow.id == identifier,
                EntityRow.engagement_id == project,
            )
        ).scalar()
        if not payload:
            raise HTTPException(404, "Evidence unavailable in this project")
        if revision is not None and payload["revision"] != revision:
            raise HTTPException(409, "Evidence changed; inspect its current revision")
        normalized = envelope(kind, payload, connection)
        # Deliberately return allowlisted metadata, not request/response secrets.
        return {
            "kind": kind,
            "id": identifier,
            "revision": payload["revision"],
            "producer": payload.get("source") or kind,
            "occurred_at": payload.get("observed_at") or payload.get("created_at"),
            "context": {
                k: normalized.get(k)
                for k in ("browser_session_id", "tab_id", "identity_id", "occurred_at")
            }
            if normalized
            else {},
            "facts": normalized.get("facts", {}) if normalized else {},
            "source_available": True,
        }

    def evidence(self, project, kind, identifier):
        self.project(project)
        with self.store.database.engine.connect() as connection:
            return self._evidence(connection, project, kind, identifier)

    def evidence_list(self, project, offset=0, limit=100):
        self.project(project)
        with self.store.database.engine.connect() as connection:
            rows = connection.execute(
                select(EntityRow.kind, EntityRow.id)
                .where(
                    EntityRow.engagement_id == project, EntityRow.kind.in_(SOURCE_KINDS)
                )
                .order_by(EntityRow.created_at.desc(), EntityRow.id)
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return {
                "evidence": [
                    self._evidence(connection, project, row.kind, row.id)
                    for row in rows[:limit]
                ],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    def history(self, project, after=0, limit=100):
        self.project(project)
        with self.store.database.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(edits.c.payload)
                    .where(edits.c.project_id == project, edits.c.revision > after)
                    .order_by(edits.c.revision)
                    .limit(limit + 1)
                )
                .scalars()
                .all()
            )
            return {
                "edits": rows[:limit],
                "next_revision": rows[min(len(rows), limit) - 1]["revision"]
                if rows
                else after,
                "has_more": len(rows) > limit,
            }

    def _claim(self, connection, project, claim, stamp):
        claim = Claim.model_validate(claim)
        for value in (claim.value, claim.reason):
            nonsecret(value)
        if claim.status == "observed" and not any(
            r.role == "supporting" for r in claim.evidence
        ):
            raise ValueError("Observed claims require supporting source evidence")
        snapshots = [
            self._evidence(connection, project, r.kind, r.id, r.revision)
            for r in claim.evidence
        ]
        return {**claim.model_dump(mode="json"), **stamp, "sources": snapshots}

    def transact(self, project, request: GraphTransaction, *, producer="operator"):
        self.project(project)
        raw = request.model_dump(mode="json")
        fingerprint = digest({"producer": producer, "request": raw})
        with self.store.database.engine.connect() as connection:
            # Existing store lock: SQLite BEGIN IMMEDIATE, PostgreSQL advisory
            # transaction lock. Cross-process serialization, not an in-memory lock.
            self.store._begin_run_write(connection, "application-graph:" + project)
            try:
                if not connection.execute(
                    select(EntityRow.id).where(
                        EntityRow.id == project,
                        EntityRow.kind == Engagement.entity_kind,
                    )
                ).scalar():
                    raise HTTPException(404, "Project unavailable")
                receipt = connection.execute(
                    select(edits.c.digest, edits.c.payload).where(
                        edits.c.project_id == project,
                        edits.c.idempotency_key == request.idempotency_key,
                    )
                ).first()
                if receipt:
                    if receipt.digest != fingerprint:
                        raise HTTPException(
                            409, "Retry key was already used for a different edit"
                        )
                    return receipt.payload["result"]
                graph = self._read(connection, project)
                if graph["revision"] != request.expected_revision:
                    raise HTTPException(
                        409,
                        {
                            "message": "Model changed; review the current revision. Your draft is retained.",
                            "current_revision": graph["revision"],
                        },
                    )
                stamp = {
                    "revision": graph["revision"] + 1,
                    "producer": producer,
                    "updated_at": utc_now().isoformat(),
                }
                aliases, changes = {}, []
                for operation in request.operations:
                    op = operation.model_dump(mode="json")
                    kind = op.pop("op")
                    registry = self.registry(graph)
                    if kind in ("define_type", "define_relationship"):
                        definition = op["definition"]
                        for value in (definition["description"], definition["label"]):
                            nonsecret(value)
                        collection = (
                            "types" if kind == "define_type" else "relationship_types"
                        )
                        if any(
                            t["name"] == definition["name"] for t in graph[collection]
                        ):
                            raise ValueError(
                                "Definition already exists; use the existing project type"
                            )
                        if kind == "define_type":
                            for prop in definition["properties"]:
                                if re.search(
                                    r"(?i)(secret|password|token_value|cookie_value|credential)",
                                    prop["name"],
                                ):
                                    raise ValueError(
                                        "Secret properties are not allowed"
                                    )
                        graph[collection].append(definition)
                        self.registry(
                            graph
                        )  # Eager inheritance/compatibility validation.
                        changes.append({"op": kind, "definition": definition})
                        continue
                    identifier = aliases.get(op["id"], op["id"])
                    if kind == "dismiss":
                        collection = (
                            "objects"
                            if op["kind"] in ("object", "property")
                            else "relationships"
                        )
                        item = graph[collection].get(identifier)
                        if not item:
                            raise HTTPException(
                                404, "Selected graph item is unavailable"
                            )
                        nonsecret(op["reason"])
                        if op["kind"] == "property":
                            prop = op["property"]
                            if prop not in item["properties"]:
                                raise ValueError("Property is unavailable")
                            before = item["properties"].pop(prop)
                            graph["tombstones"][f"property:{identifier}:{prop}"] = stamp
                        else:
                            before = graph[collection].pop(identifier)
                            graph["tombstones"][f"{collection}:{identifier}"] = stamp
                            if collection == "objects" and before.get("identity"):
                                graph["tombstones"][
                                    "identity:" + before["identity"]
                                ] = stamp
                            if collection == "relationships":
                                graph["tombstones"][
                                    "edge:"
                                    + digest(
                                        [
                                            before["type"],
                                            before["source"],
                                            before["target"],
                                        ]
                                    )
                                ] = stamp
                            if collection == "objects":
                                for edge_id, edge in list(
                                    graph["relationships"].items()
                                ):
                                    if identifier in (edge["source"], edge["target"]):
                                        del graph["relationships"][edge_id]
                                        graph["tombstones"][
                                            f"relationships:{edge_id}"
                                        ] = stamp
                                        changes.append(
                                            {
                                                "op": "dismiss",
                                                "kind": "relationship",
                                                "id": edge_id,
                                                "before": edge,
                                                "reason": "Endpoint dismissed",
                                            }
                                        )
                        changes.append(
                            {"op": kind, **op, "id": identifier, "before": before}
                        )
                        continue
                    collection = "objects" if kind == "put_object" else "relationships"
                    if f"{collection}:{identifier}" in graph["tombstones"]:
                        raise HTTPException(
                            409, "This item was dismissed; review its history"
                        )
                    before = deepcopy(graph[collection].get(identifier))
                    if kind == "put_object":
                        nonsecret(op["label"])
                        nonsecret(op["authentication_context"])
                        typename = op["classification"]["value"]
                        if (
                            not isinstance(typename, str)
                            or typename not in registry.types
                        ):
                            raise ValueError("Choose a registered object type")
                        registry.validate_properties(
                            typename,
                            {k: v["value"] for k, v in op["properties"].items()},
                        )
                        if before and (
                            before["classification"]["value"] != typename
                            or before["authentication_context"]
                            != op["authentication_context"]
                        ):
                            raise ValueError(
                                "Object type and authentication context are immutable; create a separate object"
                            )
                        properties = deepcopy(before["properties"]) if before else {}
                        for key, value in op["properties"].items():
                            if f"property:{identifier}:{key}" in graph["tombstones"]:
                                raise HTTPException(
                                    409, "Property was dismissed; review its history"
                                )
                            properties[key] = self._claim(
                                connection, project, value, stamp
                            )
                        hints = registry.types[typename].identity_hints
                        identity = (
                            digest(
                                [
                                    typename,
                                    op["authentication_context"],
                                    [[h, properties[h]["value"]] for h in hints],
                                ]
                            )
                            if all(h in properties for h in hints)
                            else None
                        )
                        if identity and "identity:" + identity in graph["tombstones"]:
                            raise HTTPException(
                                409,
                                "Matching identity was dismissed; review its history",
                            )
                        for existing in graph["objects"].values():
                            if (
                                identity
                                and existing.get("identity") == identity
                                and existing["id"] != identifier
                            ):
                                aliases[op["id"]] = existing["id"]
                                raise HTTPException(
                                    409,
                                    {
                                        "message": "Matching object exists; reuse its identity",
                                        "object_id": existing["id"],
                                    },
                                )
                        item = {
                            **op,
                            "id": identifier,
                            "identity": identity,
                            **stamp,
                            "classification": self._claim(
                                connection, project, op["classification"], stamp
                            ),
                            "properties": properties,
                        }
                    else:
                        source, target = (
                            aliases.get(op["source"], op["source"]),
                            aliases.get(op["target"], op["target"]),
                        )
                        if (
                            source not in graph["objects"]
                            or target not in graph["objects"]
                        ):
                            raise ValueError(
                                "Both endpoints must exist in this project"
                            )
                        if not registry.compatible(
                            op["type"],
                            graph["objects"][source]["classification"]["value"],
                            graph["objects"][target]["classification"]["value"],
                        ):
                            raise ValueError(
                                "Relationship is incompatible with the endpoint types"
                            )
                        if (
                            "edge:" + digest([op["type"], source, target])
                            in graph["tombstones"]
                        ):
                            raise HTTPException(
                                409, "Relationship was dismissed; review its history"
                            )
                        if op["claim"]["value"] is not True:
                            raise ValueError("A relationship claim value must be true")
                        if any(
                            r["id"] != identifier
                            and (r["type"], r["source"], r["target"])
                            == (op["type"], source, target)
                            for r in graph["relationships"].values()
                        ):
                            raise HTTPException(
                                409,
                                "Relationship already exists; edit the existing relationship",
                            )
                        item = {
                            **op,
                            "id": identifier,
                            "source": source,
                            "target": target,
                            **stamp,
                            "claim": self._claim(
                                connection, project, op["claim"], stamp
                            ),
                        }
                    graph[collection][identifier] = item
                    changes.append(
                        {
                            "op": kind,
                            "id": identifier,
                            "before": before,
                            "after": deepcopy(item),
                        }
                    )
                if (
                    len(graph["objects"]) > 5000
                    or len(graph["relationships"]) > 10000
                    or len(json.dumps(graph)) > 8_000_000
                ):
                    raise ValueError(
                        "Project graph capacity reached; dismiss obsolete claims before adding more"
                    )
                graph["revision"] = stamp["revision"]
                if request.expected_revision == 0:
                    connection.execute(
                        insert(graphs).values(
                            project_id=project,
                            revision=graph["revision"],
                            payload=graph,
                        )
                    )
                else:
                    changed = connection.execute(
                        update(graphs)
                        .where(
                            graphs.c.project_id == project,
                            graphs.c.revision == request.expected_revision,
                        )
                        .values(revision=graph["revision"], payload=graph)
                    )
                    if changed.rowcount != 1:
                        raise HTTPException(
                            409, "Model changed; reload the current revision"
                        )
                result = {
                    "revision": graph["revision"],
                    "changed_ids": [c["id"] for c in changes if "id" in c],
                }
                connection.execute(
                    insert(edits).values(
                        project_id=project,
                        revision=graph["revision"],
                        idempotency_key=request.idempotency_key,
                        digest=fingerprint,
                        payload={**stamp, "changes": changes, "result": result},
                    )
                )
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise
