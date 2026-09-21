"""Stored history keeps what the operator attached, in a form the model accepts.

Two kinds of content ride on a stored user message besides its text: image
blocks and operator-selected context (``metadata.context_attachments``). Both
must survive into later turns, compaction and context-length recovery, and a
text-only model must receive stored images as text rather than image parts.
"""

import asyncio
import base64
import hashlib

import pytest

import nebula.v3.chat as chat_module
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.chat import (
    ChatCompletionRequest,
    ChatConfigurationError,
    ChatRuntimeSwitchPreflightRequest,
    ChatService,
)
from nebula.v3.context import estimate_model_request, resolve_context_limits
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ContextSnapshot,
    Engagement,
    ProviderProfile,
)
from nebula.v3.providers import ModelCapabilities, ModelProvider
from nebula.v3.storage import NebulaStore
from tests.v3.test_chat import ContextRejectingProvider, FakeProvider, _profile

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_SNIPPET = "def check(token):\n    return token == ADMIN_TOKEN  # timing-unsafe compare"


def _attachment(text: str = _SNIPPET) -> dict[str, object]:
    return {
        "source_kind": "editor_selection",
        "source_label": "auth.py:10-11",
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _provider_profile(profile_id: str, *, vision: bool) -> ProviderProfile:
    payload = _profile(local=True).model_dump(mode="python")
    payload["id"] = profile_id
    payload["capabilities"] = {"streaming": True, "vision": vision}
    return ProviderProfile.model_validate(payload)


def _provider_factory(profile: ProviderProfile) -> ModelProvider:
    provider = FakeProvider(profile.id, local=True)
    provider.config = provider.config.model_copy(
        update={
            "capabilities": ModelCapabilities(
                streaming=True, vision=profile.capabilities.vision
            )
        }
    )
    return provider


def _image_block(
    store: NebulaStore,
    artifacts: ArtifactStore,
    engagement_id: str,
    name: str,
    salt: int = 0,
) -> dict[str, object]:
    original = store.create(
        artifacts.put_bytes(
            _PNG + bytes([salt]),
            engagement_id=engagement_id,
            filename=name,
            media_type="image/png",
            source="chat-upload",
            metadata={"sensitive": True, "chat_image_original": True},
        )
    )
    preview = store.create(
        artifacts.put_bytes(
            _PNG + bytes([salt]),
            engagement_id=engagement_id,
            filename=f"preview-{name}",
            media_type="image/png",
            source="chat-image-preview",
            parent_artifact_id=original.id,
            metadata={"chat_image_preview": True, "metadata_stripped": True},
        )
    )
    return {
        "type": "image",
        "artifact_id": original.id,
        "media_type": "image/png",
        "alt": name,
        "metadata": {"preview_artifact_id": preview.id},
    }


def _image_session(
    store: NebulaStore, artifacts: ArtifactStore, profile_id: str, images: int
) -> tuple[Engagement, ChatSession]:
    engagement = store.create(Engagement(id="eng-images", name="Images"))
    session = store.create(
        ChatSession(
            id="session-images",
            engagement_id=engagement.id,
            title="Screenshots",
            provider_profile_id=profile_id,
            model="model-a",
            metadata={"last_sequence": images * 2, "message_count": images * 2},
        )
    )
    sequence = 0
    for index in range(images):
        block = _image_block(
            store, artifacts, engagement.id, f"shot-{index}.png", index
        )
        sequence += 1
        store.create(
            ChatMessage(
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.USER,
                content=f"screenshot {index}",
                content_blocks=[block],
            )
        )
        sequence += 1
        store.create(
            ChatMessage(
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=sequence,
                role=ChatRole.ASSISTANT,
                content=f"noted {index}",
            )
        )
    return engagement, session


def _placeholder(name: str) -> str:
    return f"[image: {name} omitted: this model does not accept images]"


def test_text_only_switch_replaces_history_images_with_placeholders(tmp_path):
    store = NebulaStore(tmp_path / "switch-images.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    engagement = store.create(Engagement(id="eng-switch", name="Switch"))
    store.create(_provider_profile("vision", vision=True))
    store.create(_provider_profile("text-only", vision=False))
    service = ChatService(
        store, provider_factory=_provider_factory, artifact_store=artifacts
    )
    block = _image_block(store, artifacts, engagement.id, "login-page.png")

    first = service.prepare(
        ChatCompletionRequest(
            provider_id="vision",
            engagement_id=engagement.id,
            include_knowledge=False,
            messages=[
                {
                    "role": "user",
                    "content": "What is in this screenshot?",
                    "content_blocks": [block],
                }
            ],
        )
    )
    sent = first.model_request.messages[0].content
    assert isinstance(sent, list)
    assert [part["type"] for part in sent] == ["text", "image"]
    asyncio.run(service.complete(first))
    session = store.get(ChatSession, first.session.id)

    preflight = service.runtime_switch_preflight(
        session.id,
        ChatRuntimeSwitchPreflightRequest(
            provider_id="text-only",
            model="model-a",
            tools_enabled=False,
            expected_session_revision=session.revision,
        ),
    )
    assert preflight.compatible is True

    # A client that replays the whole transcript, stored image included.
    stored = service.session_messages(session.id)
    replayed = service.prepare(
        ChatCompletionRequest(
            provider_id="text-only",
            engagement_id=engagement.id,
            session_id=session.id,
            include_knowledge=False,
            messages=[
                {
                    "role": "user",
                    "content": stored[0].content,
                    "content_blocks": [
                        item.model_dump(mode="json")
                        for item in stored[0].content_blocks
                    ],
                },
                {"role": "assistant", "content": stored[1].content},
                {"role": "user", "content": "Summarise what the page showed."},
            ],
        )
    )
    assert [message.content for message in replayed.model_request.messages] == [
        "What is in this screenshot?\n\n" + _placeholder("login-page.png"),
        stored[1].content,
        "Summarise what the page showed.",
    ]
    asyncio.run(service.complete(replayed))

    # The web client sends only the new message for an existing conversation.
    follow_up = service.prepare(
        ChatCompletionRequest(
            provider_id="text-only",
            engagement_id=engagement.id,
            session_id=session.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": "Now summarise our findings."}],
        )
    )
    messages = follow_up.model_request.messages
    assert all(isinstance(message.content, str) for message in messages)
    assert messages[0].content == (
        "What is in this screenshot?\n\n" + _placeholder("login-page.png")
    )
    assert messages[-1].content == "Now summarise our findings."
    # The stored transcript keeps the image itself for a later vision model.
    assert service.session_messages(session.id)[0].content_blocks[0].type == "image"

    # A new image sent to the text-only model is still refused.
    with pytest.raises(ChatConfigurationError, match="not verified for vision input"):
        service.prepare(
            ChatCompletionRequest(
                provider_id="text-only",
                engagement_id=engagement.id,
                session_id=session.id,
                include_knowledge=False,
                messages=[
                    {
                        "role": "user",
                        "content": "And this one?",
                        "content_blocks": [
                            _image_block(
                                store, artifacts, engagement.id, "second.png", 9
                            )
                        ],
                    }
                ],
            )
        )


def test_image_history_counts_toward_compaction(tmp_path):
    store = NebulaStore(tmp_path / "image-budget.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    profile = store.create(_provider_profile("vision", vision=True))
    engagement, session = _image_session(store, artifacts, profile.id, images=3)
    service = ChatService(
        store, provider_factory=_provider_factory, artifact_store=artifacts
    )

    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            session_id=session.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": "Compare the three screenshots."}],
        )
    )

    assert prepared.context_snapshot is not None
    assert store.list_entities(ContextSnapshot, limit=10)
    limits = resolve_context_limits(profile, model="model-a")
    assert estimate_model_request(prepared.model_request) <= limits.input_capacity
    assert prepared.model_request.messages[-1].content == (
        "Compare the three screenshots."
    )


def test_switch_preflight_estimates_history_images_for_the_target_model(tmp_path):
    store = NebulaStore(tmp_path / "switch-estimate.db")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    store.create(_provider_profile("vision", vision=True))
    store.create(_provider_profile("vision-b", vision=True))
    store.create(_provider_profile("text-only", vision=False))
    _, session = _image_session(store, artifacts, "vision", images=3)
    service = ChatService(
        store, provider_factory=_provider_factory, artifact_store=artifacts
    )

    def preflight(target: str):
        return service.runtime_switch_preflight(
            session.id,
            ChatRuntimeSwitchPreflightRequest(
                provider_id=target,
                model="model-a",
                tools_enabled=False,
                expected_session_revision=session.revision,
            ),
        )

    to_vision = preflight("vision-b")
    to_text = preflight("text-only")

    # Three images are sent to a vision model at the same per-image reserve the
    # request capacity check charges, so the switch must offer compaction.
    assert to_vision.compatible is True
    assert to_vision.estimated_active_input_tokens >= 3 * 2_048
    assert to_vision.requires_compaction_confirmation is True
    # A text-only model receives short placeholders instead.
    assert to_text.compatible is True
    assert to_text.estimated_active_input_tokens < 2_048
    assert to_text.requires_compaction_confirmation is False


def test_selected_context_survives_followups(tmp_path):
    store = NebulaStore(tmp_path / "selected-followup.db")
    engagement = store.create(Engagement(id="eng-selected", name="Selected"))
    profile = store.create(_profile(local=True))
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)

    first = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": "Is this comparison safe?"}],
            context_attachments=[_attachment()],
        )
    )
    assert "BEGIN SELECTED CONTEXT" in str(first.model_request.messages[-1].content)
    asyncio.run(service.complete(first))
    session_id = first.session.id
    stored = service.session_messages(session_id)
    # The durable transcript keeps the operator's words; the attachment stays
    # in metadata.
    assert stored[0].content == "Is this comparison safe?"

    second = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            session_id=session_id,
            include_knowledge=False,
            messages=[
                {"role": "user", "content": "Rewrite it with a constant-time compare."}
            ],
        )
    )
    history = str(second.model_request.messages[0].content)
    assert history.startswith("Is this comparison safe?\n\nBEGIN SELECTED CONTEXT")
    assert "ADMIN_TOKEN" in history
    assert second.model_request.messages[-1].content == (
        "Rewrite it with a constant-time compare."
    )

    # A client replaying the transcript sends the operator's words, not the
    # envelope, and still matches the durable history.
    replayed = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            session_id=session_id,
            include_knowledge=False,
            messages=[
                {"role": "user", "content": stored[0].content},
                {"role": "assistant", "content": stored[1].content},
                {"role": "user", "content": "Rewrite it with hmac.compare_digest."},
            ],
        )
    )
    assert "ADMIN_TOKEN" in str(replayed.model_request.messages[0].content)
    assert [message.content for message in replayed.new_messages] == [
        "Rewrite it with hmac.compare_digest."
    ]


