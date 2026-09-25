"""Generic ``/api/v1/<kind>`` routes exist only for kinds classified to have them.

``create_app`` derives list/get/create/replace/patch/delete routes from
``ENTITY_MODELS``. A lifecycle, ledger or evidence kind reached through them
skips the service that owns it, so every kind must be classified explicitly
and the classification must match the route table.
"""

from datetime import timedelta
import hashlib

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from nebula.v3.api import (
    API_PREFIX,
    APPEND_ONLY_RESOURCES,
    CUSTOM_RESOURCES,
    GENERIC_CRUD_RESOURCES,
    READ_ONLY_RESOURCES,
    create_app,
)
from nebula.v3.chat_goals import ChatGoalService, GoalCreate
from nebula.v3.domain import (
    ENTITY_MODEL_BY_KIND,
    ChatGoal,
    ChatGoalStatus,
    ChatSession,
    Engagement,
    PairedDeviceSession,
    ProviderProfile,
    WorkspaceProvenanceObservation,
    utc_now,
)
from nebula.v3.storage import NebulaStore

TOKEN = "generic-crud-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
GENERIC_ENDPOINT = "_register_crud_routes.<locals>."

# Kinds a Core service advances, charges, attests or grants. They must never
# be writable through generic routes, whatever else changes in the sets.
SERVICE_OWNED_KINDS = (
    "chat_goals",
    "chat_goal_usage_charges",
    "chat_subagents",
    "chat_subagent_messages",
    "chat_agent_messages",
    "chat_schedules",
    "chat_turns",
    "chat_queues",
    "chat_decisions",
    # Write-once values a turn or goal resumes from.
    "chat_snapshot_parts",
    "native_checkpoints",
    "native_hook_executions",
    "workspace_provenance_observations",
    "paired_device_sessions",
    "action_intents",
    "handoff_envelopes",
    "harness_sessions",
    "harness_turns",
    "harness_interactions",
    "browser_automation_leases",
    "browser_commands",
    "browser_proxy_rules",
    "scope_imports",
    "runs",
    "tasks",
    "agent_attempts",
    "tool_calls",
    "approvals",
    "command_executions",
    "evidence",
)


@pytest.fixture(scope="module")
def generic_methods(tmp_path_factory) -> dict[str, set[str]]:
    """Map each entity kind to the methods its generic routes serve."""

    database = tmp_path_factory.mktemp("routes") / "routes.db"
    served: dict[str, set[str]] = {}
    for route in create_app(NebulaStore(database)).routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.endpoint.__qualname__.startswith(GENERIC_ENDPOINT):
            continue
        resource = route.path.removeprefix(f"{API_PREFIX}/").removesuffix(
            "/{entity_id}"
        )
        served.setdefault(resource.replace("-", "_"), set()).update(route.methods)
    return served


@pytest.fixture
def api(tmp_path):
    store = NebulaStore(tmp_path / "crud.db")
    store.create(Engagement(id="project", name="Project"))
    return TestClient(create_app(store, auth_token=TOKEN)), store


def test_every_entity_kind_is_classified_exactly_once():
    kinds = set(ENTITY_MODEL_BY_KIND)
    classes = {
        "GENERIC_CRUD_RESOURCES": set(GENERIC_CRUD_RESOURCES),
        "READ_ONLY_RESOURCES": set(READ_ONLY_RESOURCES),
        "CUSTOM_RESOURCES": set(CUSTOM_RESOURCES),
    }
    unclassified = kinds.difference(*classes.values())
    assert not unclassified, (
        f"classify {sorted(unclassified)} in nebula.v3.api: CUSTOM_RESOURCES when "
        "dedicated routes own the kind, READ_ONLY_RESOURCES when generic reads "
        "are all clients need, or GENERIC_CRUD_RESOURCES with the reason clients "
        "may write it directly"
    )
    for name, members in classes.items():
        assert members <= kinds, f"{name} names unknown kinds {members - kinds}"
    names = list(classes)
    for index, first in enumerate(names):
        for second in names[index + 1 :]:
            shared = classes[first] & classes[second]
            assert not shared, f"{sorted(shared)} is in both {first} and {second}"
    assert APPEND_ONLY_RESOURCES <= set(GENERIC_CRUD_RESOURCES)
    assert all(reason.strip() for reason in GENERIC_CRUD_RESOURCES.values())


def test_generic_routes_follow_the_classification(generic_methods):
    served = generic_methods
    writable = {kind for kind, methods in served.items() if methods & WRITE_METHODS}
    assert writable == set(GENERIC_CRUD_RESOURCES)
    read_only = {kind for kind, methods in served.items() if methods == {"GET"}}
    assert read_only == READ_ONLY_RESOURCES
    assert not set(served) & CUSTOM_RESOURCES


@pytest.mark.parametrize("kind", SERVICE_OWNED_KINDS)
def test_service_owned_kinds_have_no_generic_write_route(generic_methods, kind):
    assert kind in ENTITY_MODEL_BY_KIND
    assert kind not in GENERIC_CRUD_RESOURCES
    assert not generic_methods.get(kind, set()) & WRITE_METHODS


