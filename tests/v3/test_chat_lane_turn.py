"""A non-streamed project chat that holds its project's work lane keeps a turn.

Without one, a tool-free stream:false chat called its provider inline while it
held the lane, so every later request in the project waited up to the
provider timeout. With a durable turn the route releases the lane before the
provider is called, as streamed turns do.
"""

import asyncio

import pytest

from nebula.v3.chat import ChatCompletionRequest, ChatService
from nebula.v3.domain import (
    ChatMessage,
    ChatRole,
    ChatSession,
    ChatTurn,
    ChatTurnStatus,
    Engagement,
    ProviderProfile,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ProviderConfig,
    ProviderHealth,
    ProviderKind,
)
from nebula.v3.storage import NebulaStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class WaitingProvider(ModelProvider):
    def __init__(self, config):
        super().__init__(config)
        self.called = asyncio.Event()
        self.answer = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.called.set()
        await self.answer.wait()
        return ModelResponse(
            provider_id=self.config.id,
            model=request.model or "model",
            text="Done",
            usage=ModelUsage(input_tokens=2, output_tokens=2, total_tokens=4),
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.config.id, healthy=True)


@pytest.fixture
async def chat_setup(tmp_path):
    store, provider, chat = await _chat(tmp_path)
    try:
        yield store, provider, chat
    finally:
        provider.answer.set()
        await chat.shutdown()
        await store.event_history.close()
        await store.database.dispose()


async def _chat(tmp_path):
    store = NebulaStore(tmp_path / "lane-turn.db")
    (await store.create(Engagement(id="project", name="Project")))
    profile = await store.create(
        ProviderProfile(
            id="provider",
            name="OpenRouter",
            provider_type="openrouter",
            endpoint="https://openrouter.ai/api/v1",
            model_allowlist=["model"],
            metadata={"default_model": "model"},
        )
    )
    (
        await store.create(
            ChatSession(
                id="session",
                engagement_id="project",
                title="Chat",
                provider_profile_id=profile.id,
                model="model",
            )
        )
    )
    provider = WaitingProvider(
        ProviderConfig(
            id=profile.id,
            kind=ProviderKind.OPENAI_COMPATIBLE,
            base_url="https://openrouter.ai/api/v1",
            default_model="model",
            model_allowlist=["model"],
            capabilities=ModelCapabilities(streaming=True),
        )
    )
    return store, provider, ChatService(store, provider_factory=lambda _: provider)


def _request(*, lane: bool) -> ChatCompletionRequest:
    request = ChatCompletionRequest(
        provider_id="provider",
        engagement_id="project",
        session_id="session",
        model="model",
        messages=[{"role": "user", "content": "Reply."}],
        include_knowledge=False,
        stream=False,
    )
    request._lane_turn = lane
    return request


async def test_a_lane_held_non_streamed_chat_saves_its_turn_before_the_provider(
    chat_setup,
):
    store, provider, chat = chat_setup
    prepared = await chat.prepare_async(_request(lane=True))
    assert prepared.tools_enabled is False
    assert prepared.turn is not None
    saved = await store.get(ChatTurn, prepared.turn.id)
    assert saved.status == ChatTurnStatus.QUEUED

    answering = asyncio.create_task(chat.complete(prepared))
    await asyncio.wait_for(provider.called.wait(), 10)
    # The operator's message is durable while the provider is still working,
    # so a lane released now cannot lose or reorder it.
    messages = await store.list_session_entities(ChatMessage, "session")
    assert [(m.role, m.content) for m in messages] == [(ChatRole.USER, "Reply.")]
    provider.answer.set()
    completion = await asyncio.wait_for(answering, 10)
    assert completion.message.content == "Done"
    assert completion.turn_id == prepared.turn.id
    assert (await store.get(ChatTurn, prepared.turn.id)).status == (
        ChatTurnStatus.COMPLETE
    )
    messages = await store.list_session_entities(ChatMessage, "session")
    assert [m.role for m in messages] == [ChatRole.USER, ChatRole.ASSISTANT]


async def test_an_in_process_non_streamed_chat_stays_turnless(chat_setup):
    _, provider, chat = chat_setup
    prepared = await chat.prepare_async(_request(lane=False))
    assert prepared.turn is None
    provider.answer.set()
    assert (await chat.complete(prepared)).message.content == "Done"
