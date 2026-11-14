"""Existence probes and Butler search inside a guarded execution worker."""

import pytest

from nebula.v3.action_registry import ActionRegistry
from nebula.v3.database import SearchDocumentRow
from nebula.v3.domain import (
    ChatSession,
    CommandExecution,
    Engagement,
    ResourceKind,
    SearchScope,
    utc_now,
)
from nebula.v3.execution_scope import execution_context
from nebula.v3.search import FederatedSearch
from tests.v3.test_execution_scope import (
    anyio_backend as anyio_backend,
    execution as execution,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


async def test_existence_probes_run_in_a_worker_within_its_projects(execution):
    control, runtime, coordinator, lease = execution
    for project in ("mine", "other"):
        await control.create(
            CommandExecution(
                id=f"command-{project}",
                engagement_id=project,
                session_id=f"automation-{project}",
                process_id=f"process-{project}",
                command="true",
                command_sha256="0" * 64,
                runtime_digest="sha256:" + "0" * 64,
                policy_revision=1,
                metadata={"approval_id": f"approval-{project}"},
            )
        )
    await control.create(Engagement(id="empty", name="empty"))
    empty_lease = await coordinator.claim_project("empty", "execution")

    with execution_context(lease):
        # The first use of an approved command checks whether it was consumed.
        assert await runtime.has_entity_with_metadata(
            CommandExecution, "approval_id", "approval-mine"
        )
        assert not await runtime.has_entity_with_metadata(
            CommandExecution, "approval_id", "approval-other"
        )
        # Deleting a non-archived project first asks whether it owns anything.
        assert await runtime.engagement_has_dependents("mine")
        assert not await runtime.engagement_has_dependents(
            "mine", exclude_entity_ids=["turn-mine", "command-mine"]
        )
    with execution_context(empty_lease):
        assert not await runtime.engagement_has_dependents("empty")
    assert await control.has_entity_with_metadata(
        CommandExecution, "approval_id", "approval-other"
    )


async def test_butler_search_hides_private_and_foreign_chats_in_a_worker(execution):
    control, runtime, _, lease = execution
    for chat_id, project, metadata in (
        ("chat-visible", "mine", {}),
        ("chat-butler", "mine", {"control_assistant": True}),
        ("chat-side", "mine", {"side_chat": True}),
        ("chat-foreign", "other", {}),
    ):
        await control.create(
            ChatSession(
                id=chat_id,
                engagement_id=project,
                title=f"Find {chat_id}",
                model="inert",
                provider_profile_id="provider",
                metadata=metadata,
            )
        )

    async def found(store):
        result = await FederatedSearch(store, ActionRegistry(store)).search(
            query="Find",
            active_project="mine",
            scope=SearchScope.ALL,
            kinds=[ResourceKind.CONVERSATION],
            cursor=None,
            limit=50,
            exclude_private_chats=True,
        )
        return sorted(item.ref.id for item in result.items)

    assert await found(control) == ["chat-foreign", "chat-visible"]
    with execution_context(lease):
        assert await found(runtime) == ["chat-visible"]

    # A readable document that names a chat must not reveal whether that chat
    # exists in another project: the joined chat is scoped like the document.
    for target in ("chat-foreign", "chat-missing"):
        async with control.database.session() as session:
            session.add(
                SearchDocumentRow(
                    id="probe",
                    project_id="mine",
                    resource_kind=ResourceKind.CONVERSATION.value,
                    resource_id=target,
                    revision=1,
                    label="Find probe",
                    updated_at=utc_now(),
                )
            )
        with execution_context(lease):
            assert await found(runtime) == ["chat-visible"]
        async with control.database.session() as session:
            await session.delete(await session.get(SearchDocumentRow, "probe"))
