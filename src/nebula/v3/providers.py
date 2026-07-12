"""Provider-neutral model gateway for Nebula 3.

The domain only depends on the types in this module.  Provider SDK objects are
never exposed to orchestration, policy, storage, or the UI.  Credentials are
resolved lazily from environment references and are deliberately excluded from
model serialization and repr output.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Iterable
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

import boto3
import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .domain import ProviderProfile


class ProviderError(RuntimeError):
    """A normalized, secret-safe provider failure."""


class UnsupportedCapability(ProviderError):
    """Raised before a request when a required capability is unavailable."""


class ProviderKind(str, Enum):
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    BEDROCK = "bedrock"


class ProviderFlavor(str, Enum):
    """Product/runtime identity, independent of the wire protocol adapter."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    VERTEX = "vertex"
    BEDROCK = "bedrock"
    AZURE_OPENAI = "azure_openai"
    MICROSOFT_FOUNDRY = "microsoft_foundry"
    MISTRAL = "mistral"
    COHERE = "cohere"
    XAI = "xai"
    DEEPSEEK = "deepseek"
    GROQ = "groq"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    OPENROUTER = "openrouter"
    LITELLM = "litellm"
    OLLAMA = "ollama"
    VLLM = "vllm"
    LLAMA_CPP = "llama_cpp"
    SGLANG = "sglang"
    LM_STUDIO = "lm_studio"
    HUGGINGFACE_ENDPOINT = "huggingface_endpoint"
    NVIDIA_NIM = "nvidia_nim"
    CUSTOM = "custom"


class ModelCapabilities(BaseModel):
    streaming: bool = True
    tools: bool = False
    strict_tools: bool = False
    parallel_tools: bool = False
    structured_output: bool = False
    vision: bool = False
    documents: bool = False
    audio: bool = False
    embeddings: bool = False
    reasoning_controls: bool = False
    usage: bool = True
    context_window: int | None = None
    max_output_tokens: int | None = None

    def supports(self, required: Iterable[str]) -> bool:
        return all(bool(getattr(self, name, False)) for name in required)


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    id: str
    kind: ProviderKind
    flavor: ProviderFlavor = ProviderFlavor.CUSTOM
    base_url: str
    default_model: str | None = None
    model_allowlist: list[str] = Field(default_factory=list)
    api_key_env: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    local: bool = False
    data_residency: str | None = None
    data_retention: str | None = None
    enabled: bool = True
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must use http or https")
        return value.rstrip("/")

    @field_validator("api_key_env")
    @classmethod
    def valid_api_key_environment_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("api_key_env must be a valid environment variable name")
        return value

    @model_validator(mode="after")
    def endpoint_respects_locality(self) -> "ProviderConfig":
        parsed = urlsplit(self.base_url)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(
                "provider endpoint must have a host and no URL credentials"
            )
        if parsed.query or parsed.fragment:
            raise ValueError(
                "provider endpoint cannot contain query parameters or fragments"
            )
        host = parsed.hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        is_local_address = host == "localhost" or bool(
            address
            and (
                address.is_loopback
                or address.is_private
                or address.is_link_local
                or address.is_reserved
            )
        )
        if is_local_address and not self.local:
            raise ValueError(
                "private/link-local provider endpoints must be explicitly labeled local"
            )
        if self.local and not is_local_address:
            raise ValueError(
                "local provider endpoints must use localhost or a private, "
                "link-local, or reserved IP address"
            )
        if parsed.scheme == "http" and not is_local_address:
            raise ValueError(
                "unencrypted provider endpoints are allowed only on local/private addresses"
            )
        return self

    def resolve_api_key(self) -> SecretStr | None:
        if not self.api_key_env:
            return None
        value = os.getenv(self.api_key_env)
        if not value:
            raise ProviderError(
                f"provider {self.id!r} requires environment variable "
                f"{self.api_key_env!r}"
            )
        return SecretStr(value)


class ModelMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]]


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool = True

    @field_validator("input_schema")
    @classmethod
    def object_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object":
            raise ValueError("tool input schema must have type=object")
        return value


class ModelToolResult(BaseModel):
    call_id: str
    name: str
    output: dict[str, Any] | str
    is_error: bool = False


class ModelRequest(BaseModel):
    messages: list[ModelMessage]
    model: str | None = None
    instructions: str | None = None
    tools: list[ToolDefinition] = Field(default_factory=list)
    tool_results: list[ModelToolResult] = Field(default_factory=list)
    max_output_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = None
    parallel_tool_calls: bool = False
    response_schema: dict[str, Any] | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ModelResponse(BaseModel):
    provider_id: str
    model: str
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    raw: dict[str, Any] | None = Field(default=None, exclude=True)


