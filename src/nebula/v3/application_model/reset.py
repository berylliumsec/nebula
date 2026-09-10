"""Operator-only, project-scoped reset; shared files and evidence are retained."""

from sqlalchemy import and_, or_, select, func, delete, insert, update
from fastapi import HTTPException

from ..database import EntityRow
from ..domain import Engagement, utc_now
from .persistence import graphs, edits


def captures(project):
    return and_(
        EntityRow.engagement_id == project,
        or_(
            EntityRow.kind.in_(
                (
                    "browser_traffic",
                    "browser_websocket_frames",
                    "browser_repeater_results",
                )
            ),
            and_(
                EntityRow.kind == "observations",
                EntityRow.payload["source"].as_string() == "browser_companion",
            ),
        ),
    )


def preview(service, project):
    service.project(project)
    with service.store.database.engine.connect() as connection:
        graph = service._read(connection, project)
        return {
            "revision": graph["revision"],
            "objects": len(graph["objects"]),
            "relationships": len(graph["relationships"]),
            "captures": connection.execute(
                select(func.count()).select_from(EntityRow).where(captures(project))
            ).scalar_one(),
            "custom_definitions": len(graph["types"])
            + len(graph["relationship_types"]),
        }


def reset(service, project, request):
    from .service import digest, empty

    service.project(project)
    fingerprint = digest({"op": "reset", "request": request.model_dump(mode="json")})
    with service.store.database.engine.connect() as connection:
        service.store._begin_run_write(connection, "application-graph:" + project)
        try:
            if not connection.execute(
                select(EntityRow.id).where(
                    EntityRow.id == project, EntityRow.kind == Engagement.entity_kind
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
                        409, "This retry key belongs to a different edit"
                    )
                return receipt.payload["result"]
            old = service._read(connection, project)
            if old["revision"] != request.expected_revision:
                raise HTTPException(
                    409,
                    "The model changed. Review the latest counts before clearing; nothing was cleared.",
                )
            stamp = utc_now()
            removed = connection.execute(
                delete(EntityRow).where(captures(project))
            ).rowcount
            fresh = empty(project)
            fresh.update(revision=old["revision"] + 1, evidence_since=stamp.isoformat())
            connection.execute(delete(edits).where(edits.c.project_id == project))
            if old["revision"]:
                connection.execute(
                    update(graphs)
                    .where(graphs.c.project_id == project)
                    .values(revision=fresh["revision"], payload=fresh)
                )
            else:
                connection.execute(
                    insert(graphs).values(
                        project_id=project, revision=fresh["revision"], payload=fresh
                    )
                )
            result = {"revision": fresh["revision"], "cleared_captures": removed}
            connection.execute(
                insert(edits).values(
                    project_id=project,
                    revision=fresh["revision"],
                    idempotency_key=request.idempotency_key,
                    digest=fingerprint,
                    payload={
                        "revision": fresh["revision"],
                        "producer": "operator",
                        "updated_at": stamp.isoformat(),
                        "changes": [
                            {
                                "op": "reset",
                                "reason": "Application model and browser captures cleared",
                            }
                        ],
                        "result": result,
                    },
                )
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
