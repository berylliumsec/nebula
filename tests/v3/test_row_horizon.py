"""Lookups keep finding new records after a kind passes one 1,000-row page.

``NebulaStore.list_entities`` returns at most 1,000 rows, oldest first. Code
that read one page and filtered it in Python silently stopped seeing newer
rows once a Project held that many records of the kind. The guard below keeps
new code from reintroducing that pattern; the other tests pin the store
queries that replaced it.
"""

import ast
import base64
from datetime import timedelta
from pathlib import Path

import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import ChatRequestMessage, resolve_chat_model_content
from nebula.v3.database import Database
from nebula.v3.domain import (
    Artifact,
    BrowserAutomationLease,
    BrowserAutomationLeaseStatus,
    BrowserAttackResult,
    ChatContentBlock,
    ChatRole,
    Engagement,
    RiskClass,
    utc_now,
)
from nebula.v3.storage import NebulaStore
from tests.v3.row_horizon_fixture import PAGE, seed_older_copies

SOURCE = Path(__file__).resolve().parents[2] / "src" / "nebula" / "v3"

# Kinds an operator adds one at a time in Settings. Core never creates them
# per turn, run, request or capture, so one page holds every row.
BOUNDED_KINDS = {
    "McpServerProfile": "MCP servers the operator added or imported",
    "ProviderProfile": "model providers the operator configured",
    "RunnerProfile": "container runtimes the operator configured",
    "StoredRunnerProfile": "container runtimes the operator configured",
    "SshEnvironment": "SSH hosts the operator saved settings for",
    "VpnProfile": "OpenVPN profiles the operator uploaded",
}

# Temporary: other open pull requests replace the one-page lookups in these
# modules. Remove each entry once its branch merges.
PENDING_MODULES = {
    "harnesses.py": "owned by claude/harness-lifecycle",
    "missions.py": "owned by claude/mission-recovery",
}

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _oldest_page_lookups(source: str) -> list[tuple[int, str]]:
    """Return ``(line, model)`` for each call that reads one oldest-first page.

    A call is one page when it asks ``list_entities`` or ``find_entities`` for
    the 1,000-row maximum without an ``offset`` (so nothing pages past it) and
    without ``newest_first=True``.
    """

    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"list_entities", "find_entities"}
        ):
            continue
        keywords = {item.arg: item.value for item in node.keywords if item.arg}
        limit = keywords.get("limit")
        if not (isinstance(limit, ast.Constant) and limit.value == PAGE):
            continue
        if "offset" in keywords:
            continue
        newest = keywords.get("newest_first")
        if newest is not None and not (
            isinstance(newest, ast.Constant) and newest.value is False
        ):
            continue
        model = ast.unparse(node.args[0]) if node.args else "?"
        found.append((node.lineno, model))
    return found


def test_guard_recognises_a_one_page_lookup():
    source = """
store.list_entities(Artifact, engagement_id=project, limit=1_000)
store.find_entities(Artifact, {}, limit=1000, newest_first=False)
store.list_entities(Artifact, limit=1_000, offset=offset)
store.list_entities(Artifact, limit=1_000, newest_first=True)
store.find_entities(Artifact, {}, limit=1_000, newest_first=True)
store.list_entities(Artifact, limit=1)
store.find_entities(Artifact, {"id": "a"})
"""
    assert _oldest_page_lookups(source) == [(2, "Artifact"), (3, "Artifact")]


def test_no_lookup_reads_only_the_oldest_page_of_a_growing_kind():
    offenders: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(SOURCE).as_posix()
        if relative in PENDING_MODULES:
            continue
        for line, model in _oldest_page_lookups(path.read_text(encoding="utf-8")):
            if model not in BOUNDED_KINDS:
                offenders.append(f"{relative}:{line} {model}")
    assert offenders == [], (
        "These calls read only the oldest 1,000 rows, so they stop seeing new "
        "records once the kind grows past one page. Filter in SQL with "
        "find_entities, page with offset, or show the newest window with "
        "list_latest_entities. A kind the operator configures one at a time "
        "belongs in BOUNDED_KINDS with its bound."
    )


@pytest.fixture
def store(tmp_path):
    return NebulaStore(Database(tmp_path / "nebula.db"))