class StreamEventType(str, Enum):
    STARTED = "started"
    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    COMPLETED = "completed"
    ERROR = "error"


class ModelStreamEvent(BaseModel):
    type: StreamEventType
    delta: str | None = None
    tool_call: ToolCall | None = None
    response: ModelResponse | None = None
    error: str | None = None


class ProviderHealth(BaseModel):
    provider_id: str
    healthy: bool
    models: list[str] = Field(default_factory=list)
    detail: str | None = None


class ProviderCatalogEntry(BaseModel):
    flavor: ProviderFlavor
    adapter: ProviderKind
    display_name: str
    local: bool = False
    default_base_url: str | None = None
    suggested_key_env: str | None = None
    support_tier: str = Field(pattern=r"^(native|standard|compatible|gateway)$")
    notes: str = "Capabilities are enabled only after provider contract tests."


class ProviderRouteRequest(BaseModel):
    required_capabilities: list[str] = Field(default_factory=list)
    local_only: bool = False
    cloud_allowed: bool = True
    residency: str | None = None
    max_input_cost_per_million: float | None = Field(default=None, ge=0)
    preferred_provider_ids: list[str] = Field(default_factory=list)


class ModelProvider(ABC):
    def __init__(
        self,
        config: ProviderConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport

    @property
    def capabilities(self) -> ModelCapabilities:
        return self.config.capabilities

    def require(self, request: ModelRequest) -> str:
        if not self.config.enabled:
            raise ProviderError(f"provider {self.config.id!r} is disabled")
        model = request.model or self.config.default_model
        if not model:
            raise ProviderError(
                f"provider {self.config.id!r} requires an explicit model"
            )
        if self.config.model_allowlist and model not in self.config.model_allowlist:
            raise ProviderError(
                f"model {model!r} is not allowed by provider {self.config.id!r}"
            )
        required: list[str] = []
        if request.tools or request.tool_results:
            required.append("tools")
            if any(tool.strict for tool in request.tools):
                required.append("strict_tools")
        if request.response_schema:
            required.append("structured_output")
        if not self.capabilities.supports(required):
            missing = [
                name for name in required if not getattr(self.capabilities, name)
            ]
            raise UnsupportedCapability(
                f"provider {self.config.id!r} does not support: {', '.join(missing)}"
            )
        return model

    def _client(self, headers: dict[str, str]) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.base_url,
            headers={**self.config.extra_headers, **headers},
            timeout=self.config.timeout_seconds,
            transport=self._transport,
        )

    def _path(self, path: str) -> str:
        """Avoid duplicating `/v1` when users provide an SDK-style base URL."""

        normalized = "/" + path.lstrip("/")
        base_path = httpx.URL(self.config.base_url).path.rstrip("/")
        if base_path.endswith("/v1") and normalized.startswith("/v1/"):
            return normalized[3:]
        if base_path.endswith("/v1beta") and normalized.startswith("/v1beta/"):
            return normalized[7:]
        return normalized

    def _bearer_or_key_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self.config.resolve_api_key()
        if key:
            header = str(self.config.options.get("api_key_header", "Authorization"))
            scheme = str(self.config.options.get("api_key_scheme", "Bearer "))
            headers[header] = f"{scheme}{key.get_secret_value()}"
        return headers

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        # Providers can override with native streaming.  This fallback still
        # honors cancellation and produces the same event contract.
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        try:
            response = await self.complete(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield ModelStreamEvent(type=StreamEventType.ERROR, error=str(exc))
            return
        if response.text:
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta=response.text)
        for call in response.tool_calls:
            yield ModelStreamEvent(type=StreamEventType.TOOL_CALL, tool_call=call)
        yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)

    @abstractmethod
    async def health(self) -> ProviderHealth:
        raise NotImplementedError


def _safe_error(response: httpx.Response) -> ProviderError:
    request_id = response.headers.get("x-request-id") or response.headers.get(
        "request-id"
    )
    try:
        body = response.json()
        detail = body.get("error", {}).get("message") or body.get("message")
    except (ValueError, AttributeError):
        detail = None
    suffix = f" request_id={request_id}" if request_id else ""
    return ProviderError(
        f"provider returned HTTP {response.status_code}{suffix}"
        + (f": {detail}" if detail else "")
    )


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("provider returned malformed tool arguments") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("provider returned non-object tool arguments")
    return parsed


