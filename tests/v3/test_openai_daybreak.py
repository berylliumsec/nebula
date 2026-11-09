"""Focused direct OpenAI API credential, program, and product guidance checks."""

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from nebula.v3.domain import ProviderProfile
from nebula.v3.knowledge_index import ChromaKnowledgeIndex
from nebula.v3.product_knowledge import packaged_product_passages, search_product_knowledge
from nebula.v3.providers import (
    ModelMessage,
    ModelRequest,
    OpenAIResponsesProvider,
    ProviderFlavor,
    config_from_catalog,
    provider_from_profile,
)


@pytest.mark.parametrize("daybreak_blue", [False, True])
async def test_openai_responses_sends_requested_daybreak_blue(daybreak_blue):
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["authorization"] = request.headers.get("authorization")
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_daybreak",
                "model": "gpt-daybreak-blue-latest",
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "ready"}]}],
            },
        )

    config = config_from_catalog(
        provider_id="openai-api",
        flavor=ProviderFlavor.OPENAI,
        api_key_value=SecretStr("synthetic-key"),
        default_model="gpt-daybreak-blue-latest",
        options={"openai_cyber_access_program": "daybreak_blue"} if daybreak_blue else {},
    )
    provider = OpenAIResponsesProvider(config, transport=httpx.MockTransport(handler))
    response = await provider.complete(
        ModelRequest(messages=[ModelMessage(role="user", content="Hello")])
    )

    assert response.text == "ready"
    assert observed["path"] == "/v1/responses"
    assert observed["authorization"] == "Bearer synthetic-key"
    assert observed["payload"].get("access_programs") == (
        {"cyber": "daybreak_blue"} if daybreak_blue else None
    )


def test_daybreak_blue_is_only_a_direct_openai_provider_option():
    profile = ProviderProfile(
        name="OpenAI API",
        provider_type="openai",
        metadata={"options": {"openai_cyber_access_program": "daybreak_blue"}},
    )
    runtime = provider_from_profile(profile)
    assert runtime.config.options["openai_cyber_access_program"] == "daybreak_blue"

    for provider_type, program in (
        ("openrouter", "daybreak_blue"),
        ("openai", "daybreakBlue"),
    ):
        with pytest.raises(ValidationError, match="openai_cyber_access_program"):
            ProviderProfile(
                name="Unsupported program",
                provider_type=provider_type,
                metadata={"options": {"openai_cyber_access_program": program}},
            )


def test_bundled_product_guidance_finds_openai_daybreak_and_syncs_digest(tmp_path, monkeypatch):
    from scripts.build_product_knowledge import ensure, source_digest, source_paths

    ensure()
    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    digest, _ = packaged_product_passages()
    assert digest == source_digest(source_paths())
    matches = search_product_knowledge("Where do I set an OpenAI API key and Daybreak Blue?")
    assert any(item["heading"] == "Model providers" for item in matches)
    index = ChromaKnowledgeIndex(tmp_path / "chroma")
    result = index.sync_product_knowledge()
    assert result["source_digest"] == digest
    assert index._product_collection.metadata["source_digest"] == digest