def test_find_entities_reaches_a_row_past_the_first_page_by_indexed_and_integer_filters(
    store,
):
    project = store.create(Engagement(name="Browser automation"))
    lease = BrowserAutomationLease(
        engagement_id=project.id,
        run_id="run-1",
        session_id="session-1",
        identity_id="identity-1",
        scope_policy_id="scope-1",
        scope_policy_revision=1,
        target_urls=["https://app.example.test/"],
        allowed_risk_classes=[RiskClass.PASSIVE],
        expires_at=utc_now() + timedelta(minutes=5),
        status=BrowserAutomationLeaseStatus.EXPIRED,
    )
    seed_older_copies(store, lease)
    active = store.create(
        lease.model_copy(
            update={"id": "lease-newest", "status": BrowserAutomationLeaseStatus.ACTIVE}
        )
    )
    other_run = store.create(
        lease.model_copy(
            update={
                "id": "lease-other-run",
                "run_id": "run-2",
                "status": BrowserAutomationLeaseStatus.ACTIVE,
            }
        )
    )

    assert "lease-newest" not in {
        item.id
        for item in store.list_entities(
            BrowserAutomationLease, engagement_id=project.id, limit=PAGE
        )
    }
    assert [
        item.id
        for item in store.find_entities(
            BrowserAutomationLease,
            {},
            engagement_id=project.id,
            automation_run_id="run-1",
            automation_status="active",
        )
    ] == [active.id]
    assert {
        item.id
        for item in store.find_entities(
            BrowserAutomationLease,
            {},
            automation_session_id="session-1",
            automation_status=["active", "paused"],
        )
    } == {active.id, other_run.id}
    assert (
        store.find_entities(
            BrowserAutomationLease, {}, engagement_id=project.id, automation_status=[]
        )
        == []
    )

    result = BrowserAttackResult(
        engagement_id=project.id, attack_id="attack-1", sequence=0
    )
    seed_older_copies(store, result, vary=lambda index: {"sequence": index})
    newest = store.create(
        result.model_copy(update={"id": "result-newest", "sequence": PAGE})
    )
    assert store.find_entities(
        BrowserAttackResult, {"attack_id": "attack-1", "sequence": PAGE}
    ) == [newest]
    assert [
        item.sequence
        for item in store.find_entities(
            BrowserAttackResult, {"attack_id": "attack-1", "sequence": 7}
        )
    ] == [7]
    with pytest.raises(TypeError, match="boolean"):
        store.find_entities(BrowserAttackResult, {"blocked": True})


def test_list_latest_entities_windows_from_the_newest_end_in_chronological_order(
    store,
):
    project = store.create(Engagement(name="Traffic"))
    template = Artifact(
        engagement_id=project.id,
        filename="capture.bin",
        media_type="application/octet-stream",
        sha256="0" * 64,
        size=1,
        storage_path="00/capture.bin",
    )
    seed_older_copies(store, template)
    base = utc_now()
    newest = [
        store.create(
            template.model_copy(
                update={
                    "id": f"artifact-new-{index}",
                    "created_at": base + timedelta(seconds=index),
                    "updated_at": base + timedelta(seconds=index),
                }
            )
        )
        for index in range(3)
    ]

    window = store.list_latest_entities(Artifact, engagement_id=project.id)
    assert len(window) == PAGE
    assert [item.id for item in window[-3:]] == [item.id for item in newest]
    assert [item.created_at for item in window] == sorted(
        item.created_at for item in window
    )
    assert [
        item.id
        for item in store.list_latest_entities(
            Artifact, {"filename": "capture.bin"}, engagement_id=project.id, limit=2
        )
    ] == ["artifact-new-1", "artifact-new-2"]
    with pytest.raises(ValueError):
        store.list_latest_entities(Artifact, limit=PAGE + 1)


def test_chat_image_preview_is_found_after_the_project_holds_a_page_of_artifacts(
    store, tmp_path
):
    project = store.create(Engagement(name="Screenshots"))
    artifacts = ArtifactStore(tmp_path / "artifacts")
    original = store.create(
        artifacts.put_bytes(
            _PNG,
            engagement_id=project.id,
            filename="shot.png",
            media_type="image/png",
            source="chat-upload",
            metadata={"sensitive": True, "chat_image_original": True},
        )
    )
    # A long-lived Project already holds a page of other artifacts, all older
    # than the screenshot the operator attaches now.
    seed_older_copies(
        store,
        original.model_copy(update={"metadata": {}}),
        vary=lambda index: {"filename": f"evidence-{index}.png"},
    )
    preview = store.create(
        artifacts.put_bytes(
            _PNG,
            engagement_id=project.id,
            filename="preview-shot.png",
            media_type="image/png",
            source="chat-image-preview",
            parent_artifact_id=original.id,
            metadata={"chat_image_preview": True, "metadata_stripped": True},
        )
    )
    # Messages stored before the preview id was recorded on the block resolve
    # the preview by its parent instead.
    message = ChatRequestMessage(
        role=ChatRole.USER,
        content="What does this show?",
        content_blocks=[
            ChatContentBlock(
                type="image",
                artifact_id=original.id,
                media_type="image/png",
                alt="shot",
            )
        ],
    )

    parts = resolve_chat_model_content(store, artifacts, message, project.id)

    assert isinstance(parts, list)
    assert parts[1] == {
        "type": "image",
        "media_type": preview.media_type,
        "data": base64.b64encode(_PNG).decode("ascii"),
        "alt": "shot",
    }
