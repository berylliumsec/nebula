"""Durable projection and query lifecycle for recorded application knowledge."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
from sqlalchemy import select, update, delete, func
from ..database import EntityRow, ApplicationModelOutboxRow
from ..domain import BrowserSession, Engagement, Artifact
from ..storage import NebulaStore, NotFoundError, ConflictError
from .domain import (
    MODEL_TYPES,
    ModelSession,
    Observation,
    Object,
    ObjectVersion,
    KnowledgeState,
    ObservedTransition,
    Assertion,
    SolverQuery,
    Value,
    semantic_hash,
)
from .ingestion import enabled, SOURCE_KINDS, envelope, enqueue_envelope
from .solver import isolated_solve


class ApplicationModelService:
    def __init__(self, store: NebulaStore, artifacts=None):
        self.store = store
        self.artifacts = artifacts
        self.worker = None
        self.queries: dict[str, asyncio.Task] = {}
        self.solver_slots = asyncio.Semaphore(2)

    def retry_projection(self, project, collection):
        self.check_enabled()
        self.get(ModelSession, project, collection)
        with self.store.database.session() as db:
            db.execute(
                update(ApplicationModelOutboxRow)
                .where(
                    ApplicationModelOutboxRow.engagement_id == project,
                    ApplicationModelOutboxRow.model_session_id == collection,
                    ApplicationModelOutboxRow.status == "pending",
                )
                .values(attempts=0, error=None)
            )
        return {"retry_scheduled": True}

    def check_enabled(self):
        if not enabled():
            raise ValueError(
                "Application model is disabled. Enable NEBULA_APPLICATION_MODEL on Core."
            )

    def get(self, model, project, identifier, collection=None):
        item = self.store.get(model, identifier)
        if item.engagement_id != project or (
            collection and getattr(item, "model_session_id", None) != collection
        ):
            raise NotFoundError(
                "Application model record does not belong to this project or collection"
            )
        return item

    def list(self, model, project, collection=None):
        with self.store.database.session() as db:
            query = select(EntityRow.payload).where(
                EntityRow.kind == model.entity_kind, EntityRow.engagement_id == project
            )
            if collection:
                query = query.where(
                    EntityRow.payload["model_session_id"].as_string() == collection
                )
            rows = db.scalars(
                query.order_by(EntityRow.created_at, EntityRow.id).limit(2001)
            ).all()
            if len(rows) > 2000:
                raise ValueError(
                    "Collection exceeds inspector limit; narrow or create a new collection"
                )
            return [model.model_validate(x) for x in rows]

    def create(self, project, browser_session_id, import_history=False):
        self.check_enabled()
        self.store.get(Engagement, project)
        self.get(BrowserSession, project, browser_session_id)
        item = self.store.create(
            ModelSession(engagement_id=project, browser_session_id=browser_session_id)
        )
        if import_history:
            self.import_history(project, item.id)
        return item

    def import_history(self, project, collection):
        self.check_enabled()
        session = self.get(ModelSession, project, collection)
        if session.status != "active":
            raise ValueError("Resume this collection before importing history")
        with self.store.transaction() as tx:
            rows = tx.session.scalars(
                select(EntityRow)
                .where(
                    EntityRow.engagement_id == project, EntityRow.kind.in_(SOURCE_KINDS)
                )
                .order_by(EntityRow.created_at, EntityRow.id)
            )
            count = 0
            for row in rows:
                data = envelope(row.kind, row.payload, tx.session)
                if data and data["browser_session_id"] == session.browser_session_id:
                    enqueue_envelope(tx.session, session.model_dump(mode="json"), data)
                    count += 1
            return {"records_considered": count}

    def status(self, project):
        self.store.get(Engagement, project)
        with self.store.database.session() as db:
            lag = db.scalar(
                select(func.count())
                .select_from(ApplicationModelOutboxRow)
                .where(
                    ApplicationModelOutboxRow.engagement_id == project,
                    ApplicationModelOutboxRow.status == "pending",
                )
            )
        return {
            "enabled": enabled(),
            "solver_available": importlib.util.find_spec("z3") is not None,
            "pending_count": lag,
        }

    def workspace(self, project, collection):
        session = self.get(ModelSession, project, collection)
        result = {"session": session}
        with self.store.database.session() as db:
            errors = db.scalars(
                select(ApplicationModelOutboxRow.error)
                .where(
                    ApplicationModelOutboxRow.model_session_id == collection,
                    ApplicationModelOutboxRow.error.is_not(None),
                )
                .limit(5)
            ).all()
            result["projection_errors"] = errors
        for key, model in (
            ("states", KnowledgeState),
            ("observations", Observation),
            ("objects", Object),
            ("object_versions", ObjectVersion),
            ("assertions", Assertion),
            ("queries", SolverQuery),
            ("transitions", ObservedTransition),
        ):
            result[key] = self.list(model, project, collection)
        return result

    def transition(self, project, collection, status):
        self.check_enabled()
        item = self.get(ModelSession, project, collection)
        return self.store.update(
            ModelSession, item.id, {"status": status}, expected_revision=item.revision
        )

    def remove(self, project, collection):
        self.get(ModelSession, project, collection)
        for query in self.list(SolverQuery, project, collection):
            if query.id in self.queries:
                self.queries[query.id].cancel()
        with self.store.transaction() as tx:
            for model in MODEL_TYPES:
                if model is ModelSession:
                    continue
                rows = tx.session.scalars(
                    select(EntityRow).where(
                        EntityRow.kind == model.entity_kind,
                        EntityRow.engagement_id == project,
                        EntityRow.payload["model_session_id"].as_string() == collection,
                    )
                ).all()
                for row in rows:
                    tx.delete(model, row.id)
            tx.session.execute(
                delete(ApplicationModelOutboxRow).where(
                    ApplicationModelOutboxRow.model_session_id == collection,
                    ApplicationModelOutboxRow.engagement_id == project,
                )
            )
            tx.delete(ModelSession, collection)

    def process_one(self, outbox_id):
        with self.store.transaction() as tx:
            row = tx.session.get(ApplicationModelOutboxRow, outbox_id)
            if not row or row.status != "pending":
                return
            session_row = tx.session.get(EntityRow, row.model_session_id)
            if not session_row or session_row.payload["status"] != "active":
                return
            # Revision guard serializes projection per collection, including
            # multiple workers. Failed transactions leave the checkpoint intact.
            session = ModelSession.model_validate(session_row.payload)
            tx.update(
                ModelSession,
                session.id,
                {
                    "processed_count": session.processed_count + 1,
                    "last_source_id": row.source_id,
                    "error": None,
                },
                expected_revision=session.revision,
            )
            data = row.payload
            data = {**data, "facts": dict(data["facts"])}
            body_id = data.get("response_body_artifact_id")
            representation_id = None
            if body_id and self.artifacts:
                artifact_row = tx.session.get(EntityRow, body_id)
                if (
                    not artifact_row
                    or artifact_row.kind != Artifact.entity_kind
                    or artifact_row.engagement_id != row.engagement_id
                ):
                    data["facts"]["response_body"] = Value(
                        kind="unknown", type="string", reason="capture_unavailable"
                    ).model_dump(mode="json")
                else:
                    artifact = Artifact.model_validate(artifact_row.payload)
                    if (
                        artifact.metadata.get("redacted") is True
                        and artifact.metadata.get("browser_session_id")
                        == session.browser_session_id
                    ):
                        try:
                            raw = self.artifacts.read(artifact)
                            body = json.loads(raw) if len(raw) <= 1_048_576 else None
                        except (OSError, ValueError):
                            body = None
                        if isinstance(body, dict):
                            for name, value in list(body.items())[:100]:
                                if not isinstance(name, str) or len(name) > 100:
                                    continue
                                if re.search(
                                    r"password|secret|token|cookie|authorization|session|csrf|key",
                                    name,
                                    re.I,
                                ) or (
                                    isinstance(value, str)
                                    and "redacted" in value.lower()
                                ):
                                    data["facts"]["response." + name] = Value(
                                        kind="unknown", type="string", reason="redacted"
                                    ).model_dump(mode="json")
                                elif (
                                    type(value) in (str, bool, int)
                                    and len(str(value)) <= 2000
                                ):
                                    data["facts"]["response." + name] = Value(
                                        kind="concrete",
                                        type={
                                            str: "string",
                                            bool: "boolean",
                                            int: "integer",
                                        }[type(value)],
                                        value=value,
                                    ).model_dump(mode="json")
                                    if name == "id":
                                        representation_id = value
            branch = str(data.get("tab_id") or "unattributed")
            observation_id = "amo_" + row.id[:40]
            shared = {
                "engagement_id": row.engagement_id,
                "model_session_id": row.model_session_id,
            }
            observation = Observation(
                id=observation_id,
                **shared,
                source_kind=row.source_kind,
                source_id=row.source_id,
                source_revision=row.source_revision,
                branch_key=branch,
                browser_session_id=session.browser_session_id,
                identity_id=data.get("identity_id"),
                tab_id=data.get("tab_id"),
                command_id=data.get("command_id"),
                action_id=data.get("action_id"),
                exchange_id=data.get("exchange_id"),
                tool_call_id=data.get("tool_call_id"),
                facts=data["facts"],
                evidence_ids=[
                    identifier
                    for identifier in data["evidence_ids"]
                    if (source := tx.session.get(EntityRow, identifier))
                    and source.engagement_id == row.engagement_id
                    and source.kind == "evidence"
                ],
                artifact_ids=[
                    identifier
                    for identifier in data["artifact_ids"]
                    if (source := tx.session.get(EntityRow, identifier))
                    and source.engagement_id == row.engagement_id
                    and source.kind == "artifacts"
                ],
                occurred_at=data["occurred_at"],
                causal_status="explicit"
                if data.get("command_id")
                or data.get("action_id")
                or data.get("exchange_id")
                else "unknown",
            )
            # Objects represent recorded source identities. Resource equivalence
            # across requests needs evidence and is not guessed from URL strings.
            route = data["facts"].get("route", {}).get("value")
            identity_key = (
                [session.id, branch, route, representation_id]
                if representation_id is not None and route
                else [session.id, row.source_kind, row.source_id]
            )
            object_id = "amo_obj_" + semantic_hash(identity_key)[:40]
            existing = tx.session.get(EntityRow, object_id)
            obj = Object(
                id=object_id,
                **shared,
                kind="response_representation"
                if representation_id is not None
                else "recorded_interaction",
                identity_key=json.dumps(identity_key),
                label=f"{route or row.source_kind}"
                + (
                    f" · id {representation_id}"
                    if representation_id is not None
                    else ""
                ),
            )
            if existing is None:
                tx.add(obj)
            properties = dict(observation.facts)
            properties["response_body"] = Value(
                kind="unknown", type="string", reason="capture_unavailable"
            )
            version = ObjectVersion(
                **shared,
                object_id=object_id,
                properties=properties,
                observation_ids=[observation.id],
                semantic_hash=semantic_hash(
                    {k: v.model_dump(mode="json") for k, v in properties.items()}
                ),
            )
            prior_row = tx.session.scalar(
                select(EntityRow)
                .where(
                    EntityRow.kind == KnowledgeState.entity_kind,
                    EntityRow.engagement_id == row.engagement_id,
                    EntityRow.payload["model_session_id"].as_string() == session.id,
                    EntityRow.payload["branch_key"].as_string() == branch,
                    EntityRow.payload["interpretation"].as_boolean().is_(False),
                )
                .order_by(EntityRow.created_at.desc(), EntityRow.id.desc())
                .limit(1)
            )
            prior = (
                KnowledgeState.model_validate(prior_row.payload) if prior_row else None
            )
            versions = []
            for identifier in prior.object_version_ids if prior else []:
                old = tx.session.get(EntityRow, identifier)
                if old and old.payload["object_id"] != object_id:
                    versions.append(identifier)
            versions.append(version.id)
            hashes = [
                [
                    tx.session.get(EntityRow, identifier).payload["object_id"],
                    tx.session.get(EntityRow, identifier).payload["semantic_hash"],
                ]
                for identifier in versions[:-1]
            ] + [[version.object_id, version.semantic_hash]]
            assertions = [
                Assertion(
                    **shared,
                    subject=object_id,
                    predicate=name,
                    value=value,
                    support="observed" if value.kind == "concrete" else "unknown",
                    lifecycle="accepted",
                    evidence_ids=[observation.id],
                    producer="extractor",
                )
                for name, value in observation.facts.items()
            ]
            prior_assertions = []
            for identifier in prior.assertion_ids if prior else []:
                old = tx.session.get(EntityRow, identifier)
                if old and old.payload["subject"] != object_id:
                    prior_assertions.append(identifier)
            state = KnowledgeState(
                **shared,
                branch_key=branch,
                parent_state_ids=[prior.id] if prior else [],
                observation_ids=[observation.id],
                object_version_ids=versions,
                assertion_ids=prior_assertions + [a.id for a in assertions],
                semantic_hash=semantic_hash(
                    {
                        "objects": sorted(hashes),
                        "assertions": sorted(
                            [
                                semantic_hash(
                                    Assertion.model_validate(
                                        tx.session.get(EntityRow, identifier).payload
                                    ).semantic_content()
                                )
                                for identifier in prior_assertions
                            ]
                            + [
                                semantic_hash(assertion.semantic_content())
                                for assertion in assertions
                            ]
                        ),
                    }
                ),
            )
            tx.add_all(
                [
                    observation,
                    version,
                    *assertions,
                    state,
                    ObservedTransition(
                        **shared,
                        source_state_id=prior.id if prior else None,
                        destination_state_id=state.id,
                        observation_id=observation.id,
                    ),
                ]
            )
            row.status = "complete"
            row.error = None
            row.attempts += 1

    def process_batch(self):
        if not enabled():
            return
        with self.store.database.session() as db:
            ids = list(
                db.scalars(
                    select(ApplicationModelOutboxRow.id)
                    .join(
                        EntityRow,
                        EntityRow.id == ApplicationModelOutboxRow.model_session_id,
                    )
                    .where(
                        ApplicationModelOutboxRow.status == "pending",
                        ApplicationModelOutboxRow.attempts < 3,
                        EntityRow.payload["status"].as_string() == "active",
                    )
                    .order_by(
                        ApplicationModelOutboxRow.created_at,
                        ApplicationModelOutboxRow.id,
                    )
                    .limit(100)
                )
            )
        for identifier in ids:
            try:
                self.process_one(identifier)
            except ConflictError:
                continue
            except Exception:
                with self.store.database.session() as db:
                    db.execute(
                        update(ApplicationModelOutboxRow)
                        .where(ApplicationModelOutboxRow.id == identifier)
                        .values(
                            attempts=ApplicationModelOutboxRow.attempts + 1,
                            error="Projection failed; source records remain available",
                        )
                    )

    def fields(self, project, collection, state_id):
        state = self.get(KnowledgeState, project, state_id, collection)
        result = {}
        for identifier in state.object_version_ids:
            version = self.get(ObjectVersion, project, identifier, collection)
            for name, value in version.properties.items():
                result[f"{version.object_id}.{name}"] = value.model_dump(mode="json")
        return result

    def diff(self, project, collection, left, right):
        a, b = (
            self.fields(project, collection, left),
            self.fields(project, collection, right),
        )
        return {
            "left_state_id": left,
            "right_state_id": right,
            "changes": [
                {"field": k, "before": a.get(k), "after": b.get(k)}
                for k in sorted(a.keys() | b.keys())
                if a.get(k) != b.get(k)
            ],
        }

    def propose(self, project, collection, proposal):
        self.check_enabled()
        self.get(ModelSession, project, collection)
        self.get(Object, project, proposal.subject, collection)
        for identifier in proposal.evidence_ids:
            self.get(Observation, project, identifier, collection)
        return self.store.create(
            Assertion(
                **proposal.model_dump(),
                engagement_id=project,
                model_session_id=collection,
                producer="agent",
                support="inferred",
                lifecycle="proposed",
            )
        )

    def query_payload(self, project, collection, query):
        state = self.get(KnowledgeState, project, query.state_id, collection)
        assumptions = []
        field_assertions = {}
        for identifier in dict.fromkeys([*state.assertion_ids, *query.assertion_ids]):
            assertion = self.get(Assertion, project, identifier, collection)
            if assertion.support == "observed" and assertion.value.kind == "concrete":
                field_assertions[f"{assertion.subject}.{assertion.predicate}"] = (
                    assertion.id
                )
            if assertion.formula is None:
                if identifier in query.assertion_ids:
                    raise ValueError(
                        "Selected assertion has no supported constraint formula"
                    )
                continue
            assumptions.append(
                {"id": identifier, "formula": assertion.formula.model_dump(mode="json")}
            )
        return {
            "fields": self.fields(project, collection, state.id),
            "formula": query.formula.model_dump(mode="json"),
            "assumptions": assumptions,
            "field_assertions": field_assertions,
            "timeout_ms": query.timeout_ms,
        }

    async def submit(self, project, collection, request):
        self.check_enabled()
        if len(self.queries) >= 16:
            raise ValueError("Solver queue is full; wait for a query to finish")
        self.get(ModelSession, project, collection)
        query = SolverQuery(
            engagement_id=project,
            model_session_id=collection,
            **request.model_dump(),
            formula_hash=semantic_hash(request.formula.model_dump(mode="json")),
        )
        payload = self.query_payload(project, collection, query)
        if len(payload["fields"]) > 1000 or len(json.dumps(payload)) > 900000:
            raise ValueError(
                "Selected state exceeds the V1 solver budget; choose an earlier state or smaller collection"
            )
        self.store.create(query)
        self.queries[query.id] = asyncio.create_task(self.run_query(query, payload))
        return query

    async def run_query(self, query, payload):
        try:
            async with self.solver_slots:
                current = self.store.update(
                    SolverQuery,
                    query.id,
                    {"status": "running"},
                    expected_revision=query.revision,
                )
                result = await isolated_solve(payload)
                self.store.update(
                    SolverQuery,
                    query.id,
                    {"status": "complete", **result},
                    expected_revision=current.revision,
                )
        except asyncio.CancelledError:
            self.finish_failed_query(query.id, "cancelled", "Query cancelled")
        except Exception:
            self.finish_failed_query(
                query.id,
                "failed",
                "Solver unavailable or worker failed; recorded state remains available",
            )
        finally:
            self.queries.pop(query.id, None)

    def cancel_query(self, project, collection, identifier):
        record = self.get(SolverQuery, project, identifier, collection)
        task = self.queries.pop(identifier, None)
        if task:
            task.cancel()
        if record.status in {"queued", "running"}:
            self.finish_failed_query(identifier, "cancelled", "Query cancelled")
        return self.get(SolverQuery, project, identifier, collection)

    def finish_failed_query(self, identifier, status, error):
        try:
            current = self.store.get(SolverQuery, identifier)
            self.store.update(
                SolverQuery,
                identifier,
                {"status": status, "error": error},
                expected_revision=current.revision,
            )
        except (NotFoundError, ConflictError):
            pass

    async def startup(self):
        # A lost worker cannot be represented as still running after restart.
        with self.store.database.session() as db:
            ids = list(
                db.scalars(
                    select(EntityRow.id).where(
                        EntityRow.kind == SolverQuery.entity_kind,
                        EntityRow.payload["status"]
                        .as_string()
                        .in_(["queued", "running"]),
                    )
                )
            )
        for identifier in ids:
            self.finish_failed_query(
                identifier,
                "failed",
                "Core restarted before query completed; submit the saved condition again",
            )
        self.worker = asyncio.create_task(self.loop())

    async def loop(self):
        while True:
            batch = asyncio.create_task(asyncio.to_thread(self.process_batch))
            try:
                await asyncio.shield(batch)
            except asyncio.CancelledError:
                await batch
                raise
            except Exception:
                # A transient database failure must not permanently stop the
                # projector or the independent browser capture workflow.
                pass
            await asyncio.sleep(1)

    async def shutdown(self):
        tasks = list(self.queries.values()) + ([self.worker] if self.worker else [])
        for identifier in list(self.queries):
            self.finish_failed_query(identifier, "cancelled", "Core is shutting down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