def test_goal_state_cannot_be_written_around_the_goal_service(api):
    client, store = api
    store.create(
        ProviderProfile(id="provider", name="Provider", provider_type="openrouter")
    )
    store.create(
        ChatSession(
            id="session",
            engagement_id="project",
            title="Goal chat",
            provider_profile_id="provider",
            model="model",
        )
    )
    goal = ChatGoalService(store).create(
        "session", GoalCreate(objective="Ship it", completion_criteria=["Shipped"])
    )
    forged = {"status": "running", "started_at": utc_now().isoformat()}

    responses = [
        client.post(
            "/api/v1/chat-goals",
            headers=HEADERS,
            json={
                "engagement_id": "project",
                "session_id": "session",
                "objective": "Forged",
                "completion_criteria": ["Forged"],
                **forged,
            },
        ),
        client.patch(
            f"/api/v1/chat-goals/{goal.id}",
            headers=HEADERS,
            json={"changes": forged, "expected_revision": goal.revision},
        ),
        client.put(
            f"/api/v1/chat-goals/{goal.id}",
            headers=HEADERS,
            json={**goal.model_dump(mode="json"), **forged},
        ),
        client.delete(f"/api/v1/chat-goals/{goal.id}", headers=HEADERS),
    ]

    assert [response.status_code for response in responses] == [404] * 4
    stored = store.get(ChatGoal, goal.id)
    assert (stored.status, stored.revision) == (ChatGoalStatus.DRAFT, goal.revision)
    assert len(store.list_entities(ChatGoal, engagement_id="project")) == 1
    # The goal stays reachable through the routes that own its lifecycle.
    owned = client.get("/api/v1/chat/sessions/session/goal", headers=HEADERS)
    assert owned.status_code == 200, owned.text
    assert owned.json()["id"] == goal.id


def test_workspace_provenance_receipts_cannot_be_forged_or_edited(api):
    client, store = api
    receipt = store.create(
        WorkspaceProvenanceObservation(
            engagement_id="project",
            workspace_root="/workspace",
            scope_kind="turn",
            scope_id="turn-1",
            actor_id="chat:session",
        )
    )
    forged_attribution = {"owned": [{"path": "secret.txt"}]}

    responses = [
        client.post(
            "/api/v1/workspace-provenance-observations",
            headers=HEADERS,
            json={
                "engagement_id": "project",
                "workspace_root": "/workspace",
                "scope_kind": "turn",
                "scope_id": "turn-2",
                "actor_id": "chat:forged",
                "attribution": forged_attribution,
            },
        ),
        client.patch(
            f"/api/v1/workspace-provenance-observations/{receipt.id}",
            headers=HEADERS,
            json={"changes": {"attribution": forged_attribution}},
        ),
        client.put(
            f"/api/v1/workspace-provenance-observations/{receipt.id}",
            headers=HEADERS,
            json={
                **receipt.model_dump(mode="json"),
                "attribution": forged_attribution,
            },
        ),
        client.delete(
            f"/api/v1/workspace-provenance-observations/{receipt.id}",
            headers=HEADERS,
        ),
    ]

    assert [response.status_code for response in responses] == [404] * 4
    stored = store.get(WorkspaceProvenanceObservation, receipt.id)
    assert (stored.attribution, stored.revision) == ({}, receipt.revision)
    assert [
        item.id
        for item in store.list_entities(
            WorkspaceProvenanceObservation, engagement_id="project"
        )
    ] == [receipt.id]


def test_device_sessions_cannot_be_minted_or_restored_generically(api):
    client, store = api
    now = utc_now()
    revoked = store.create(
        PairedDeviceSession(
            name="Lost phone",
            token_sha256=hashlib.sha256(b"lost-token").hexdigest(),
            csrf_sha256=hashlib.sha256(b"lost-csrf").hexdigest(),
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=30),
            revoked_at=now,
        )
    )
    far_future = (now + timedelta(days=3650)).isoformat()

    minted = client.post(
        "/api/v1/paired-device-sessions",
        headers=HEADERS,
        json={
            "name": "Minted",
            "token_sha256": hashlib.sha256(b"chosen-token").hexdigest(),
            "csrf_sha256": hashlib.sha256(b"chosen-csrf").hexdigest(),
            "idle_expires_at": far_future,
            "absolute_expires_at": far_future,
        },
    )
    restored = client.patch(
        f"/api/v1/paired-device-sessions/{revoked.id}",
        headers=HEADERS,
        json={"changes": {"revoked_at": None, "absolute_expires_at": far_future}},
    )
    hashes = client.get("/api/v1/paired-device-sessions", headers=HEADERS)

    assert (minted.status_code, restored.status_code, hashes.status_code) == (
        404,
        404,
        404,
    )
    assert store.get(PairedDeviceSession, revoked.id).revoked_at is not None
    assert [item.id for item in store.list_entities(PairedDeviceSession)] == [
        revoked.id
    ]
    # Pairing and revocation stay on the authentication routes.
    devices = client.get("/api/v1/auth/devices", headers=HEADERS)
    assert devices.status_code == 200, devices.text