class OpenAIResponsesProvider(ModelProvider):
    """OpenAI Responses API adapter.

    Function tools use the flattened Responses shape documented by OpenAI;
    commands are never parsed out of prose.
    """

    def _headers(self) -> dict[str, str]:
        return self._bearer_or_key_headers()

    def _payload(self, request: ModelRequest, model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": [message.model_dump() for message in request.messages],
        }
        payload["input"].extend(
            {
                "type": "function_call_output",
                "call_id": result.call_id,
                "output": (
                    json.dumps(result.output, sort_keys=True)
                    if isinstance(result.output, dict)
                    else result.output
                ),
            }
            for result in request.tool_results
        )
        if request.instructions:
            payload["instructions"] = request.instructions
        if request.max_output_tokens:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": tool.strict,
                }
                for tool in request.tools
            ]
        if request.response_schema:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "nebula_response",
                    "strict": True,
                    "schema": request.response_schema,
                }
            }
        if request.metadata:
            payload["metadata"] = request.metadata
        return payload

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        async with self._client(self._headers()) as client:
            response = await client.post(
                self._path("/v1/responses"), json=self._payload(request, model)
            )
        if response.is_error:
            raise _safe_error(response)
        data = response.json()
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for item in data.get("output", []):
            if item.get("type") == "function_call":
                calls.append(
                    ToolCall(
                        id=item.get("call_id") or item.get("id", ""),
                        name=item["name"],
                        arguments=_arguments(item.get("arguments")),
                    )
                )
            elif item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"}:
                        text_parts.append(content.get("text", ""))
        usage = data.get("usage") or {}
        return ModelResponse(
            provider_id=self.config.id,
            model=data.get("model", model),
            text="".join(text_parts),
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get(
                    "total_tokens",
                    usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                ),
            ),
            finish_reason=data.get("status"),
            provider_request_id=data.get("id"),
            raw=data,
        )

    async def health(self) -> ProviderHealth:
        try:
            async with self._client(self._headers()) as client:
                response = await client.get(self._path("/v1/models"))
            if response.is_error:
                raise _safe_error(response)
            models = [item["id"] for item in response.json().get("data", [])]
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=models
            )
        except Exception as exc:
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )


class OpenAICompatibleProvider(ModelProvider):
    """Adapter for Chat Completions-compatible hosted and local runtimes."""

    def _headers(self) -> dict[str, str]:
        return self._bearer_or_key_headers()

    def _payload(self, request: ModelRequest, model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [message.model_dump() for message in request.messages],
        }
        payload["messages"].extend(
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": (
                    json.dumps(result.output, sort_keys=True)
                    if isinstance(result.output, dict)
                    else result.output
                ),
            }
            for result in request.tool_results
        )
        if request.instructions:
            payload["messages"].insert(
                0, {"role": "system", "content": request.instructions}
            )
        if request.max_output_tokens:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                        "strict": tool.strict,
                    },
                }
                for tool in request.tools
            ]
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "nebula_response",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        return payload

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        payload = self._payload(request, model)
        async with self._client(self._headers()) as client:
            response = await client.post(
                self._path("/v1/chat/completions"), json=payload
            )
        if response.is_error:
            raise _safe_error(response)
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = [
            ToolCall(
                id=item.get("id", ""),
                name=item.get("function", {}).get("name", ""),
                arguments=_arguments(item.get("function", {}).get("arguments")),
            )
            for item in message.get("tool_calls", [])
        ]
        usage = data.get("usage") or {}
        return ModelResponse(
            provider_id=self.config.id,
            model=data.get("model", model),
            text=message.get("content") or "",
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason"),
            provider_request_id=data.get("id"),
            raw=data,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        async for event in _stream_openai_compatible(self, request):
            yield event

    async def health(self) -> ProviderHealth:
        try:
            async with self._client(self._headers()) as client:
                response = await client.get(self._path("/v1/models"))
            if response.is_error:
                raise _safe_error(response)
            models = [item["id"] for item in response.json().get("data", [])]
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=models
            )
        except Exception as exc:
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )


async def _stream_openai_compatible(
    provider: OpenAICompatibleProvider, request: ModelRequest
) -> AsyncIterator[ModelStreamEvent]:
    """Stream Chat Completions SSE for vLLM and compatible runtimes."""

    model = provider.require(request)
    payload = provider._payload(request, model)
    payload.update({"stream": True, "stream_options": {"include_usage": True}})
    yield ModelStreamEvent(type=StreamEventType.STARTED)
    text_parts: list[str] = []
    call_parts: dict[int, dict[str, str]] = {}
    usage = ModelUsage()
    finish_reason: str | None = None
    response_id: str | None = None
    response_model = model
    try:
        async with provider._client(provider._headers()) as client:
            async with client.stream(
                "POST", provider._path("/v1/chat/completions"), json=payload
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise _safe_error(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    encoded = line.removeprefix("data:").strip()
                    if not encoded or encoded == "[DONE]":
                        continue
                    data = json.loads(encoded)
                    response_id = data.get("id", response_id)
                    response_model = data.get("model", response_model)
                    chunk_usage = data.get("usage") or {}
                    if chunk_usage:
                        usage = ModelUsage(
                            input_tokens=chunk_usage.get("prompt_tokens", 0),
                            output_tokens=chunk_usage.get("completion_tokens", 0),
                            total_tokens=chunk_usage.get("total_tokens", 0),
                        )
                    choice = (data.get("choices") or [{}])[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        text_parts.append(content)
                        yield ModelStreamEvent(
                            type=StreamEventType.TEXT_DELTA, delta=content
                        )
                    for item in delta.get("tool_calls", []):
                        index = int(item.get("index", 0))
                        current = call_parts.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        current["id"] += item.get("id") or ""
                        function = item.get("function") or {}
                        current["name"] += function.get("name") or ""
                        arguments = function.get("arguments")
                        current["arguments"] += (
                            json.dumps(arguments)
                            if isinstance(arguments, dict)
                            else arguments or ""
                        )
        calls = [
            ToolCall(
                id=value["id"],
                name=value["name"],
                arguments=_arguments(value["arguments"]),
            )
            for _, value in sorted(call_parts.items())
        ]
        for call in calls:
            yield ModelStreamEvent(type=StreamEventType.TOOL_CALL, tool_call=call)
        final = ModelResponse(
            provider_id=provider.config.id,
            model=response_model,
            text="".join(text_parts),
            tool_calls=calls,
            usage=usage,
            finish_reason=finish_reason,
            provider_request_id=response_id,
        )
        yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=final)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        yield ModelStreamEvent(type=StreamEventType.ERROR, error=str(exc))


class AnthropicProvider(ModelProvider):
    def _headers(self) -> dict[str, str]:
        key = self.config.resolve_api_key()
        if not key:
            raise ProviderError("Anthropic requires an API key")
        return {
            "x-api-key": key.get_secret_value(),
            "anthropic-version": self.config.options.get(
                "anthropic_version", "2023-06-01"
            ),
            "Content-Type": "application/json",
        }

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens or 4096,
            "messages": [
                message.model_dump()
                for message in request.messages
                if message.role != "system"
            ],
        }
        payload["messages"].extend(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": (
                            json.dumps(result.output, sort_keys=True)
                            if isinstance(result.output, dict)
                            else result.output
                        ),
                        "is_error": result.is_error,
                    }
                ],
            }
            for result in request.tool_results
        )
        systems = [m.content for m in request.messages if m.role == "system"]
        if request.instructions or systems:
            payload["system"] = "\n".join(
                [str(item) for item in [*systems, request.instructions] if item]
            )
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
        async with self._client(self._headers()) as client:
            response = await client.post(self._path("/v1/messages"), json=payload)
        if response.is_error:
            raise _safe_error(response)
        data = response.json()
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments=_arguments(block.get("input")),
                    )
                )
        usage = data.get("usage") or {}
        return ModelResponse(
            provider_id=self.config.id,
            model=data.get("model", model),
            text="".join(text_parts),
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0),
            ),
            finish_reason=data.get("stop_reason"),
            provider_request_id=data.get("id"),
            raw=data,
        )

    async def health(self) -> ProviderHealth:
        # Anthropic does not require model discovery for health.  A configured
        # key and model is a non-invasive readiness signal; live calls remain
        # explicit to avoid cost.
        try:
            self._headers()
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=bool(self.config.default_model),
                models=[self.config.default_model] if self.config.default_model else [],
                detail=None
                if self.config.default_model
                else "no default model configured",
            )
        except Exception as exc:
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )


class GeminiProvider(ModelProvider):
    def _headers(self) -> dict[str, str]:
        key = self.config.resolve_api_key()
        if not key:
            raise ProviderError("Gemini/Vertex requires an API key or access token")
        if self.config.flavor == ProviderFlavor.VERTEX:
            return {
                "Authorization": f"Bearer {key.get_secret_value()}",
                "Content-Type": "application/json",
            }
        return {
            "x-goog-api-key": key.get_secret_value(),
            "Content-Type": "application/json",
        }

    def _model_path(self, model: str, operation: str) -> str:
        if self.config.flavor != ProviderFlavor.VERTEX:
            return self._path(f"/v1beta/models/{model}:{operation}")
        project = self.config.options.get("project")
        location = self.config.options.get("location")
        if not project or not location:
            raise ProviderError("Vertex profiles require project and location options")
        publisher = self.config.options.get("publisher", "google")
        return (
            f"/v1/projects/{project}/locations/{location}/publishers/"
            f"{publisher}/models/{model}:{operation}"
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        contents = []
        for message in request.messages:
            if message.role == "system":
                continue
            role = "model" if message.role == "assistant" else "user"
            parts = (
                [{"text": message.content}]
                if isinstance(message.content, str)
                else message.content
            )
            contents.append({"role": role, "parts": parts})
        contents.extend(
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": result.name,
                            "response": (
                                result.output
                                if isinstance(result.output, dict)
                                else {"output": result.output}
                            ),
                        }
                    }
                ],
            }
            for result in request.tool_results
        )
        payload: dict[str, Any] = {"contents": contents}
        system_parts = [str(m.content) for m in request.messages if m.role == "system"]
        if request.instructions:
            system_parts.append(request.instructions)
        if system_parts:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n".join(system_parts)}]
            }
        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        }
                        for tool in request.tools
                    ]
                }
            ]
        generation: dict[str, Any] = {}
        if request.max_output_tokens:
            generation["maxOutputTokens"] = request.max_output_tokens
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.response_schema:
            generation.update(
                {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": request.response_schema,
                }
            )
        if generation:
            payload["generationConfig"] = generation
        async with self._client(self._headers()) as client:
            response = await client.post(
                self._model_path(model, "generateContent"), json=payload
            )
        if response.is_error:
            raise _safe_error(response)
        data = response.json()
        candidate = (data.get("candidates") or [{}])[0]
        parts = candidate.get("content", {}).get("parts", [])
        text_parts = [part.get("text", "") for part in parts if "text" in part]
        calls = [
            ToolCall(
                id=part.get("functionCall", {}).get("id", ""),
                name=part["functionCall"]["name"],
                arguments=_arguments(part["functionCall"].get("args")),
            )
            for part in parts
            if "functionCall" in part
        ]
        usage = data.get("usageMetadata") or {}
        return ModelResponse(
            provider_id=self.config.id,
            model=model,
            text="".join(text_parts),
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=usage.get("promptTokenCount", 0),
                output_tokens=usage.get("candidatesTokenCount", 0),
                total_tokens=usage.get("totalTokenCount", 0),
            ),
            finish_reason=candidate.get("finishReason"),
            provider_request_id=data.get("responseId"),
            raw=data,
        )

    async def health(self) -> ProviderHealth:
        try:
            async with self._client(self._headers()) as client:
                if self.config.flavor == ProviderFlavor.VERTEX:
                    project = self.config.options.get("project")
                    location = self.config.options.get("location")
                    if not project or not location:
                        raise ProviderError(
                            "Vertex profiles require project and location options"
                        )
                    response = await client.get(
                        f"/v1/projects/{project}/locations/{location}/publishers/google/models"
                    )
                else:
                    response = await client.get(self._path("/v1beta/models"))
            if response.is_error:
                raise _safe_error(response)
            models = [
                item["name"].rsplit("/", 1)[-1]
                for item in response.json().get("models", [])
            ]
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=models
            )
        except Exception as exc:
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )


class BedrockProvider(ModelProvider):
    """AWS Bedrock Converse adapter using boto3 without leaking its types."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        kwargs: dict[str, Any] = {
            "modelId": model,
            "messages": [
                {
                    "role": "assistant" if msg.role == "assistant" else "user",
                    "content": [{"text": str(msg.content)}],
                }
                for msg in request.messages
                if msg.role != "system"
            ],
        }
        kwargs["messages"].extend(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": result.call_id,
                            "content": [
                                (
                                    {"json": result.output}
                                    if isinstance(result.output, dict)
                                    else {"text": result.output}
                                )
                            ],
                            "status": "error" if result.is_error else "success",
                        }
                    }
                ],
            }
            for result in request.tool_results
        )
        systems = [str(m.content) for m in request.messages if m.role == "system"]
        if request.instructions:
            systems.append(request.instructions)
        if systems:
            kwargs["system"] = [{"text": "\n".join(systems)}]
        if request.tools:
            kwargs["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": tool.input_schema},
                        }
                    }
                    for tool in request.tools
                ]
            }
        inference: dict[str, Any] = {}
        if request.max_output_tokens:
            inference["maxTokens"] = request.max_output_tokens
        if request.temperature is not None:
            inference["temperature"] = request.temperature
        if inference:
            kwargs["inferenceConfig"] = inference

        def invoke() -> dict[str, Any]:
            client = boto3.client(
                "bedrock-runtime", region_name=self.config.options.get("region")
            )
            return client.converse(**kwargs)

        try:
            data = await asyncio.to_thread(invoke)
        except Exception as exc:
            raise ProviderError(
                f"Bedrock request failed: {type(exc).__name__}"
            ) from exc
        blocks = data.get("output", {}).get("message", {}).get("content", [])
        calls = [
            ToolCall(
                id=block["toolUse"].get("toolUseId", ""),
                name=block["toolUse"].get("name", ""),
                arguments=_arguments(block["toolUse"].get("input")),
            )
            for block in blocks
            if "toolUse" in block
        ]
        usage = data.get("usage") or {}
        return ModelResponse(
            provider_id=self.config.id,
            model=model,
            text="".join(block.get("text", "") for block in blocks),
            tool_calls=calls,
            usage=ModelUsage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
                total_tokens=usage.get("totalTokens", 0),
            ),
            finish_reason=data.get("stopReason"),
            provider_request_id=(data.get("ResponseMetadata") or {}).get("RequestId"),
            raw=data,
        )

    async def health(self) -> ProviderHealth:
        def discover() -> list[str]:
            client = boto3.client(
                "bedrock",
                region_name=self.config.options.get("region"),
            )
            response = client.list_foundation_models()
            return [
                item["modelId"]
                for item in response.get("modelSummaries", [])
                if isinstance(item.get("modelId"), str)
            ]

        try:
            models = await asyncio.to_thread(discover)
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=True,
                models=list(dict.fromkeys(models)),
            )
        except Exception as exc:
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=False,
                detail=f"Bedrock health check failed: {type(exc).__name__}",
            )


PROVIDER_CATALOG: dict[ProviderFlavor, ProviderCatalogEntry] = {
    entry.flavor: entry
    for entry in [
        ProviderCatalogEntry(
            flavor=ProviderFlavor.OPENAI,
            adapter=ProviderKind.OPENAI_RESPONSES,
            display_name="OpenAI",
            default_base_url="https://api.openai.com",
            suggested_key_env="OPENAI_API_KEY",
            support_tier="native",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.ANTHROPIC,
            adapter=ProviderKind.ANTHROPIC,
            display_name="Anthropic",
            default_base_url="https://api.anthropic.com",
            suggested_key_env="ANTHROPIC_API_KEY",
            support_tier="native",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.GEMINI,
            adapter=ProviderKind.GEMINI,
            display_name="Google Gemini",
            default_base_url="https://generativelanguage.googleapis.com",
            suggested_key_env="GEMINI_API_KEY",
            support_tier="native",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.VERTEX,
            adapter=ProviderKind.GEMINI,
            display_name="Google Vertex AI",
            suggested_key_env="GOOGLE_ACCESS_TOKEN",
            support_tier="native",
            notes="Requires a Vertex endpoint and OAuth header profile; capabilities are contract-tested per deployment.",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.BEDROCK,
            adapter=ProviderKind.BEDROCK,
            display_name="AWS Bedrock",
            default_base_url="https://bedrock-runtime.amazonaws.com",
            suggested_key_env=None,
            support_tier="native",
            notes="Uses the AWS credential chain; credentials are never sent to sandbox workers.",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.AZURE_OPENAI,
            adapter=ProviderKind.OPENAI_RESPONSES,
            display_name="Azure OpenAI",
            suggested_key_env="AZURE_OPENAI_API_KEY",
            support_tier="native",
            notes="Use the resource `/openai/v1` endpoint and set api_key_header=api-key.",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.MICROSOFT_FOUNDRY,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Microsoft Foundry",
            suggested_key_env="AZURE_AI_API_KEY",
            support_tier="native",
            notes="Supports Foundry deployments exposing the OpenAI v1 contract.",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.MISTRAL,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Mistral",
            default_base_url="https://api.mistral.ai/v1",
            suggested_key_env="MISTRAL_API_KEY",
            support_tier="standard",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.COHERE,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Cohere",
            suggested_key_env="COHERE_API_KEY",
            support_tier="gateway",
            notes="Use a tested OpenAI-compatible gateway profile until the native contract is enabled.",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.XAI,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="xAI",
            default_base_url="https://api.x.ai/v1",
            suggested_key_env="XAI_API_KEY",
            support_tier="standard",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.DEEPSEEK,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="DeepSeek",
            default_base_url="https://api.deepseek.com/v1",
            suggested_key_env="DEEPSEEK_API_KEY",
            support_tier="standard",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.GROQ,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Groq",
            default_base_url="https://api.groq.com/openai/v1",
            suggested_key_env="GROQ_API_KEY",
            support_tier="standard",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.TOGETHER,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Together AI",
            default_base_url="https://api.together.xyz/v1",
            suggested_key_env="TOGETHER_API_KEY",
            support_tier="standard",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.FIREWORKS,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Fireworks AI",
            default_base_url="https://api.fireworks.ai/inference/v1",
            suggested_key_env="FIREWORKS_API_KEY",
            support_tier="standard",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.OPENROUTER,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="OpenRouter",
            default_base_url="https://openrouter.ai/api/v1",
            suggested_key_env="OPENROUTER_API_KEY",
            support_tier="standard",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.LITELLM,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="LiteLLM proxy",
            support_tier="gateway",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.OLLAMA,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Ollama",
            local=True,
            default_base_url="http://127.0.0.1:11434/v1",
            support_tier="compatible",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.VLLM,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="vLLM",
            local=True,
            default_base_url="http://127.0.0.1:8000/v1",
            support_tier="compatible",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.LLAMA_CPP,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="llama.cpp server",
            local=True,
            default_base_url="http://127.0.0.1:8080/v1",
            support_tier="compatible",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.SGLANG,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="SGLang",
            local=True,
            default_base_url="http://127.0.0.1:30000/v1",
            support_tier="compatible",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.LM_STUDIO,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="LM Studio",
            local=True,
            default_base_url="http://127.0.0.1:1234/v1",
            support_tier="compatible",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.HUGGINGFACE_ENDPOINT,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Hugging Face Inference Endpoint",
            suggested_key_env="HF_TOKEN",
            support_tier="compatible",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.NVIDIA_NIM,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="NVIDIA NIM",
            support_tier="compatible",
        ),
        ProviderCatalogEntry(
            flavor=ProviderFlavor.CUSTOM,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="Custom OpenAI-compatible endpoint",
            support_tier="compatible",
        ),
    ]
}


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider, *, replace: bool = False) -> None:
        if provider.config.id in self._providers and not replace:
            raise ValueError(f"provider {provider.config.id!r} is already registered")
        self._providers[provider.config.id] = provider

    def get(self, provider_id: str) -> ModelProvider:
        try:
            provider = self._providers[provider_id]
        except KeyError as exc:
            raise ProviderError(f"unknown provider {provider_id!r}") from exc
        if not provider.config.enabled:
            raise ProviderError(f"provider {provider_id!r} is disabled")
        return provider

    def select(
        self,
        *,
        required: Iterable[str] = (),
        local_only: bool = False,
        residency: str | None = None,
    ) -> ModelProvider:
        for provider in self._providers.values():
            if not provider.config.enabled:
                continue
            if local_only and not provider.config.local:
                continue
            if residency and provider.config.data_residency != residency:
                continue
            if provider.capabilities.supports(required):
                return provider
        raise UnsupportedCapability(
            "no provider satisfies the requested policy and capabilities"
        )

    def route(self, request: ProviderRouteRequest) -> ModelProvider:
        if request.local_only and not request.cloud_allowed:
            cloud_allowed = False
        else:
            cloud_allowed = request.cloud_allowed
        ordered = list(self._providers.values())
        preference = {
            value: index for index, value in enumerate(request.preferred_provider_ids)
        }
        ordered.sort(key=lambda item: preference.get(item.config.id, len(preference)))
        for provider in ordered:
            config = provider.config
            if not config.enabled:
                continue
            if request.local_only and not config.local:
                continue
            if not cloud_allowed and not config.local:
                continue
            if request.residency and config.data_residency != request.residency:
                continue
            declared_cost = config.options.get("input_cost_per_million")
            if request.max_input_cost_per_million is not None and (
                declared_cost is None
                or float(declared_cost) > request.max_input_cost_per_million
            ):
                continue
            if not provider.capabilities.supports(request.required_capabilities):
                continue
            return provider
        raise UnsupportedCapability(
            "no provider satisfies the requested capabilities, privacy, residency, and budget"
        )

    async def health(self) -> list[ProviderHealth]:
        return list(
            await asyncio.gather(
                *(provider.health() for provider in self._providers.values())
            )
        )

    def public_configs(self) -> list[dict[str, Any]]:
        return [provider.config.model_dump() for provider in self._providers.values()]


def build_provider(
    config: ProviderConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> ModelProvider:
    implementations: dict[ProviderKind, type[ModelProvider]] = {
        ProviderKind.OPENAI_RESPONSES: OpenAIResponsesProvider,
        ProviderKind.OPENAI_COMPATIBLE: OpenAICompatibleProvider,
        ProviderKind.ANTHROPIC: AnthropicProvider,
        ProviderKind.GEMINI: GeminiProvider,
        ProviderKind.BEDROCK: BedrockProvider,
    }
    return implementations[config.kind](config, transport=transport)


def config_from_catalog(
    *,
    provider_id: str,
    flavor: ProviderFlavor,
    base_url: str | None = None,
    api_key_env: str | None = None,
    capabilities: ModelCapabilities | None = None,
    **overrides: Any,
) -> ProviderConfig:
    entry = PROVIDER_CATALOG[flavor]
    endpoint = base_url or entry.default_base_url
    if endpoint is None:
        raise ValueError(f"{entry.display_name} requires an explicit endpoint")
    options = dict(overrides.pop("options", {}))
    local = bool(overrides.pop("local", entry.local)) or entry.local
    if flavor == ProviderFlavor.AZURE_OPENAI:
        options.setdefault("api_key_header", "api-key")
        options.setdefault("api_key_scheme", "")
    return ProviderConfig(
        id=provider_id,
        kind=entry.adapter,
        flavor=flavor,
        base_url=endpoint,
        api_key_env=api_key_env or entry.suggested_key_env,
        local=local,
        capabilities=capabilities or ModelCapabilities(),
        options=options,
        **overrides,
    )


def provider_from_profile(profile: ProviderProfile) -> ModelProvider:
    """Build a runtime adapter from a persisted, secret-safe provider profile."""

    legacy_flavors = {
        "openai-compatible": ProviderFlavor.CUSTOM.value,
        "openai_compatible": ProviderFlavor.CUSTOM.value,
        "openai-responses": ProviderFlavor.OPENAI.value,
        "openai_responses": ProviderFlavor.OPENAI.value,
    }
    provider_type = legacy_flavors.get(profile.provider_type, profile.provider_type)
    try:
        flavor = ProviderFlavor(provider_type)
    except ValueError as exc:
        raise ValueError(
            f"unknown provider type {profile.provider_type!r}; "
            "use provider-catalog values"
        ) from exc
    secret_env: str | None = None
    if profile.secret_ref:
        if not profile.secret_ref.startswith("env:"):
            raise ValueError("provider secret_ref must use an env:NAME reference")
        secret_env = profile.secret_ref.removeprefix("env:")
    capabilities = ModelCapabilities(
        streaming=profile.capabilities.streaming,
        tools=profile.capabilities.tool_calling,
        strict_tools=profile.capabilities.strict_structured_output,
        parallel_tools=profile.capabilities.parallel_tool_calls,
        structured_output=profile.capabilities.strict_structured_output,
        vision=profile.capabilities.vision,
        documents=profile.capabilities.documents,
        audio=profile.capabilities.audio,
        embeddings=profile.capabilities.embeddings,
        reasoning_controls=profile.capabilities.reasoning_controls,
    )
    default_model = profile.metadata.get("default_model") or next(
        iter(profile.model_allowlist), None
    )
    config = config_from_catalog(
        provider_id=profile.id,
        flavor=flavor,
        base_url=profile.endpoint,
        api_key_env=secret_env,
        default_model=default_model,
        model_allowlist=profile.model_allowlist,
        capabilities=capabilities,
        local=profile.is_local,
        enabled=profile.enabled,
        data_residency=(
            profile.privacy.residency[0] if profile.privacy.residency else None
        ),
        data_retention=profile.privacy.retention,
        options=profile.metadata.get("options", {}),
    )
    if profile.privacy.local_only and not config.local:
        raise ValueError("a local-only privacy profile cannot use a cloud provider")
    return build_provider(config)


__all__ = [
    "AnthropicProvider",
    "BedrockProvider",
    "GeminiProvider",
    "ModelCapabilities",
    "ModelMessage",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelToolResult",
    "OpenAICompatibleProvider",
    "OpenAIResponsesProvider",
    "PROVIDER_CATALOG",
    "ProviderCatalogEntry",
    "ProviderConfig",
    "ProviderError",
    "ProviderFlavor",
    "ProviderHealth",
    "ProviderKind",
    "ProviderRegistry",
    "ProviderRouteRequest",
    "ToolDefinition",
    "UnsupportedCapability",
    "build_provider",
    "config_from_catalog",
    "provider_from_profile",
]
