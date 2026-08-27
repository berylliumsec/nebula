"""Shared resource-action catalog with Core-owned availability resolution."""

from __future__ import annotations

from .database import EntityRow
from .domain import (
    ActionAuthority,
    ActionConfirmationPolicy,
    ActionDescriptor,
    ActionResolutionRequest,
    ActionRisk,
    ResourceKind,
)
from .relations import RESOURCE_ENTITY_KINDS
from .storage import NebulaStore

ALL_RESOURCE_KINDS = list(ResourceKind)
CONTENT_KINDS = [
    ResourceKind.CONVERSATION,
    ResourceKind.NOTE,
    ResourceKind.SOURCE,
    ResourceKind.LIBRARY_ITEM,
    ResourceKind.WORKSPACE_FILE,
    ResourceKind.ASSET,
    ResourceKind.EVIDENCE,
    ResourceKind.FINDING,
    ResourceKind.REPORT,
    ResourceKind.TERMINAL_SESSION,
    ResourceKind.TERMINAL_COMMAND,
    ResourceKind.BROWSER_SESSION,
    ResourceKind.BROWSER_TAB,
    ResourceKind.BROWSER_EXCHANGE,
    ResourceKind.MISSION,
    ResourceKind.EXECUTION,
    ResourceKind.ARTIFACT,
]

ACTION_CATALOG: tuple[ActionDescriptor, ...] = (
    ActionDescriptor(
        id="open",
        accepted_resource_kinds=ALL_RESOURCE_KINDS,
        authority=ActionAuthority.UI,
    ),
    ActionDescriptor(
        id="ask_nebula",
        accepted_resource_kinds=CONTENT_KINDS,
        authority=ActionAuthority.UI,
    ),
    ActionDescriptor(
        id="take_note",
        accepted_resource_kinds=CONTENT_KINDS,
        result_kind=ResourceKind.NOTE,
        authority=ActionAuthority.CORE,
        risk=ActionRisk.MUTATING,
        confirmation_policy=ActionConfirmationPolicy.MUTATION,
    ),
    ActionDescriptor(
        id="preserve_as_evidence",
        accepted_resource_kinds=[
            ResourceKind.SOURCE,
            ResourceKind.WORKSPACE_FILE,
            ResourceKind.TERMINAL_COMMAND,
            ResourceKind.BROWSER_EXCHANGE,
            ResourceKind.ARTIFACT,
        ],
        result_kind=ResourceKind.EVIDENCE,
        authority=ActionAuthority.CORE,
        risk=ActionRisk.MUTATING,
        confirmation_policy=ActionConfirmationPolicy.MUTATION,
    ),
    ActionDescriptor(
        id="draft_finding",
        accepted_resource_kinds=[ResourceKind.EVIDENCE, ResourceKind.NOTE],
        result_kind=ResourceKind.FINDING,
        authority=ActionAuthority.CORE,
        risk=ActionRisk.MUTATING,
        confirmation_policy=ActionConfirmationPolicy.MUTATION,
    ),
    ActionDescriptor(
        id="add_to_report",
        accepted_resource_kinds=[ResourceKind.FINDING, ResourceKind.NOTE],
        result_kind=ResourceKind.REPORT,
        authority=ActionAuthority.CORE,
        risk=ActionRisk.MUTATING,
        confirmation_policy=ActionConfirmationPolicy.MUTATION,
    ),
    ActionDescriptor(
        id="open_source",
        accepted_resource_kinds=[ResourceKind.EVIDENCE, ResourceKind.FINDING],
        authority=ActionAuthority.UI,
    ),
    ActionDescriptor(
        id="download",
        accepted_resource_kinds=[
            ResourceKind.SOURCE,
            ResourceKind.LIBRARY_ITEM,
            ResourceKind.WORKSPACE_FILE,
            ResourceKind.EVIDENCE,
            ResourceKind.REPORT,
            ResourceKind.ARTIFACT,
        ],
        authority=ActionAuthority.CORE,
    ),
    ActionDescriptor(
        id="copy",
        accepted_resource_kinds=CONTENT_KINDS,
        authority=ActionAuthority.DEVICE,
        required_capabilities=["clipboard.write"],
    ),
    ActionDescriptor(
        id="reveal",
        accepted_resource_kinds=[ResourceKind.WORKSPACE_FILE, ResourceKind.ARTIFACT],
        authority=ActionAuthority.DEVICE,
        required_capabilities=["filesystem.reveal"],
    ),
    ActionDescriptor(
        id="navigate",
        accepted_resource_kinds=[
            ResourceKind.SOURCE,
            ResourceKind.BROWSER_SESSION,
            ResourceKind.BROWSER_TAB,
            ResourceKind.BROWSER_EXCHANGE,
        ],
        authority=ActionAuthority.DEVICE,
        required_capabilities=["browser.navigate"],
    ),
    ActionDescriptor(
        id="configure",
        accepted_resource_kinds=[ResourceKind.PROJECT],
        authority=ActionAuthority.UI,
        risk=ActionRisk.MUTATING,
        confirmation_policy=ActionConfirmationPolicy.MUTATION,
    ),
)


class ActionRegistry:
    def __init__(self, store: NebulaStore) -> None:
        self.store = store

    def _resource_error(self, request: ActionResolutionRequest) -> str | None:
        project_ids = {ref.project_id for ref in request.resources if ref.project_id}
        if len(project_ids) > 1:
            return "Selected resources belong to different projects."
        with self.store.database.session() as session:
            for ref in request.resources:
                entity_kind = RESOURCE_ENTITY_KINDS.get(ref.kind)
                if entity_kind is None:
                    continue
                row = session.get(EntityRow, ref.id)
                if row is None or row.kind != entity_kind:
                    return f"The selected {ref.kind.value.replace('_', ' ')} no longer exists."
                actual_project_id = (
                    row.id if ref.kind == ResourceKind.PROJECT else row.engagement_id
                )
                if ref.project_id != actual_project_id:
                    return "The selected resource belongs to a different project."
                if ref.revision is not None and ref.revision != row.revision:
                    return "The selected resource changed. Refresh it before using this action."
        return None

    def resolve(self, request: ActionResolutionRequest) -> list[ActionDescriptor]:
        resource_error = self._resource_error(request)
        kinds = {ref.kind for ref in request.resources}
        available_capabilities = set(request.device_capabilities)
        resolved: list[ActionDescriptor] = []
        for descriptor in ACTION_CATALOG:
            if not kinds.issubset(set(descriptor.accepted_resource_kinds)):
                continue
            disabled_reason = resource_error
            if (
                disabled_reason is None
                and descriptor.authority == ActionAuthority.DEVICE
            ):
                missing = sorted(
                    set(descriptor.required_capabilities) - available_capabilities
                )
                if missing:
                    disabled_reason = (
                        "No connected device currently provides "
                        + ", ".join(missing)
                        + "."
                    )
            resolved.append(
                descriptor.model_copy(
                    update={
                        "available": disabled_reason is None,
                        "disabled_reason": disabled_reason,
                    }
                )
            )
        return resolved
