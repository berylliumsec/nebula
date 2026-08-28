"""Privacy-preserving metadata projection and federated resource search."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .action_registry import ActionRegistry
from .database import EntityRow, SearchDocumentRow
from .domain import (
    ActionResolutionRequest,
    ResourceKind,
    ResourceRef,
    SearchResponse,
    SearchResult,
    SearchScope,
)
from .storage import NebulaStore


@dataclass(frozen=True)
class SearchProjection:
    resource_kind: ResourceKind
    label: str
    description: str = ""
    breadcrumb: str = ""
    content: str = ""


INDEXED_ENTITY_KINDS = (
    "engagements",
    "chat_sessions",
    "observations",
    "knowledge",
    "library_items",
    "assets",
    "evidence",
    "findings",
    "reports",
    "command_executions",
    "automation_sessions",
    "browser_sessions",
    "browser_assessments",
    "runs",
    "operator_executions",
    "approvals",
    "action_intents",
)


def _text(value: object, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    return str(value).strip()[:limit]


def _safe_url(value: object) -> str:
    """Keep useful location metadata while dropping credentials and query secrets."""

    if not isinstance(value, str):
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))[:2000]


def project_search_document(
    kind: str, payload: dict[str, object]
) -> SearchProjection | None:
    """Return only explicitly permitted durable metadata; never arbitrary payload JSON."""

    status = _text(payload.get("status"), 100)
    if kind == "engagements":
        return SearchProjection(
            ResourceKind.PROJECT,
            _text(payload.get("name"), 500),
            _text(payload.get("description")),
            "Projects",
            " ".join(
                filter(
                    None,
                    [
                        _text(payload.get("tags")),
                        _text(payload.get("workspace_path"), 2000),
                    ],
                )
            ),
        )
    if kind == "chat_sessions":
        return SearchProjection(
            ResourceKind.CONVERSATION,
            _text(payload.get("title"), 500) or "Conversation",
            _text(payload.get("model"), 300),
            "Workbench",
        )
    if kind == "observations":
        return SearchProjection(
            ResourceKind.NOTE,
            _text(payload.get("title"), 500) or "Note",
            _text(payload.get("body")),
            "Notes",
            _text(payload.get("observation_type"), 200),
        )
    if kind == "knowledge":
        return SearchProjection(
            ResourceKind.SOURCE,
            _text(payload.get("name") or payload.get("title"), 500) or "Source",
            _text(payload.get("citation") or payload.get("description")),
            "Sources",
            _text(payload.get("source_type"), 200),
        )
    if kind == "library_items":
        return SearchProjection(
            ResourceKind.LIBRARY_ITEM,
            _text(payload.get("name") or payload.get("title"), 500) or "Library item",
            _text(payload.get("citation") or payload.get("description")),
            "Library",
            _text(payload.get("source_type"), 200),
        )
    if kind == "assets":
        return SearchProjection(
            ResourceKind.ASSET,
            _text(payload.get("name"), 500),
            _text(payload.get("hostname") or payload.get("address")),
            "Assets",
            _text(payload.get("tags")),
        )
    if kind == "evidence":
        return SearchProjection(
            ResourceKind.EVIDENCE,
            _text(payload.get("title"), 500),
            _text(payload.get("description")),
            "Evidence",
            " ".join(
                filter(
                    None,
                    [
                        _text(payload.get("evidence_type"), 100),
                        _text(payload.get("sha256"), 100),
                    ],
                )
            ),
        )
    if kind == "findings":
        return SearchProjection(
            ResourceKind.FINDING,
            _text(payload.get("title"), 500),
            _text(payload.get("description")),
            "Findings",
            " ".join(
                filter(
                    None,
                    [
                        status,
                        _text(payload.get("severity"), 100),
                        _text(payload.get("cve_ids")),
                        _text(payload.get("cwe_ids")),
                    ],
                )
            ),
        )
    if kind == "reports":
        return SearchProjection(
            ResourceKind.REPORT,
            _text(payload.get("title"), 500),
            _text(payload.get("executive_summary")),
            "Reports",
            status,
        )
    if kind == "command_executions":
        # The command and its digest are metadata; output and errors are deliberately absent.
        return SearchProjection(
            ResourceKind.TERMINAL_COMMAND,
            _text(payload.get("command"), 500),
            _text(payload.get("cwd"), 500),
            "Terminal history",
            " ".join(filter(None, [status, _text(payload.get("command_sha256"), 100)])),
        )
    if kind == "automation_sessions":
        return SearchProjection(
            ResourceKind.TERMINAL_SESSION,
            f"{_text(payload.get('owner_kind'), 100) or 'Terminal'} session",
            status,
            "Terminal sessions",
            " ".join(
                filter(
                    None,
                    [
                        _text(payload.get("runtime_image"), 500),
                        _text(payload.get("runtime_digest"), 100),
                    ],
                )
            ),
        )
    if kind == "browser_sessions":
        raw_tabs = payload.get("tabs")
        tabs: list[object] = raw_tabs if isinstance(raw_tabs, list) else []
        tab_text = " ".join(
            filter(
                None,
                (
                    _text(tab.get("title"), 500) + " " + _safe_url(tab.get("url"))
                    for tab in tabs
                    if isinstance(tab, dict)
                ),
            )
        )
        return SearchProjection(
            ResourceKind.BROWSER_SESSION,
            _text(payload.get("name"), 500),
            status,
            "Browser sessions",
            tab_text[:8000],
        )
    if kind == "browser_assessments":
        raw_targets = payload.get("target_urls")
        targets = raw_targets if isinstance(raw_targets, list) else []
        safe_targets = " ".join(_safe_url(target) for target in targets)
        return SearchProjection(
            ResourceKind.BROWSER_ASSESSMENT,
            _text(payload.get("name"), 500) or "Security Browser assessment",
            _text(payload.get("objective")),
            "Security Browser",
            " ".join(
                filter(
                    None,
                    [
                        status,
                        _text(payload.get("phase"), 100),
                        _text(payload.get("profile"), 100),
                        safe_targets,
                    ],
                )
            )[:8000],
        )
    if kind == "runs":
        return SearchProjection(
            ResourceKind.MISSION,
            _text(payload.get("objective"), 500) or "Mission",
            status,
            "Missions",
        )
    if kind == "operator_executions":
        return SearchProjection(
            ResourceKind.EXECUTION,
            f"{_text(payload.get('language'), 100) or 'Operator'} execution",
            status,
            "Executions",
            _text(payload.get("source_sha256"), 100),
        )
    if kind == "approvals":
        return SearchProjection(
            ResourceKind.APPROVAL,
            _text(payload.get("policy_rationale"), 500) or "Approval",
            status,
            "Approvals",
            " ".join(
                filter(
                    None,
                    [
                        _text(payload.get("risk_class"), 100),
                        _text(payload.get("target"), 500),
                    ],
                )
            ),
        )
    if kind == "action_intents":
        return SearchProjection(
            ResourceKind.RECEIPT,
            f"{_text(payload.get('action_id'), 100) or 'Device action'} receipt",
            status,
            "Action receipts",
            " ".join(
                filter(
                    None,
                    [
                        _text(payload.get("selected_device_id"), 200),
                        _text(payload.get("error"), 500),
                    ],
                )
            ),
        )
    return None


def upsert_search_document(session: Session, row: EntityRow) -> None:
    projection = project_search_document(row.kind, row.payload)
    current = session.get(SearchDocumentRow, row.id)
    if projection is None:
        if current is not None:
            session.delete(current)
        return
    values = dict(
        project_id=row.id if row.kind == "engagements" else row.engagement_id,
        resource_kind=projection.resource_kind.value,
        resource_id=row.id,
        revision=row.revision,
        label=projection.label or row.id,
        description=projection.description,
        breadcrumb=projection.breadcrumb,
        content=projection.content,
        updated_at=row.updated_at,
    )
    if current is None:
        session.add(SearchDocumentRow(id=row.id, **values))
    else:
        for key, value in values.items():
            setattr(current, key, value)
    if row.kind == "browser_sessions":
        tab_prefix = f"{row.id}::tab::"
        existing_tabs = {
            item.id: item
            for item in session.scalars(
                select(SearchDocumentRow).where(
                    SearchDocumentRow.id.like(f"{tab_prefix}%")
                )
            )
        }
        raw_tabs = row.payload.get("tabs")
        tabs: list[object] = raw_tabs if isinstance(raw_tabs, list) else []
        desired: set[str] = set()
        for tab in tabs:
            if not isinstance(tab, dict) or not tab.get("id"):
                continue
            tab_id = str(tab["id"])
            document_id = f"{tab_prefix}{tab_id}"
            desired.add(document_id)
            tab_values = dict(
                project_id=row.engagement_id,
                resource_kind=ResourceKind.BROWSER_TAB.value,
                resource_id=f"{row.id}/{tab_id}",
                revision=row.revision,
                label=_text(tab.get("title"), 500) or "Browser tab",
                description=_safe_url(tab.get("url")),
                breadcrumb="Browser tabs",
                content=_safe_url(tab.get("url")),
                updated_at=row.updated_at,
            )
            tab_document = existing_tabs.get(document_id)
            if tab_document is None:
                session.add(SearchDocumentRow(id=document_id, **tab_values))
            else:
                for key, value in tab_values.items():
                    setattr(tab_document, key, value)
        for document_id, tab_document in existing_tabs.items():
            if document_id not in desired:
                session.delete(tab_document)


class FederatedSearch:
    def __init__(self, store: NebulaStore, actions: ActionRegistry) -> None:
        self.store = store
        self.actions = actions

    def repair_stale_projection(self) -> bool:
        repaired = False
        with self.store.database.session() as session:
            entity_signature = session.execute(
                select(
                    func.count(EntityRow.id),
                    func.coalesce(func.sum(EntityRow.revision), 0),
                    func.max(EntityRow.updated_at),
                ).where(EntityRow.kind.in_(INDEXED_ENTITY_KINDS))
            ).one()
            document_signature = session.execute(
                select(
                    func.count(SearchDocumentRow.id),
                    func.coalesce(func.sum(SearchDocumentRow.revision), 0),
                    func.max(SearchDocumentRow.updated_at),
                ).where(SearchDocumentRow.id == SearchDocumentRow.resource_id)
            ).one()
            if entity_signature == document_signature:
                return False
            rows = list(
                session.scalars(
                    select(EntityRow).where(EntityRow.kind.in_(INDEXED_ENTITY_KINDS))
                )
            )
            documents = {
                item.id: item for item in session.scalars(select(SearchDocumentRow))
            }
            row_ids = {row.id for row in rows}
            for row in rows:
                if row.kind == "browser_sessions":
                    raw_tabs = row.payload.get("tabs")
                    if isinstance(raw_tabs, list):
                        row_ids.update(
                            f"{row.id}::tab::{tab['id']}"
                            for tab in raw_tabs
                            if isinstance(tab, dict) and tab.get("id")
                        )
            for row in rows:
                document = documents.get(row.id)
                if document is None or document.revision != row.revision:
                    upsert_search_document(session, row)
                    repaired = True
            for document in documents.values():
                if document.id not in row_ids:
                    session.delete(document)
                    repaired = True
        return repaired

    def search(
        self,
        *,
        query: str,
        active_project: str | None,
        scope: SearchScope,
        kinds: list[ResourceKind],
        cursor: str | None,
        limit: int,
    ) -> SearchResponse:
        repaired = self.repair_stale_projection()
        offset = int(base64.urlsafe_b64decode(cursor + "===").decode()) if cursor else 0
        terms = [term.casefold() for term in query.split() if term]
        with self.store.database.session() as session:
            statement = select(SearchDocumentRow)
            if scope == SearchScope.ACTIVE:
                statement = statement.where(
                    or_(
                        SearchDocumentRow.project_id == active_project,
                        SearchDocumentRow.project_id.is_(None),
                    )
                )
            if kinds:
                statement = statement.where(
                    SearchDocumentRow.resource_kind.in_([kind.value for kind in kinds])
                )
            for term in terms:
                pattern = f"%{term}%"
                statement = statement.where(
                    or_(
                        func.lower(SearchDocumentRow.label).like(pattern),
                        func.lower(SearchDocumentRow.description).like(pattern),
                        func.lower(SearchDocumentRow.content).like(pattern),
                    )
                )
            candidates = list(session.scalars(statement.limit(2000)))
            project_names = {
                row.id: _text(row.payload.get("name"), 500)
                for row in session.scalars(
                    select(EntityRow).where(EntityRow.kind == "engagements")
                )
            }

        def score(row: SearchDocumentRow) -> float:
            label = row.label.casefold()
            exact = 1000 if query.casefold() == label else 0
            prefix = 300 if label.startswith(query.casefold()) else 0
            active = 50 if active_project and row.project_id == active_project else 0
            return float(
                exact
                + prefix
                + active
                + sum(40 if term in label else 10 for term in terms)
            )

        candidates.sort(
            key=lambda row: (-score(row), -row.updated_at.timestamp(), row.id)
        )
        page = candidates[offset : offset + limit]
        results: list[SearchResult] = []
        for row in page:
            ref = ResourceRef(
                project_id=row.project_id,
                kind=ResourceKind(row.resource_kind),
                id=row.resource_id,
                revision=row.revision,
            )
            actions = self.actions.resolve(ActionResolutionRequest(resources=[ref]))
            results.append(
                SearchResult(
                    ref=ref,
                    project=project_names.get(
                        row.project_id or "",
                        "Library" if row.project_id is None else row.project_id,
                    ),
                    label=row.label,
                    description=row.description,
                    snippet=row.content[:300],
                    breadcrumb=row.breadcrumb,
                    updated_at=row.updated_at,
                    score=score(row),
                    actions=actions,
                )
            )
        next_cursor = None
        if offset + limit < len(candidates):
            next_cursor = (
                base64.urlsafe_b64encode(str(offset + limit).encode())
                .decode()
                .rstrip("=")
            )
        return SearchResponse(
            items=results, next_cursor=next_cursor, partial_index=repaired
        )