def test_selected_context_survives_context_length_recovery(tmp_path):
    store = NebulaStore(tmp_path / "selected-recovery.db")
    engagement = store.create(Engagement(id="eng-recovery", name="Recovery"))
    payload = _profile(local=False, permits_sensitive_data=True).model_dump(
        mode="python"
    )
    payload["provider_type"] = "openrouter"
    payload["model_allowlist"] = ["author/model-a"]
    payload["metadata"] = {
        "default_model": "author/model-a",
        "route_catalog_revision": "wide-routes",
        "model_descriptors": [
            {
                "id": "author/model-a",
                "context_window": 20_000,
                "max_output_tokens": 2_000,
                "route_limits_verified": True,
                "route_limits": [
                    {
                        "provider_name": "wide",
                        "context_window": 20_000,
                        "max_input_tokens": 18_000,
                        "max_output_tokens": 2_000,
                        "supported_parameters": [],
                    }
                ],
            }
        ],
    }
    profile = store.create(ProviderProfile.model_validate(payload))
    provider = ContextRejectingProvider(profile.id)
    provider.config.default_model = "author/model-a"
    provider.config.model_allowlist = ["author/model-a"]
    service = ChatService(store, provider_factory=lambda _: provider)

    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            model="author/model-a",
            allow_cloud_knowledge=True,
            include_knowledge=False,
            stream=True,
            messages=[{"role": "user", "content": "Is this comparison safe?"}],
            context_attachments=[_attachment()],
        )
    )

    async def collect_stream():
        return [item async for item in service.stream(prepared)]

    assert asyncio.run(collect_stream())[-1][0] == "done"
    assert provider.normal_attempts == 2
    retried = next(
        request
        for request in provider.requests
        if request.metadata.get("context_length_recovery") == "1"
    )
    last = str(retried.messages[-1].content)
    assert last.startswith("Is this comparison safe?\n\nBEGIN SELECTED CONTEXT")
    assert "ADMIN_TOKEN" in last


def test_compaction_summarises_selected_context_of_archived_messages(tmp_path):
    store = NebulaStore(tmp_path / "selected-compaction.db")
    engagement = store.create(Engagement(id="eng-compact", name="Compact"))
    profile = store.create(_profile(local=True))
    session = store.create(
        ChatSession(
            id="session-compact",
            engagement_id=engagement.id,
            title="Long review",
            provider_profile_id=profile.id,
            model="model-a",
        )
    )
    snippet = "SESSION_SECRET = 'rotate-me'  # hard-coded in settings.py"
    store.create_many(
        [
            ChatMessage(
                engagement_id=engagement.id,
                session_id=session.id,
                sequence=index + 1,
                role=ChatRole.USER if index % 2 == 0 else ChatRole.ASSISTANT,
                content=f"review-{index} " + "finding " * 300,
                metadata=(
                    {"context_attachments": [_attachment(snippet)]}
                    if index == 0
                    else {}
                ),
            )
            for index in range(12)
        ]
    )
    provider = FakeProvider(profile.id, local=True)
    service = ChatService(store, provider_factory=lambda _: provider)

    prepared = service.prepare(
        ChatCompletionRequest(
            provider_id=profile.id,
            engagement_id=engagement.id,
            session_id=session.id,
            include_knowledge=False,
            messages=[{"role": "user", "content": "What else should we rotate?"}],
        )
    )

    assert prepared.context_snapshot is not None
    compaction = [
        request
        for request in provider.requests
        if request.metadata.get("operation") == "context_compaction"
    ]
    assert compaction
    assert any(
        "rotate-me" in str(request.messages) + (request.instructions or "")
        for request in compaction
    )


@pytest.fixture(autouse=True)
def _no_real_provider(monkeypatch):
    def refuse(_profile: ProviderProfile) -> ModelProvider:
        raise AssertionError("tests pass their own provider factory")

    monkeypatch.setattr(chat_module, "provider_from_profile", refuse)
