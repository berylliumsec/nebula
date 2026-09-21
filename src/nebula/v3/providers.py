"""Provider-neutral model gateway for Nebula 3.

The domain only depends on the types in this module.  Provider SDK objects are
never exposed to orchestration, policy, storage, or the UI.  Credentials are
resolved lazily from environment references and are deliberately excluded from
model serialization and repr output.
"""

from __future__ import annotations

from .diagnostics import record_caught_exception, record_diagnostic

import asyncio
import base64
import binascii
import ipaddress
import hashlib
import json
import os
import random
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Literal
from urllib.parse import quote, urlsplit

import boto3  # type: ignore[import-untyped]
from botocore.config import Config as BotocoreConfig  # type: ignore[import-untyped]
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
from .dsml import recover as _recover_dsml
from .model_catalog import (
    ModelDescriptor,
    ModelRouteDescriptor,
    UpstreamProvider,
    openrouter_model_routes,
    openrouter_models,
    openrouter_upstream_providers,
)
from .redaction import redact_text


class ProviderError(RuntimeError):
    """A normalized, secret-safe provider failure."""


class ProviderContextLengthError(ProviderError):
    """The provider explicitly rejected the request for exceeding context."""


class ProviderOverloadedError(ProviderError):
    """A transient upstream failure that the identical request can retry."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ProviderResponseError(ProviderError):
    """The provider completed a request without the required response shape."""

    status_code = 502


class ProviderQuotaError(ProviderError):
    """The account's quota or billing limit is spent; retrying cannot help."""


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
    ORCAROUTER = "orcarouter"
    LITELLM = "litellm"
    OLLAMA = "ollama"
    VLLM = "vllm"
    LLAMA_CPP = "llama_cpp"
    SGLANG = "sglang"
    LM_STUDIO = "lm_studio"
    HUGGINGFACE_ENDPOINT = "huggingface_endpoint"
    NVIDIA_NIM = "nvidia_nim"
    CUSTOM = "custom"


class ToolChoice(str, Enum):
    """Provider-neutral tool routing policy."""

    AUTO = "auto"
    REQUIRED = "required"


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
    api_key_value: SecretStr | None = Field(default=None, exclude=True)
    credential_ref: str | None = Field(default=None, exclude=True)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    local: bool = False
    data_residency: str | None = None
    data_retention: str | None = None
    enabled: bool = True
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    options: dict[str, Any] = Field(default_factory=dict)
    # Request parameters each exact model advertises (OpenRouter catalog).
    model_parameters: dict[str, list[str]] = Field(default_factory=dict)

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
        except ValueError as caught_error:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_001",
                "A handled providers operation raised an exception.",
                caught_error,
                stage="providers",
            )
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
        if self.api_key_value is not None:
            return self.api_key_value
        if self.credential_ref:
            raise ProviderError(
                f"provider {self.id!r} credential reference is unavailable"
            )
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


def _openai_responses_message(message: ModelMessage) -> dict[str, Any]:
    if isinstance(message.content, str):
        return message.model_dump()
    content: list[dict[str, Any]] = []
    for part in message.content:
        if part.get("type") == "image":
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{part.get('media_type')};base64,{part.get('data')}",
                }
            )
        elif part.get("type") == "text":
            content.append({"type": "input_text", "text": str(part.get("text") or "")})
    return {"role": message.role, "content": content}


def _openai_chat_message(message: ModelMessage) -> dict[str, Any]:
    if isinstance(message.content, str):
        return message.model_dump()
    content: list[dict[str, Any]] = []
    for part in message.content:
        if part.get("type") == "image":
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.get('media_type')};base64,{part.get('data')}"
                    },
                }
            )
        elif part.get("type") == "text":
            content.append({"type": "text", "text": str(part.get("text") or "")})
    return {"role": message.role, "content": content}


def _anthropic_message(message: ModelMessage) -> dict[str, Any]:
    if isinstance(message.content, str):
        return message.model_dump()
    content: list[dict[str, Any]] = []
    for part in message.content:
        if part.get("type") == "image":
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.get("media_type"),
                        "data": part.get("data"),
                    },
                }
            )
        elif part.get("type") == "text":
            content.append({"type": "text", "text": str(part.get("text") or "")})
    return {"role": message.role, "content": content}


def _gemini_parts(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict[str, Any]] = []
    for part in content:
        if part.get("type") == "image":
            parts.append(
                {
                    "inline_data": {
                        "mime_type": part.get("media_type"),
                        "data": part.get("data"),
                    }
                }
            )
        elif part.get("type") == "text":
            parts.append({"text": str(part.get("text") or "")})
    return parts


_BEDROCK_IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _bedrock_content(content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converse content blocks. An image travels as an image block, never as text."""

    if isinstance(content, str):
        return [{"text": content}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            blocks.append({"text": str(part.get("text") or "")})
            continue
        if kind != "image":
            raise ProviderError(f"Bedrock does not accept {kind or 'untyped'} parts")
        media_type = str(part.get("media_type") or "").lower()
        image_format = _BEDROCK_IMAGE_FORMATS.get(media_type)
        if image_format is None:
            raise ProviderError(
                f"Bedrock does not accept {media_type or 'untyped'} images; "
                "send png, jpeg, gif or webp"
            )
        try:
            data = base64.b64decode(str(part.get("data") or ""), validate=True)
        except (
            binascii.Error,
            ValueError,
        ) as exc:  # diagnostic-expected: a malformed attachment is reported, not sent as text
            raise ProviderError("image part is not valid base64") from exc
        blocks.append({"image": {"format": image_format, "source": {"bytes": data}}})
    return blocks


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
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | str
    is_error: bool = False


ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
"""Reasoning levels Nebula can ask for, lowest spend first past ``none``.

A route advertises whether it takes the control at all; it does not advertise
which levels it honours, so an adapter sends what it was asked for and the
caller treats an unchanged response as the answer, not as a guarantee.
"""

REASONING_EFFORTS: tuple[ReasoningEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)


class ModelRequest(BaseModel):
    messages: list[ModelMessage]
    model: str | None = None
    instructions: str | None = None
    tools: list[ToolDefinition] = Field(default_factory=list)
    tool_results: list[ModelToolResult] = Field(default_factory=list)
    tool_choice: ToolChoice = ToolChoice.AUTO
    max_output_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = None
    parallel_tool_calls: bool = False
    response_schema: dict[str, Any] | None = None
    # How much of the output budget a reasoning model may spend thinking.
    # Provider-neutral: an adapter translates it, or ignores it where the
    # route does not advertise the control. ``None`` leaves the model's own
    # default alone, which is what an ordinary turn wants.
    reasoning_effort: ReasoningEffort | None = None
    reasoning_max_tokens: int | None = Field(default=None, gt=0)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def required_choice_has_tools(self) -> "ModelRequest":
        if self.tool_choice == ToolChoice.REQUIRED and not self.tools:
            raise ValueError("tool_choice=required requires at least one tool")
        return self


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=500)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    arguments: dict[str, Any]


def _normalized_tool_call(**values: Any) -> ToolCall:
    try:
        return ToolCall.model_validate(values)
    except ValueError as exc:
        record_caught_exception(
            "providers",
            "providers.providers.malformed_tool_call",
            "A provider returned an invalid tool-call identity or name.",
            exc,
            stage="providers",
        )
        raise ProviderError("provider returned a malformed tool call") from exc


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ModelResponse(BaseModel):
    provider_id: str
    model: str
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    raw: dict[str, Any] | None = Field(default=None, exclude=True)
    raw_body: bytes | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _recover_serialized_tool_calls(self) -> "ModelResponse":
        """Read DSML tool calls a route serialized into ``text``.

        Some OpenRouter routes put a model's DSML tool calls in the Chat
        Completions ``content`` field. They are tool calls wherever they
        arrive, so Core reads them into ``tool_calls`` and every caller
        treats them the same as a call the route reported properly: the
        broker, the policy engine and approvals all still apply. A frame
        Core cannot read completely is left in ``text`` untouched, for the
        caller's quarantine to refuse.
        """

        if "DSML" not in self.text:
            return self
        found = _recover_dsml(self.text)
        if not found.recovered:
            return self
        recovered: list[ToolCall] = []
        for index, call in enumerate(found.calls):
            try:
                recovered.append(
                    ToolCall(
                        id=f"dsml-{uuid.uuid4().hex[:24]}",
                        name=call.name,
                        arguments=call.arguments,
                    )
                )
            except ValueError as exc:
                record_caught_exception(
                    "providers",
                    "providers.dsml.invalid_recovered_call",
                    "A DSML frame parsed into a call the tool schema rejects.",
                    exc,
                    stage="providers",
                    metadata={"call_index": index},
                )
                # One unusable call makes the whole frame untrustworthy, so
                # the text is left exactly as it arrived.
                return self
        record_diagnostic(
            "warning",
            "providers",
            "providers.dsml.recovered_tool_calls",
            "A route serialized tool calls into assistant content; Core "
            "recovered them instead of discarding the turn.",
            outcome="fallback",
            stage="providers",
            retryable=False,
            metadata={
                "provider_id": self.provider_id,
                "model": self.model,
                "recovered_calls": len(recovered),
                "unparsed_frames": found.unparsed_frames,
            },
        )
        # An after-validator has to settle the model in place; a copy is
        # discarded when the model is built through ``__init__``.
        self.text = found.text
        self.tool_calls = [*self.tool_calls, *recovered]
        return self


class StreamEventType(str, Enum):
    STARTED = "started"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL = "tool_call"
    COMPLETED = "completed"
    ERROR = "error"


class ModelStreamEvent(BaseModel):
    type: StreamEventType
    delta: str | None = None
    tool_call: ToolCall | None = None
    response: ModelResponse | None = None
    error: str | None = None
    context_length_exceeded: bool = False
    # A transient upstream failure the operator may simply retry, as opposed
    # to a request defect or a chat-side error.
    retryable: bool = False


class ProviderHealth(BaseModel):
    provider_id: str
    healthy: bool
    models: list[str] = Field(default_factory=list)
    model_descriptors: list[ModelDescriptor] = Field(default_factory=list)
    # Discovered models outside the profile's model allowlist, offered for adding.
    unlisted_models: list[str] = Field(default_factory=list)
    unlisted_model_descriptors: list[ModelDescriptor] = Field(default_factory=list)
    # OpenRouter's upstream provider directory, for the routing allowlist.
    upstream_providers: list[UpstreamProvider] = Field(default_factory=list)
    detail: str | None = None
    credential_verified: bool | None = None
    catalog_source: str | None = None
    key_expires_at: str | None = None
    key_limit_remaining: float | None = None
    provider_revision: int | None = Field(default=None, ge=1)


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

    def _client(
        self, headers: dict[str, str], *, timeout: httpx.Timeout | None = None
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.config.base_url,
            headers={**self.config.extra_headers, **headers},
            timeout=self.config.timeout_seconds if timeout is None else timeout,
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

    async def _post(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict[str, Any],
        *,
        operation: str,
    ) -> httpx.Response:
        """POST one provider request, retrying transient upstream failures."""

        return await _send_with_retry(
            self.config,
            lambda: client.post(path, json=payload),
            operation=operation,
        )

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        # Providers can override with native streaming.  This fallback still
        # honors cancellation and produces the same event contract.
        yield ModelStreamEvent(type=StreamEventType.STARTED)
        try:
            response = await self.complete(request)
        except asyncio.CancelledError as caught_error:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_002",
                "A handled providers operation raised an exception.",
                caught_error,
                stage="providers",
            )
            raise
        except Exception as exc:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_003",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            yield ModelStreamEvent(
                type=StreamEventType.ERROR,
                error=str(exc),
                context_length_exceeded=isinstance(exc, ProviderContextLengthError),
                retryable=isinstance(exc, ProviderOverloadedError),
            )
            return
        if response.reasoning:
            yield ModelStreamEvent(
                type=StreamEventType.REASONING_DELTA, delta=response.reasoning
            )
        if response.text:
            yield ModelStreamEvent(type=StreamEventType.TEXT_DELTA, delta=response.text)
        for call in response.tool_calls:
            yield ModelStreamEvent(type=StreamEventType.TOOL_CALL, tool_call=call)
        yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=response)

    @abstractmethod
    async def health(self) -> ProviderHealth:
        raise NotImplementedError


# Statuses that mean the upstream produced nothing, so replaying the identical
# request cannot duplicate work.  Other 4xx answers are request defects that a
# retry would only repeat.  529 is Anthropic's ``overloaded_error``.
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504, 529})
_DEFAULT_RETRY_ATTEMPTS = 3
_MAX_RETRY_ATTEMPTS = 8
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
_MAX_RETRY_DELAY_SECONDS = 20.0


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded automatic retries for one provider."""

    attempts: int = _DEFAULT_RETRY_ATTEMPTS
    backoff_seconds: float = _DEFAULT_RETRY_BACKOFF_SECONDS


def _bounded_number(value: Any, fallback: float, *, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        # diagnostic-expected: an unset or unreadable tuning value keeps the default
        return fallback
    if number != number or number < 0:
        return fallback
    return min(number, maximum)


def retry_policy(config: ProviderConfig) -> RetryPolicy:
    """Resolve retry limits from provider options, then the environment.

    One attempt disables retries; operators keep that escape hatch per provider
    (``options.retry_attempts``) and per deployment.
    """

    attempts = _bounded_number(
        config.options.get(
            "retry_attempts", os.getenv("NEBULA_PROVIDER_RETRY_ATTEMPTS")
        ),
        _DEFAULT_RETRY_ATTEMPTS,
        maximum=_MAX_RETRY_ATTEMPTS,
    )
    backoff = _bounded_number(
        config.options.get(
            "retry_backoff_seconds", os.getenv("NEBULA_PROVIDER_RETRY_BACKOFF_SECONDS")
        ),
        _DEFAULT_RETRY_BACKOFF_SECONDS,
        maximum=_MAX_RETRY_DELAY_SECONDS,
    )
    return RetryPolicy(attempts=max(1, int(attempts)), backoff_seconds=backoff)


_DEFAULT_NATIVE_REQUEST_TIMEOUT_SECONDS = 600.0
_MAX_NATIVE_REQUEST_TIMEOUT_SECONDS = 3600.0
_NATIVE_CONNECT_TIMEOUT_SECONDS = 10.0


def _native_request_timeout(config: ProviderConfig) -> float:
    """Seconds one non-streamed native generation may take to answer.

    The Anthropic, Responses, Gemini and Bedrock adapters receive the whole
    generation as one response, so the read timeout bounds the entire answer,
    not the gap between tokens. Operators tune it per provider
    (``options.request_timeout_seconds``) and per deployment; a config built
    with an explicit ``timeout_seconds`` keeps it when neither is set.
    """

    fallback = (
        config.timeout_seconds
        if "timeout_seconds" in config.model_fields_set
        else _DEFAULT_NATIVE_REQUEST_TIMEOUT_SECONDS
    )
    seconds = _bounded_number(
        config.options.get(
            "request_timeout_seconds",
            os.getenv("NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS"),
        ),
        fallback,
        maximum=_MAX_NATIVE_REQUEST_TIMEOUT_SECONDS,
    )
    return seconds if seconds > 0 else fallback


def _native_http_timeout(config: ProviderConfig) -> httpx.Timeout:
    read = _native_request_timeout(config)
    return httpx.Timeout(read, connect=min(_NATIVE_CONNECT_TIMEOUT_SECONDS, read))


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Read a Retry-After header in either seconds or HTTP-date form."""

    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), _MAX_RETRY_DELAY_SECONDS))
    except ValueError:
        # diagnostic-expected: Retry-After may carry an HTTP date instead of seconds
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        # diagnostic-expected: an unreadable Retry-After falls back to backoff
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delay = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(delay, _MAX_RETRY_DELAY_SECONDS))


def _retry_delay(policy: RetryPolicy, attempt: int, retry_after: float | None) -> float:
    delay = min(policy.backoff_seconds * (2 ** (attempt - 1)), _MAX_RETRY_DELAY_SECONDS)
    if retry_after is not None:
        delay = min(max(delay, retry_after), _MAX_RETRY_DELAY_SECONDS)
    # Jitter keeps concurrent turns from resending in lockstep.
    return delay + random.uniform(0.0, delay * 0.25)


def _record_retry(
    config: ProviderConfig,
    *,
    operation: str,
    attempt: int,
    policy: RetryPolicy,
    delay: float,
    status_code: int | None,
) -> None:
    record_diagnostic(
        "warning",
        "providers",
        "providers.request.retried",
        "A transient provider failure was retried automatically.",
        outcome="retrying",
        stage="providers",
        retryable=True,
        safe_failure_cause="The provider was temporarily unavailable.",
        metadata={
            "provider_id": config.id,
            "operation": operation,
            "attempt": attempt,
            "attempts_allowed": policy.attempts,
            "retry_delay_seconds": round(delay, 3),
            "http_status": status_code,
        },
    )


def _exhausted(
    error: ProviderOverloadedError, attempts: int
) -> ProviderOverloadedError:
    """Name the automatic attempts so the operator knows retrying is not new."""

    return ProviderOverloadedError(
        f"{error} after {attempts} attempts",
        status_code=error.status_code,
        retry_after=error.retry_after,
    )


async def _send_with_retry(
    config: ProviderConfig,
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    operation: str,
    retry_transient_statuses: bool = True,
) -> httpx.Response:
    """Send a provider request, retrying only transient upstream failures.

    The caller still raises for the returned response; only an exhausted
    transient failure is raised here, so its message can name the attempts.
    Health probes pass ``retry_transient_statuses=False``: a 429 or 5xx from a
    discovery endpoint is handed back with its status instead of being retried
    to exhaustion, while a refused connection is still retried.
    """

    policy = retry_policy(config)
    attempt = 1
    while True:
        status_code: int | None = None
        try:
            response = await send()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # A connection that was never established cannot have reached the
            # provider, so the same request is safe to send again.
            if attempt >= policy.attempts:
                raise _transport_failure(exc) from exc
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_016",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            delay = _retry_delay(policy, attempt, None)
        except httpx.HTTPError as exc:
            # Anything after the connection opened (a read timeout, a torn
            # body) may have reached the provider: name it, never replay it.
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_018",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            raise _transport_failure(exc) from exc
        else:
            if not response.is_error:
                return response
            error = _safe_error(response)
            if not retry_transient_statuses or not isinstance(
                error, ProviderOverloadedError
            ):
                return response
            if attempt >= policy.attempts:
                if attempt == 1:
                    return response
                raise _exhausted(error, attempt)
            status_code = response.status_code
            delay = _retry_delay(policy, attempt, error.retry_after)
        _record_retry(
            config,
            operation=operation,
            attempt=attempt,
            policy=policy,
            delay=delay,
            status_code=status_code,
        )
        await asyncio.sleep(delay)
        attempt += 1


def _transport_failure(exc: httpx.HTTPError) -> ProviderError:
    """Name a transport failure; ``str(httpx.ReadTimeout(""))`` is empty."""

    reason = redact_text(" ".join(str(exc).split()))[:300]
    kind = type(exc).__name__
    verb = "timed out" if isinstance(exc, httpx.TimeoutException) else "failed"
    return ProviderError(
        f"provider request {verb} ({kind})" + (f": {reason}" if reason else "")
    )


_SSE_LINE_END = re.compile(r"\r\n|\r|\n")


async def _sse_data_frames(response: httpx.Response) -> AsyncGenerator[str, None]:
    """Yield the ``data`` payload of each server-sent event.

    SSE lines end only at LF, CRLF or CR. ``aiter_lines()`` follows
    ``str.splitlines()`` and also breaks at U+2028, U+2029 and U+0085, which a
    gateway serialising with ``ensure_ascii=False`` sends raw inside JSON
    strings; one frame would then arrive as two unparsable halves. Several
    ``data:`` lines in one event are joined with a newline, as the format
    requires; comments and other fields are skipped.
    """

    buffer = ""
    data_lines: list[str] = []

    def take(line: str) -> str | None:
        if not line:
            payload = "\n".join(data_lines) if data_lines else None
            data_lines.clear()
            return payload
        if line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
        return None

    async for chunk in response.aiter_text():
        buffer += chunk
        start = 0
        while True:
            match = _SSE_LINE_END.search(buffer, start)
            if match is None:
                break
            if match.group() == "\r" and match.end() == len(buffer):
                # A trailing CR may be the first half of a CRLF; wait for more.
                break
            payload = take(buffer[start : match.start()])
            start = match.end()
            if payload is not None:
                yield payload
        buffer = buffer[start:]
    tail = buffer.rstrip("\r")
    if tail:
        take(tail)
    if data_lines:
        yield "\n".join(data_lines)


def _frame_has_output(data: Any) -> bool:
    """Whether a Chat Completions chunk carries anything the operator sees."""

    if not isinstance(data, dict):
        return True
    choices = data.get("choices")
    if not isinstance(choices, list):
        return bool(choices)
    for choice in choices:
        if not isinstance(choice, dict):
            return True
        if choice.get("finish_reason"):
            return True
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if (
            delta.get("tool_calls")
            or _openai_message_content(delta)
            or _openai_message_reasoning(delta)
        ):
            return True
    return False


# Role-only and heartbeat frames precede an in-band error; a peek past this
# many frames without output stops looking and hands them to the parser.
_LEADING_FRAME_LIMIT = 32


async def _leading_frames(
    frames: AsyncIterator[str],
) -> tuple[list[str], ProviderOverloadedError | None]:
    """Read frames up to the first output so a leading transient error frame
    can be replayed exactly as the equivalent HTTP status would be."""

    buffered: list[str] = []
    async for encoded in frames:
        buffered.append(encoded)
        stripped = encoded.strip()
        if not stripped:
            continue
        if stripped == "[DONE]":
            return buffered, None
        try:
            data = json.loads(stripped)
        except (
            ValueError
        ):  # diagnostic-expected: the parser reports the malformed frame
            return buffered, None
        failure = _stream_error_frame(data)
        if isinstance(failure, ProviderOverloadedError):
            return buffered, failure
        if (
            failure is not None
            or _frame_has_output(data)
            or len(buffered) >= _LEADING_FRAME_LIMIT
        ):
            return buffered, None
    return buffered, None


async def _replay_frames(
    leading: list[str], frames: AsyncIterator[str]
) -> AsyncIterator[str]:
    for encoded in leading:
        yield encoded
    async for encoded in frames:
        yield encoded


@asynccontextmanager
async def _stream_with_retry(
    config: ProviderConfig,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: dict[str, Any],
    operation: str,
) -> AsyncIterator[AsyncIterator[str]]:
    """Open a streaming provider response and hand over its SSE data frames,
    retrying before the first token.

    A transient failure arrives either as an HTTP status or, once the gateway
    has already sent its 200, as a leading ``{"error": {...}}`` frame; both
    are replayed while attempts remain. Once any output frame has been read
    the stream is never replayed: a partial answer must not be silently
    restarted underneath the operator. Transport failures are raised as
    provider errors that name the failure, since httpx's own text is often
    empty.
    """

    policy = retry_policy(config)
    attempt = 1
    while True:
        started = False
        status_code: int | None = None
        error: ProviderError | None = None
        try:
            async with client.stream(method, url, json=json) as response:
                if response.is_error:
                    await response.aread()
                    error = _safe_error(response)
                else:
                    frames = _sse_data_frames(response)
                    leading, error = await _leading_frames(frames)
                    if error is None:
                        started = True
                        yield _replay_frames(leading, frames)
                        return
                    await frames.aclose()
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if started or attempt >= policy.attempts:
                raise _transport_failure(exc) from exc
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_017",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            delay = _retry_delay(policy, attempt, None)
        except httpx.HTTPError as exc:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_019",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            raise _transport_failure(exc) from exc
        else:
            # A consumed stream already returned, so only an error reaches here.
            assert error is not None
            if not isinstance(error, ProviderOverloadedError):
                raise error
            if attempt >= policy.attempts:
                if attempt == 1:
                    raise error
                raise _exhausted(error, attempt)
            status_code = error.status_code
            delay = _retry_delay(policy, attempt, error.retry_after)
        _record_retry(
            config,
            operation=operation,
            attempt=attempt,
            policy=policy,
            delay=delay,
            status_code=status_code,
        )
        await asyncio.sleep(delay)
        attempt += 1


_CONTEXT_ERROR_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "max_tokens_exceeded",
        "prompt_too_long",
    }
)
_CONTEXT_ERROR_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "prompt too long",
    "too many tokens",
    # Gemini: "The input token count (N) exceeds the maximum number of tokens
    # allowed (M)."; Bedrock: "Input is too long for requested model."
    "input token count",
    "input is too long",
)
# A 429 carrying one of these is spent quota or billing, not a busy upstream.
_QUOTA_ERROR_CODES = frozenset(
    {"insufficient_quota", "billing_not_active", "billing_hard_limit_reached"}
)


def _error_detail(body: Any) -> tuple[str | None, str | None]:
    """Return the operator-readable message and code from a provider error body."""

    error = body.get("error", {}) if isinstance(body, dict) else {}
    detail = (error.get("message") if isinstance(error, dict) else None) or (
        body.get("message") if isinstance(body, dict) else None
    )
    if detail is None and isinstance(error, str) and error.strip():
        detail = error
    raw_code = (error.get("code") if isinstance(error, dict) else None) or (
        body.get("code") if isinstance(body, dict) else None
    )
    error_code = str(raw_code).casefold() if raw_code is not None else None
    # OpenRouter wraps the upstream provider's reason in metadata.raw; the
    # generic "Provider returned error" alone is not actionable.
    metadata = error.get("metadata") if isinstance(error, dict) else None
    upstream = metadata.get("raw") if isinstance(metadata, dict) else None
    if isinstance(upstream, str) and upstream.strip():
        try:
            decoded = json.loads(upstream)
            inner = decoded.get("error") if isinstance(decoded, dict) else None
            upstream = (
                inner.get("message") if isinstance(inner, dict) else None
            ) or upstream
        except ValueError:  # diagnostic-expected: upstream reason is not JSON; the raw text is shown instead
            pass
        upstream = " ".join(str(upstream).split())[:400]
        detail = f"{detail} (upstream: {upstream})" if detail else upstream
    return (str(detail) if detail is not None else None), error_code


def _context_length_error(detail: str | None, error_code: str | None) -> bool:
    normalized = str(detail or "").casefold()
    return error_code in _CONTEXT_ERROR_CODES or any(
        marker in normalized for marker in _CONTEXT_ERROR_MARKERS
    )


def _quota_exhausted(body: Any, detail: str | None, error_code: str | None) -> bool:
    """Whether a rate-limit answer names spent quota or billing instead."""

    error = body.get("error") if isinstance(body, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    codes = {error_code, kind.casefold() if isinstance(kind, str) else None}
    return (
        bool(codes & _QUOTA_ERROR_CODES)
        or "insufficient_quota" in str(detail or "").casefold()
    )


def _safe_error(response: httpx.Response) -> ProviderError:
    request_id = response.headers.get("x-request-id") or response.headers.get(
        "request-id"
    )
    error_code: str | None = None
    body: Any = None
    try:
        body = response.json()
        detail, error_code = _error_detail(body)
    except (ValueError, AttributeError) as caught_error:
        record_caught_exception(
            "providers",
            "providers.providers.caught_failure_004",
            "A handled providers operation raised an exception.",
            caught_error,
            stage="providers",
        )
        detail = None
    suffix = f" request_id={request_id}" if request_id else ""
    message = f"provider returned HTTP {response.status_code}{suffix}" + (
        f": {detail}" if detail else ""
    )
    if response.status_code in {400, 413, 422} and _context_length_error(
        detail, error_code
    ):
        return ProviderContextLengthError(message)
    if response.status_code == 429 and _quota_exhausted(body, detail, error_code):
        # Retrying spent quota only burns attempts and then mislabels the
        # billing cause as a transient overload.
        return ProviderQuotaError(
            f"provider quota or billing limit reached (HTTP 429){suffix}"
            + (f": {detail}" if detail else "")
        )
    if response.status_code in _RETRYABLE_STATUS_CODES:
        return ProviderOverloadedError(
            message,
            status_code=response.status_code,
            retry_after=_retry_after_seconds(response),
        )
    return ProviderError(message)


def _stream_error_frame(data: Any) -> ProviderError | None:
    """Map an in-band error frame on a 200 SSE stream to a provider failure.

    OpenRouter, vLLM and other OpenAI-compatible gateways report an upstream
    failure that happens after the response headers were sent as a
    ``{"error": {...}}`` data frame instead of an HTTP status. Ignoring the
    frame ends the stream as an empty, successful reply. A frame relaying a
    transient status is as replayable as that status would have been.
    """

    if not isinstance(data, dict) or not data.get("error"):
        return None
    detail, error_code = _error_detail(data)
    message = "provider reported an error while streaming" + (
        f": {detail}" if detail else ""
    )
    if _context_length_error(detail, error_code):
        return ProviderContextLengthError(message)
    status = int(error_code) if error_code and error_code.isdigit() else None
    if status == 429 and _quota_exhausted(data, detail, error_code):
        return ProviderQuotaError(message)
    if status in _RETRYABLE_STATUS_CODES:
        return ProviderOverloadedError(message, status_code=status)
    return ProviderError(message)


def _arguments(
    value: Any, *, diagnostic_metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        metadata = {
            **(diagnostic_metadata or {}),
            "format": "json",
        }
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="surrogatepass")
            metadata.update(
                {
                    "byte_count": len(encoded),
                    "fingerprint": hashlib.sha256(encoded).hexdigest(),
                }
            )
        if isinstance(exc, json.JSONDecodeError):
            # The decoder's reason and coordinates identify the malformed
            # shape without retaining the arguments, which may contain a
            # command, credential or other operator data.
            metadata["validation"] = (
                f"{exc.msg}; line {exc.lineno}; column {exc.colno}; character {exc.pos}"
            )
        failure = ProviderError("provider returned malformed tool arguments")
        failure.__cause__ = exc
        record_caught_exception(
            "providers",
            "providers.tool_arguments.invalid_json",
            "A provider returned tool arguments that were not valid JSON.",
            failure,
            stage="tool_arguments",
            metadata=metadata,
            sensitive_detail=(
                f"Exact provider tool arguments (UTF-8):\n{value}"
                if isinstance(value, str)
                else None
            ),
        )
        raise failure from exc
    if not isinstance(parsed, dict):
        raise ProviderError("provider returned non-object tool arguments")
    return parsed


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _openai_usage(usage: Any) -> ModelUsage:
    """Read Chat Completions usage; gateways omit ``total_tokens`` or send null.

    Goal token budgets gate on the total, so a missing one is summed rather
    than recorded as zero.
    """

    if not isinstance(usage, dict):
        return ModelUsage()
    prompt = _token_count(usage.get("prompt_tokens"))
    completion = _token_count(usage.get("completion_tokens"))
    return ModelUsage(
        input_tokens=prompt,
        output_tokens=completion,
        total_tokens=_token_count(usage.get("total_tokens")) or prompt + completion,
    )


def _merge_tool_call_deltas(call_parts: dict[int, dict[str, str]], items: Any) -> None:
    """Fold one chunk's tool-call deltas into the calls assembled so far.

    OpenAI keys fragments by ``index``. Vendors that omit it send whole calls
    per chunk, so those are keyed by ``id`` when it is already known and by
    arrival order otherwise: two calls in one chunk must never merge into one.
    ``id`` and ``name`` are taken once, because vendors that resend them on
    every fragment would otherwise yield ``call_1call_1``.
    """

    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            function = {}
        call_id = str(item.get("id") or "")
        name = str(function.get("name") or "")
        raw_index = item.get("index")
        if isinstance(raw_index, int) and not isinstance(raw_index, bool):
            key = raw_index
        else:
            known = [
                index
                for index, value in call_parts.items()
                if call_id and value["id"] == call_id
            ]
            if known:
                key = known[0]
            elif call_parts and not call_id and not name:
                # An argument fragment without any identity continues the
                # most recent call.
                key = max(call_parts)
            else:
                key = max(call_parts, default=-1) + 1
        current = call_parts.setdefault(key, {"id": "", "name": "", "arguments": ""})
        current["id"] = current["id"] or call_id
        current["name"] = current["name"] or name
        arguments = function.get("arguments")
        current["arguments"] += (
            json.dumps(arguments)
            if isinstance(arguments, dict)
            else str(arguments or "")
        )


def _openai_text_parts(value: Any) -> str:
    """Read visible text from an OpenAI-compatible content value."""

    # Streamed deltas carry meaningful edge whitespace ("the" + " quick"), so
    # text is kept exactly; only empty values are skipped.
    parts: list[str] = []
    if isinstance(value, str) and value:
        parts.append(value)
    elif isinstance(value, list):
        for block in value:
            if isinstance(block, str) and block:
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = block.get("text") or block.get("content")
            if (
                block.get("type") in {None, "text", "output_text"}
                and isinstance(text, str)
                and text
            ):
                parts.append(text)
    return "\n".join(parts)


def _openai_message_content(message: dict[str, Any]) -> str:
    """Reply text only. Reasoning fields are not a substitute for content."""

    return _openai_text_parts(message.get("content"))


def _openrouter_reasoning(
    request: "ModelRequest", supported: set[str]
) -> dict[str, Any]:
    """The reasoning object OpenRouter is sent for one request.

    ``exclude: False`` asks for the thoughts to come back, which is what the
    transcript shows the operator. An effort or a token ceiling is only added
    when the route advertises the control: sending one it does not take costs
    the request, and with ``require_parameters`` it can leave no eligible
    endpoint at all.
    """

    reasoning: dict[str, Any] = {"exclude": False}
    if "reasoning" not in supported and supported:
        return reasoning
    if request.reasoning_effort is not None:
        reasoning["effort"] = request.reasoning_effort
    if request.reasoning_max_tokens is not None:
        reasoning["max_tokens"] = request.reasoning_max_tokens
    return reasoning


def _openai_message_reasoning(message: dict[str, Any]) -> str:
    """Model thoughts from OpenRouter/OpenAI-compatible reasoning channels.

    The channels are alternative encodings of the same thought: OpenRouter sends
    each fragment in both `reasoning` and `reasoning_details`. Read only the first
    channel that carries text, or every streamed token would be repeated.
    """

    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    parts: list[str] = []
    details = message.get("reasoning_details")
    if isinstance(details, list):
        for item in details:
            if isinstance(item, str) and item:
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if "encrypted" in kind:
                continue
            text = item.get("text") or item.get("summary") or item.get("content")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _openai_message_text(message: dict[str, Any]) -> str:
    """Reply text for OpenAI-compatible messages. Thoughts stay separate."""

    return _openai_message_content(message)


def _vllm_grammar_schema(value: Any) -> Any:
    """Remove validation-only keywords unsupported by vLLM's grammar compiler.

    Nebula retains and validates the original schema at the broker boundary; this
    copy is only the provider-facing grammar used to constrain model output.
    """

    if isinstance(value, dict):
        return {
            key: _vllm_grammar_schema(item)
            for key, item in value.items()
            if key != "uniqueItems"
        }
    if isinstance(value, list):
        return [_vllm_grammar_schema(item) for item in value]
    return value


# Reasoning families reject sampling parameters: OpenAI answers 400
# "Unsupported parameter: 'temperature'" for the o-series and gpt-5 models.
_OPENAI_REASONING_MODEL = re.compile(r"^(?:o\d+|gpt-5)(?:[-.]|$)")


def _openai_reasoning_model(model: str) -> bool:
    return _OPENAI_REASONING_MODEL.match(model.rsplit("/", 1)[-1]) is not None


_STRICT_SCHEMA_MAPPINGS = frozenset(
    {"properties", "$defs", "definitions", "patternProperties"}
)
_STRICT_SCHEMA_DATA = frozenset({"enum", "const", "examples", "required"})


def _openai_strict_schema(schema: Any) -> bool:
    """Whether OpenAI strict mode accepts a schema as written.

    Strict mode requires every property to be listed in ``required`` and every
    object to forbid additional properties, and rejects ``default`` and
    ``uniqueItems``. Nebula keeps the real schema and validates arguments at
    the broker, so a schema strict mode would reject is sent without strict
    instead of failing the whole request.
    """

    if isinstance(schema, list):
        return all(_openai_strict_schema(item) for item in schema)
    if not isinstance(schema, dict):
        return True
    if "default" in schema or "uniqueItems" in schema:
        return False
    properties = schema.get("properties")
    kind = schema.get("type")
    is_object = kind == "object" or (isinstance(kind, list) and "object" in kind)
    if is_object or isinstance(properties, dict):
        required = schema.get("required") or []
        if (
            not isinstance(required, list)
            or schema.get("additionalProperties") is not False
        ):
            return False
        if isinstance(properties, dict) and set(properties) - set(required):
            return False
    for key, value in schema.items():
        if key in _STRICT_SCHEMA_DATA:
            continue
        if key in _STRICT_SCHEMA_MAPPINGS:
            if isinstance(value, dict) and not all(
                _openai_strict_schema(item) for item in value.values()
            ):
                return False
            continue
        if not _openai_strict_schema(value):
            return False
    return True


class OpenAIResponsesProvider(ModelProvider):
    """OpenAI Responses API adapter.

    Function tools use the flattened Responses shape documented by OpenAI;
    commands are never parsed out of prose.
    """

    def _headers(self) -> dict[str, str]:
        return self._bearer_or_key_headers()

    def _payload(self, request: ModelRequest, model: str) -> dict[str, Any]:
        wire_names = _wire_tool_names(request)
        payload: dict[str, Any] = {
            "model": model,
            "input": [
                _openai_responses_message(message) for message in request.messages
            ],
            # Nebula replays history itself and never reads stored items, so
            # engagement data is not kept server-side by default.
            "store": False,
        }
        for result in request.tool_results:
            payload["input"].extend(
                [
                    {
                        "type": "function_call",
                        "call_id": result.call_id,
                        "name": wire_names.get(result.name, result.name),
                        "arguments": json.dumps(result.arguments, sort_keys=True),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": result.call_id,
                        "output": (
                            json.dumps(result.output, sort_keys=True)
                            if isinstance(result.output, dict)
                            else result.output
                        ),
                    },
                ]
            )
        if request.instructions:
            payload["instructions"] = request.instructions
        if request.max_output_tokens:
            payload["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None and not _openai_reasoning_model(model):
            payload["temperature"] = request.temperature
        if request.tools:
            payload["parallel_tool_calls"] = request.parallel_tool_calls
            payload["tools"] = [
                {
                    "type": "function",
                    "name": wire_names.get(tool.name, tool.name),
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    # Strict mode is a request-level contract: one schema it
                    # rejects fails every tool in the call.
                    **(
                        {"strict": True}
                        if tool.strict and _openai_strict_schema(tool.input_schema)
                        else {}
                    ),
                }
                for tool in request.tools
            ]
            if request.tool_choice == ToolChoice.REQUIRED:
                payload["tool_choice"] = "required"
        if request.response_schema:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "nebula_response",
                    "strict": _openai_strict_schema(request.response_schema),
                    "schema": request.response_schema,
                }
            }
        if request.metadata:
            payload["metadata"] = request.metadata
        return payload

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        async with self._client(
            self._headers(), timeout=_native_http_timeout(self.config)
        ) as client:
            response = await self._post(
                client,
                self._path("/v1/responses"),
                self._payload(request, model),
                operation="responses",
            )
        if response.is_error:
            raise _safe_error(response)
        data = response.json()
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for item in data.get("output", []):
            if item.get("type") == "function_call":
                calls.append(
                    _normalized_tool_call(
                        id=item.get("call_id") or item.get("id", ""),
                        name=_decode_tool_name(request, item.get("name", "")),
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
            raw_body=response.content,
        )

    async def health(self) -> ProviderHealth:
        try:
            async with self._client(self._headers()) as client:
                response = await client.get(self._path("/v1/models"))
            if response.is_error:
                raise _safe_error(response)
            models = [
                item["id"]
                for item in response.json().get("data", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=models
            )
        except Exception as exc:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_006",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )


_WIRE_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _wire_tool_names(request: ModelRequest) -> dict[str, str]:
    """Map Nebula tool names to names every vendor accepts.

    Nebula names may contain dots (``tool_output.search``); OpenAI (Chat
    Completions and Responses), Anthropic and Bedrock accept only
    ``[a-zA-Z0-9_-]{1,64}``. The mapping is derived from the request alone, so
    responses and replayed history decode identically.
    """

    names = sorted(
        {tool.name for tool in request.tools}
        | {result.name for result in request.tool_results}
    )
    mapping: dict[str, str] = {}
    used: set[str] = {name for name in names if _WIRE_TOOL_NAME.match(name)}
    for name in names:
        if _WIRE_TOOL_NAME.match(name):
            mapping[name] = name
            continue
        wire = re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:64] or "tool"
        if wire in used:
            digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
            wire = f"{wire[:55]}_{digest}"
        used.add(wire)
        mapping[name] = wire
    return mapping


def _decode_tool_name(request: ModelRequest, wire: str) -> str:
    reverse = {value: key for key, value in _wire_tool_names(request).items()}
    return reverse.get(wire, wire)


# Mistral AI model families, served by Mistral itself or by any runtime that
# applies Mistral's chat template (vLLM, OpenRouter, a gateway).
_MISTRAL_MODEL = re.compile(
    r"mistral|mixtral|codestral|devstral|pixtral|magistral|ministral|voxtral",
    re.IGNORECASE,
)
_MISTRAL_TOOL_CALL_ID = re.compile(r"[a-zA-Z0-9]{9}")
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _mistral_tool_call_id(call_id: str) -> str:
    """A tool-call id Mistral accepts: exactly nine ``[a-zA-Z0-9]`` characters.

    Replayed ids can come from DSML recovery or from another provider after a
    mid-chat model switch. An id Mistral issued is kept; any other maps to a
    digest of itself, so the call and its result still pair up and every
    request of a turn replays the same id.
    """

    if _MISTRAL_TOOL_CALL_ID.fullmatch(call_id):
        return call_id
    number = int.from_bytes(hashlib.sha256(call_id.encode("utf-8")).digest(), "big")
    characters = []
    for _ in range(9):
        number, index = divmod(number, 62)
        characters.append(_BASE62[index])
    return "".join(characters)


def _mistral_tool_call_ids(messages: list[dict[str, Any]]) -> None:
    for message in messages:
        for call in message.get("tool_calls") or ():
            if isinstance(call, dict) and isinstance(call.get("id"), str):
                call["id"] = _mistral_tool_call_id(call["id"])
        if message.get("role") == "tool" and isinstance(
            message.get("tool_call_id"), str
        ):
            message["tool_call_id"] = _mistral_tool_call_id(message["tool_call_id"])


class OpenAICompatibleProvider(ModelProvider):
    """Adapter for Chat Completions-compatible hosted and local runtimes."""

    def _headers(self) -> dict[str, str]:
        return self._bearer_or_key_headers()

    def _payload(self, request: ModelRequest, model: str) -> dict[str, Any]:
        vllm_grammar = self.config.flavor == ProviderFlavor.VLLM
        wire_names = _wire_tool_names(request)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_openai_chat_message(message) for message in request.messages],
        }
        for result in request.tool_results:
            payload["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": result.call_id,
                                "type": "function",
                                "function": {
                                    "name": wire_names.get(result.name, result.name),
                                    "arguments": json.dumps(
                                        result.arguments, sort_keys=True
                                    ),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "content": (
                            json.dumps(result.output, sort_keys=True)
                            if isinstance(result.output, dict)
                            else result.output
                        ),
                    },
                ]
            )
        if request.instructions:
            payload["messages"].insert(
                0, {"role": "system", "content": request.instructions}
            )
        openrouter = self.config.flavor == ProviderFlavor.OPENROUTER
        if request.max_output_tokens:
            # OpenAI's reasoning families answer 400 to max_tokens ("use
            # max_completion_tokens instead"), directly and via Azure/Foundry
            # or a gateway. OpenRouter normalizes max_tokens, the name its
            # routes advertise, and translates it for every upstream.
            ceiling = (
                "max_completion_tokens"
                if not openrouter and _openai_reasoning_model(model)
                else "max_tokens"
            )
            payload[ceiling] = request.max_output_tokens
        if request.temperature is not None and not _openai_reasoning_model(model):
            payload["temperature"] = request.temperature
        if (
            not openrouter
            and request.reasoning_effort is not None
            and _openai_reasoning_model(model)
        ):
            # OpenAI's own reasoning families take the effort as a scalar.
            # Other OpenAI-compatible endpoints advertise no such control, so
            # they are left alone rather than sent a parameter they may reject.
            payload["reasoning_effort"] = request.reasoning_effort
        if request.tools:
            if openrouter:
                # Prevent OpenRouter from selecting an endpoint that drops a
                # parameter Nebula relies on for its verified tool contract.
                # No OpenRouter route advertises parallel_tool_calls, so
                # sending it with require_parameters matches no endpoint. A
                # route is free to batch calls; callers accept a batch and
                # execute it one call at a time.
                payload["provider"] = {"require_parameters": True}
            else:
                payload["parallel_tool_calls"] = request.parallel_tool_calls
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": wire_names.get(tool.name, tool.name),
                        "description": tool.description,
                        "parameters": (
                            _vllm_grammar_schema(tool.input_schema)
                            if vllm_grammar
                            else tool.input_schema
                        ),
                        # Strict mode is a request-level contract: one schema
                        # it rejects fails every tool in the call.
                        "strict": tool.strict
                        and _openai_strict_schema(tool.input_schema),
                    },
                }
                for tool in request.tools
            ]
            if request.tool_choice == ToolChoice.REQUIRED:
                payload["tool_choice"] = "required"
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "nebula_response",
                    # A schema strict mode rejects (optional properties,
                    # defaults) fails the request; callers validate the reply.
                    "strict": _openai_strict_schema(request.response_schema),
                    "schema": (
                        _vllm_grammar_schema(request.response_schema)
                        if vllm_grammar
                        else request.response_schema
                    ),
                },
            }
        if openrouter:
            # OpenRouter uses this as a sticky-routing key. A Nebula chat turn
            # can span several tool and synthesis requests, so keep those
            # requests on one upstream route instead of letting each hop choose
            # a different provider implementation.
            session_id = request.metadata.get("chat_session_id")
            if session_id:
                payload["session_id"] = session_id
            allowed = self.openrouter_allowed_providers
            if allowed:
                # Operator-selected upstream providers: never route elsewhere.
                payload["provider"] = {**payload.get("provider", {}), "only": allowed}
            payload["reasoning"] = _openrouter_reasoning(
                request, set(self.config.model_parameters.get(model, ()))
            )
            # OpenRouter compresses the middle of an oversized prompt by default
            # on endpoints of 8K or less. Nebula sizes its own context and
            # recovers from a rejected request, so take the error over silent
            # truncation of material the caller believes was sent.
            payload["plugins"] = [{"id": "context-compression", "enabled": False}]
            if request.tools:
                # With require_parameters, any optional parameter the exact
                # model does not advertise leaves no eligible endpoint.
                supported = set(self.config.model_parameters.get(model, ()))
                for optional in ("reasoning", "temperature"):
                    if optional in payload and optional not in supported:
                        payload.pop(optional)
                if supported:
                    # The tool contract's own controls and the ceiling are
                    # dropped only when the catalog says the route lacks them.
                    # A routing step that returns no call is recoverable; a
                    # request no endpoint accepts (404) fails the turn.
                    for optional in ("tool_choice", "max_tokens"):
                        if optional in payload and optional not in supported:
                            payload.pop(optional)
                    # A json_schema response_format is advertised as
                    # structured_outputs alongside response_format.
                    structured = {"response_format", "structured_outputs"}
                    if "response_format" in payload and not structured <= supported:
                        payload.pop("response_format")
        if self.config.flavor == ProviderFlavor.MISTRAL or _MISTRAL_MODEL.search(model):
            _mistral_tool_call_ids(payload["messages"])
        return payload

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        payload = self._payload(request, model)
        async with self._client(self._headers()) as client:
            response = await self._post(
                client,
                self._path("/v1/chat/completions"),
                payload,
                operation="chat_completions",
            )
        if response.is_error:
            raise _safe_error(response)
        data = response.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for item in message.get("tool_calls", []):
            function = item.get("function", {})
            name = _decode_tool_name(request, function.get("name", ""))
            calls.append(
                _normalized_tool_call(
                    id=item.get("id", ""),
                    name=name,
                    arguments=_arguments(
                        function.get("arguments"),
                        diagnostic_metadata={
                            "adapter": self.config.flavor.value,
                            "model_id": data.get("model") or model,
                            "provider": self.config.id,
                            "status": choice.get("finish_reason"),
                            "tool_id": name,
                            "vendor_request_id": data.get("id"),
                        },
                    ),
                )
            )
        return ModelResponse(
            provider_id=self.config.id,
            model=data.get("model") or model,
            text=_openai_message_content(message).strip(),
            reasoning=_openai_message_reasoning(message).strip(),
            tool_calls=calls,
            usage=_openai_usage(data.get("usage")),
            finish_reason=choice.get("finish_reason"),
            provider_request_id=data.get("id"),
            raw=data,
            raw_body=response.content,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        async for event in _stream_openai_compatible(self, request):
            yield event

    async def health(self) -> ProviderHealth:
        if self.config.flavor == ProviderFlavor.OPENROUTER:
            return await self._openrouter_health()
        try:
            async with self._client(self._headers()) as client:
                response = await _send_with_retry(
                    self.config,
                    lambda: client.get(self._path("/v1/models")),
                    operation="model_discovery",
                )
            if response.is_error:
                raise _safe_error(response)
            models = [
                item["id"]
                for item in response.json().get("data", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ]
            return ProviderHealth(
                provider_id=self.config.id, healthy=True, models=models
            )
        except Exception as exc:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_007",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )

    async def _openrouter_health(self) -> ProviderHealth:
        """Use account visibility without ever substituting the public catalog."""
        try:
            async with asyncio.timeout(30):
                async with self._client(self._headers()) as client:
                    key_response = await _send_with_retry(
                        self.config,
                        lambda: client.get(self._path("/v1/key"), timeout=10.0),
                        operation="openrouter_credential_verification",
                        retry_transient_statuses=False,
                    )
                    if key_response.is_error:
                        return ProviderHealth(
                            provider_id=self.config.id,
                            healthy=False,
                            credential_verified=False,
                            detail=(
                                f"OpenRouter credential verification failed (HTTP {key_response.status_code}). "
                                "Check the credential, then retry."
                            ),
                        )
                    key_payload = key_response.json()
                    key_data = (
                        key_payload.get("data")
                        if isinstance(key_payload, dict)
                        else None
                    )
                    if not isinstance(key_data, dict):
                        raise ValueError("Invalid OpenRouter key response")
                    response = await _send_with_retry(
                        self.config,
                        lambda: client.get(self._path("/v1/models/user"), timeout=10.0),
                        operation="openrouter_model_discovery",
                        retry_transient_statuses=False,
                    )
                    directory: httpx.Response | None = None
                    directory_failure: str | None = None
                    try:
                        if not response.is_error:
                            directory = await client.get(
                                self._path("/v1/providers"), timeout=10.0
                            )
                    except httpx.HTTPError:  # diagnostic-expected: reported below when an allowlist needs the directory; routing is unaffected
                        directory_failure = "could not be reached"
                    upstream: list[UpstreamProvider] = []
                    try:
                        if directory is not None and directory.is_error:
                            directory_failure = f"failed (HTTP {directory.status_code})"
                        elif directory is not None:
                            upstream = openrouter_upstream_providers(directory.json())
                    except ValueError:  # diagnostic-expected: reported below when an allowlist needs the directory; routing is unaffected
                        directory_failure = "was unreadable"
                    served: set[str] | None = None
                    if (
                        not response.is_error
                        and self.openrouter_allowed_providers
                        and directory_failure is None
                    ):
                        served = await self._openrouter_models_served_by(
                            client, {item.slug for item in upstream}
                        )
                    alias_targets: dict[str, str] = {}
                    try:
                        if not response.is_error:
                            alias_targets = await self._openrouter_alias_targets(
                                client, response.json()
                            )
                    except (
                        httpx.HTTPError,
                        ValueError,
                    ):  # diagnostic-expected: aliases keep the conservative cap; routing is unaffected
                        alias_targets = {}
                if response.is_error:
                    return ProviderHealth(
                        provider_id=self.config.id,
                        healthy=False,
                        credential_verified=True,
                        catalog_source="openrouter:/models/user",
                        detail=(
                            f"OpenRouter model discovery failed (HTTP {response.status_code}). "
                            "Check credentials and account access, then refresh."
                        ),
                    )
                if directory_failure is not None and self.openrouter_allowed_providers:
                    # An empty directory would filter out every model and blame
                    # the operator's allowlist; fail closed like /models/user.
                    return ProviderHealth(
                        provider_id=self.config.id,
                        healthy=False,
                        credential_verified=True,
                        catalog_source="openrouter:/models/user",
                        detail=(
                            f"The OpenRouter provider directory {directory_failure}, "
                            "so the upstream provider allowlist could not be applied. "
                            "The model list was left unchanged; refresh to retry."
                        ),
                    )
                descriptors = openrouter_models(response.json())
                # The account catalog omits alias_target, so aliases arrive without
                # the one field endpoint discovery needs.
                descriptors = [
                    item.model_copy(update={"alias_target": alias_targets[item.id]})
                    if item.alias_target is None and item.id in alias_targets
                    else item
                    for item in descriptors
                ]
                detail = "Account model catalog loaded; inference is not yet verified."
                if served is not None:
                    descriptors = [item for item in descriptors if item.id in served]
                    detail = (
                        "Showing only models served by the allowed upstream providers; "
                        "inference is not yet verified."
                    )
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=True,
                models=[model.id for model in descriptors],
                model_descriptors=descriptors,
                upstream_providers=upstream,
                credential_verified=True,
                catalog_source="openrouter:/models/user",
                key_expires_at=(
                    key_data.get("expires_at")
                    if isinstance(key_data.get("expires_at"), str)
                    else None
                ),
                key_limit_remaining=(
                    float(key_data["limit_remaining"])
                    if isinstance(key_data.get("limit_remaining"), (int, float))
                    and not isinstance(key_data.get("limit_remaining"), bool)
                    else None
                ),
                detail=detail,
            )
        except (
            httpx.HTTPError,
            ProviderError,
            TimeoutError,
            ValueError,
        ):  # diagnostic-expected: failure is reported as unhealthy; raw text may contain credentials
            # Transport and upstream payload exceptions may contain credentials;
            # an exhausted connection retry arrives as a ProviderError.
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=False,
                detail=(
                    "OpenRouter model discovery failed after bounded retries. "
                    "Check the connection and refresh."
                ),
            )

    async def _openrouter_public_catalog(
        self, client: httpx.AsyncClient, params: dict[str, str]
    ) -> list[ModelDescriptor]:
        """Page the public catalog. Membership still comes from /models/user."""

        models: list[ModelDescriptor] = []
        for _page in range(20):
            response = await client.get(
                self._path("/v1/models"), params=params, timeout=10.0
            )
            response.raise_for_status()
            payload = response.json()
            models.extend(openrouter_models(payload))
            links = payload.get("links") if isinstance(payload, dict) else None
            following = links.get("next") if isinstance(links, dict) else None
            offset = (
                httpx.URL(following).params.get("offset")
                if isinstance(following, str)
                else None
            )
            if not offset:
                break
            params = {**params, "offset": offset}
        return models

    async def _openrouter_models_served_by(
        self, client: httpx.AsyncClient, directory: set[str]
    ) -> set[str]:
        """Model ids at least one allowed upstream provider serves.

        OpenRouter ignores unknown slugs in the `providers` filter and returns the
        whole catalog when none are known, so only directory slugs are sent and an
        empty match yields no models instead of every model.
        """

        known = [
            slug for slug in self.openrouter_allowed_providers if slug in directory
        ]
        if not known:
            return set()
        params = {"providers": ",".join(known)}
        return {
            item.id for item in await self._openrouter_public_catalog(client, params)
        }

    async def _openrouter_alias_targets(
        self, client: httpx.AsyncClient, account_payload: Any
    ) -> dict[str, str]:
        """Exact model each alias in the account catalog redirects to.

        `/models/user` describes aliases without `alias_target`, so the model an
        alias serves is only published in the public catalog. Nothing here decides
        which models the operator may use; it fills one missing field on models
        the account catalog already returned.
        """

        data = (
            account_payload.get("data") if isinstance(account_payload, dict) else None
        )
        aliases = {
            item["id"]
            for item in (data if isinstance(data, list) else [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].startswith("~")
        }
        if not aliases:
            return {}
        return {
            item.id: item.alias_target
            for item in await self._openrouter_public_catalog(client, {})
            if item.id in aliases and item.alias_target
        }

    @property
    def openrouter_allowed_providers(self) -> list[str]:
        """Upstream provider slugs the operator allows (empty means any)."""

        raw = self.config.options.get("openrouter_providers")
        if not isinstance(raw, list):
            return []
        return list(
            dict.fromkeys(
                item.strip().lower()
                for item in raw
                if isinstance(item, str) and item.strip()
            )
        )

    async def openrouter_route_limits(self, model: str) -> list[ModelRouteDescriptor]:
        """Load the exact automatic-routing endpoint set for one model."""

        if self.config.flavor != ProviderFlavor.OPENROUTER:
            raise UnsupportedCapability(
                "route limits are available only for OpenRouter"
            )
        author, separator, slug = model.partition("/")
        if not separator or not author or not slug or len(model) > 500:
            raise ProviderError(
                "OpenRouter route discovery requires an exact model slug"
            )
        endpoint = (
            f"/v1/models/{quote(author, safe='')}/{quote(slug, safe=':')}/endpoints"
        )
        try:
            async with asyncio.timeout(15):
                async with self._client(self._headers()) as client:
                    response = await client.get(self._path(endpoint), timeout=10.0)
            if response.is_error:
                raise ProviderError(
                    f"OpenRouter endpoint discovery failed (HTTP {response.status_code})"
                )
            routes = openrouter_model_routes(response.json(), model=model)
            allowed = self.openrouter_allowed_providers
            if allowed:
                routes = [route for route in routes if route.provider_slug in allowed]
                if not routes:
                    raise ProviderError(
                        "None of the allowed OpenRouter providers serve this model; "
                        "choose another model or allow more providers"
                    )
            if not routes:
                if model.startswith("~"):
                    raise ProviderError(
                        "OpenRouter alias models publish no endpoints of their own; "
                        "refresh the provider so the alias target is known"
                    )
                raise ProviderError("OpenRouter returned no eligible model endpoints")
            return routes
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise ProviderError("OpenRouter endpoint discovery failed") from exc


async def _stream_openai_compatible(
    provider: OpenAICompatibleProvider, request: ModelRequest
) -> AsyncIterator[ModelStreamEvent]:
    """Stream Chat Completions SSE for vLLM and compatible runtimes."""

    model = provider.require(request)
    payload = provider._payload(request, model)
    payload.update({"stream": True, "stream_options": {"include_usage": True}})
    yield ModelStreamEvent(type=StreamEventType.STARTED)
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    call_parts: dict[int, dict[str, str]] = {}
    usage = ModelUsage()
    finish_reason: str | None = None
    response_id: str | None = None
    response_model = model
    terminated = False
    try:
        async with provider._client(provider._headers()) as client:
            async with _stream_with_retry(
                provider.config,
                client,
                "POST",
                provider._path("/v1/chat/completions"),
                json=payload,
                operation="chat_completions_stream",
            ) as frames:
                async for frame in frames:
                    encoded = frame.strip()
                    if not encoded:
                        continue
                    if encoded == "[DONE]":
                        terminated = True
                        continue
                    data = json.loads(encoded)
                    failure = _stream_error_frame(data)
                    if failure is not None:
                        raise failure
                    # Usage-only frames may carry an explicit null; it must not
                    # erase the id and model an earlier frame named.
                    response_id = data.get("id") or response_id
                    response_model = data.get("model") or response_model
                    chunk_usage = data.get("usage")
                    if chunk_usage:
                        usage = _openai_usage(chunk_usage)
                    choice = (data.get("choices") or [{}])[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    reasoning = _openai_message_reasoning(delta)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                        yield ModelStreamEvent(
                            type=StreamEventType.REASONING_DELTA, delta=reasoning
                        )
                    content = _openai_message_content(delta)
                    if content:
                        text_parts.append(content)
                        yield ModelStreamEvent(
                            type=StreamEventType.TEXT_DELTA, delta=content
                        )
                    _merge_tool_call_deltas(call_parts, delta.get("tool_calls"))
        if not terminated and finish_reason is None:
            # Chat Completions ends every reply with a finish_reason and then
            # [DONE]; a body that closes cleanly without either was cut short
            # (a proxy idle timeout) and must not be stored as a whole answer.
            raise ProviderError(
                "provider stream ended before the reply completed: no finish "
                "reason or [DONE] frame arrived"
            )
        calls: list[ToolCall] = []
        for _, value in sorted(call_parts.items()):
            name = _decode_tool_name(request, value["name"])
            calls.append(
                _normalized_tool_call(
                    id=value["id"],
                    name=name,
                    arguments=_arguments(
                        value["arguments"],
                        diagnostic_metadata={
                            "adapter": provider.config.flavor.value,
                            "model_id": response_model,
                            "provider": provider.config.id,
                            "status": finish_reason,
                            "tool_id": name,
                            "vendor_request_id": response_id,
                        },
                    ),
                )
            )
        for call in calls:
            yield ModelStreamEvent(type=StreamEventType.TOOL_CALL, tool_call=call)
        final = ModelResponse(
            provider_id=provider.config.id,
            model=response_model,
            text="".join(text_parts).strip(),
            reasoning="".join(reasoning_parts).strip(),
            tool_calls=calls,
            usage=usage,
            finish_reason=finish_reason,
            provider_request_id=response_id,
        )
        yield ModelStreamEvent(type=StreamEventType.COMPLETED, response=final)
    except asyncio.CancelledError as caught_error:
        record_caught_exception(
            "providers",
            "providers.providers.caught_failure_008",
            "A handled providers operation raised an exception.",
            caught_error,
            stage="providers",
        )
        raise
    except Exception as exc:
        record_caught_exception(
            "providers",
            "providers.providers.caught_failure_009",
            "A handled providers operation raised an exception.",
            exc,
            stage="providers",
        )
        yield ModelStreamEvent(
            type=StreamEventType.ERROR,
            error=str(exc),
            # Chat recovers from an overflow (compact, then retry once) only
            # when the event says so; the non-streaming fallback already does.
            context_length_exceeded=isinstance(exc, ProviderContextLengthError),
            # A transient upstream failure is the operator's to retry; chat
            # reports it as a provider overload rather than a chat defect.
            retryable=isinstance(exc, ProviderOverloadedError),
        )


# ``claude-<family>-<major>[-<minor>]`` anywhere after a Bedrock provider or
# region prefix (``anthropic.``, ``us.anthropic.``) or an ARN path.
_CLAUDE_MODEL = re.compile(
    r"(?:^|\.)claude-(opus|sonnet|haiku|fable|mythos)-"
    r"(?:(preview)|(\d+)(?:[-.](\d{1,2}))?(?![0-9]))"
)


def _claude_model(model: str) -> tuple[str, tuple[int, int] | None] | None:
    """Family and version of a Claude model id; ``None`` for a preview."""

    match = _CLAUDE_MODEL.search(model.casefold().rsplit("/", 1)[-1])
    if match is None:
        return None
    family, preview, major, minor = match.groups()
    if preview:
        return family, None
    return family, (int(major), int(minor or 0))


def _claude_rejects_sampling(model: str) -> bool:
    """Opus 4.7+, Sonnet 5+, Fable and Mythos return 400 for ``temperature``."""

    claude = _claude_model(model)
    if claude is None:
        return False
    family, version = claude
    if family in {"fable", "mythos"}:
        return True
    if version is None:
        return False
    return (family == "opus" and version >= (4, 7)) or (
        family == "sonnet" and version >= (5, 0)
    )


def rejects_forced_tool_choice(model: str) -> bool:
    """Fable 5.1, Mythos 5.1 and Mythos Preview reject ``any`` and ``tool``.

    Adapters send ``auto`` instead; the caller still validates the call.
    """

    claude = _claude_model(model)
    if claude is None or claude[0] not in {"fable", "mythos"}:
        return False
    return claude[1] is None or claude[1] >= (5, 1)


def _claude_thinks_by_default(model: str) -> bool:
    """Opus 5 and Sonnet 5 think by default and accept ``thinking: disabled``."""

    claude = _claude_model(model)
    return (
        claude is not None
        and claude[0] in {"opus", "sonnet"}
        and claude[1] is not None
        and claude[1] >= (5, 0)
    )


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
        wire_names = _wire_tool_names(request)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_output_tokens or 4096,
            "messages": [
                _anthropic_message(message)
                for message in request.messages
                if message.role != "system"
            ],
        }
        for result in request.tool_results:
            payload["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": result.call_id,
                                "name": wire_names.get(result.name, result.name),
                                "input": result.arguments,
                            }
                        ],
                    },
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
                    },
                ]
            )
        systems = [m.content for m in request.messages if m.role == "system"]
        if request.instructions or systems:
            payload["system"] = "\n".join(
                [str(item) for item in [*systems, request.instructions] if item]
            )
        if request.temperature is not None and not _claude_rejects_sampling(model):
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "name": wire_names.get(tool.name, tool.name),
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
            if request.tool_choice == ToolChoice.REQUIRED:
                payload["tool_choice"] = {
                    "type": "auto" if rejects_forced_tool_choice(model) else "any",
                    "disable_parallel_tool_use": not request.parallel_tool_calls,
                }
        async with self._client(
            self._headers(), timeout=_native_http_timeout(self.config)
        ) as client:
            response = await self._post(
                client,
                self._path("/v1/messages"),
                payload,
                operation="messages",
            )
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
                    _normalized_tool_call(
                        id=block.get("id", ""),
                        name=_decode_tool_name(request, block.get("name", "")),
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
        try:
            models: list[str] = []
            params: dict[str, str] = {"limit": "1000"}
            async with self._client(self._headers()) as client:
                # The list is paged (20 rows by default); a model past the
                # first page must not look like one the account lost.
                for _page in range(20):
                    response = await client.get(self._path("/v1/models"), params=params)
                    if response.is_error:
                        raise _safe_error(response)
                    payload = response.json()
                    rows = payload.get("data") if isinstance(payload, dict) else None
                    models.extend(
                        item["id"]
                        for item in (rows if isinstance(rows, list) else [])
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                    )
                    last_id = (
                        payload.get("last_id") if isinstance(payload, dict) else None
                    )
                    if (
                        not isinstance(payload, dict)
                        or not payload.get("has_more")
                        or not isinstance(last_id, str)
                        or not last_id
                    ):
                        break
                    params = {**params, "after_id": last_id}
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=True,
                models=list(dict.fromkeys(models)),
            )
        except Exception as exc:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_010",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )


# Gemini's REST function calls usually carry no ``id``; Nebula needs one to
# pair a call with its result. Ids Nebula made up are never echoed to Gemini.
_GEMINI_SYNTHETIC_CALL_ID = "nebula-gemini-call:"


def _gemini_call_scope(response_id: Any) -> str:
    """Scope made-up call ids to one response.

    ``responseId`` is optional. Without a fresh scope per response, every
    step's first call would get the same id, and a later step would look like
    a replay of an earlier one.
    """

    if isinstance(response_id, str) and response_id:
        return response_id
    return uuid.uuid4().hex


def _gemini_call_id(value: Any, scope: str, index: int) -> str:
    if isinstance(value, str) and value:
        return value
    return f"{_GEMINI_SYNTHETIC_CALL_ID}{scope}:{index}"


def _gemini_call_identity(call_id: str) -> dict[str, str]:
    if call_id.startswith(_GEMINI_SYNTHETIC_CALL_ID):
        return {}
    return {"id": call_id}


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
            parts = _gemini_parts(message.content)
            contents.append({"role": role, "parts": parts})
        for result in request.tool_results:
            contents.extend(
                [
                    {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    **_gemini_call_identity(result.call_id),
                                    "name": result.name,
                                    "args": result.arguments,
                                }
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    **_gemini_call_identity(result.call_id),
                                    "name": result.name,
                                    "response": (
                                        result.output
                                        if isinstance(result.output, dict)
                                        else {"output": result.output}
                                    ),
                                }
                            }
                        ],
                    },
                ]
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
                            # ``parameters`` is Gemini's OpenAPI subset and
                            # rejects empty objects, ``const`` and
                            # ``additionalProperties``; this takes JSON Schema.
                            "parametersJsonSchema": tool.input_schema,
                        }
                        for tool in request.tools
                    ]
                }
            ]
            if request.tool_choice == ToolChoice.REQUIRED:
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
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
        async with self._client(
            self._headers(), timeout=_native_http_timeout(self.config)
        ) as client:
            response = await self._post(
                client,
                self._model_path(model, "generateContent"),
                payload,
                operation="generate_content",
            )
        if response.is_error:
            raise _safe_error(response)
        data = response.json()
        candidate = (data.get("candidates") or [{}])[0]
        parts = candidate.get("content", {}).get("parts", [])
        text_parts = [part.get("text", "") for part in parts if "text" in part]
        call_scope = _gemini_call_scope(data.get("responseId"))
        calls = [
            _normalized_tool_call(
                id=_gemini_call_id(part["functionCall"].get("id"), call_scope, index),
                name=part["functionCall"]["name"],
                arguments=_arguments(part["functionCall"].get("args")),
            )
            for index, part in enumerate(parts)
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
            if self.config.flavor == ProviderFlavor.VERTEX:
                project = self.config.options.get("project")
                location = self.config.options.get("location")
                if not project or not location:
                    raise ProviderError(
                        "Vertex profiles require project and location options"
                    )
                path = f"/v1/projects/{project}/locations/{location}/publishers/google/models"
                params: dict[str, str] = {}
            else:
                path = self._path("/v1beta/models")
                params = {"pageSize": "1000"}
            models: list[str] = []
            async with self._client(self._headers()) as client:
                # Google pages the catalog (50 rows by default); follow the
                # token so a model past page one is not reported as gone.
                for _page in range(20):
                    response = await client.get(path, params=params)
                    if response.is_error:
                        raise _safe_error(response)
                    payload = response.json()
                    rows = payload.get("models") if isinstance(payload, dict) else None
                    models.extend(
                        item["name"].rsplit("/", 1)[-1]
                        for item in (rows if isinstance(rows, list) else [])
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    )
                    token = (
                        payload.get("nextPageToken")
                        if isinstance(payload, dict)
                        else None
                    )
                    if not isinstance(token, str) or not token:
                        break
                    params = {**params, "pageToken": token}
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=True,
                models=list(dict.fromkeys(models)),
            )
        except Exception as exc:
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_011",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            return ProviderHealth(
                provider_id=self.config.id, healthy=False, detail=str(exc)
            )


def _bedrock_error_detail(exc: BaseException) -> str:
    """Describe a boto3 failure without leaking its types into the adapter.

    A botocore ClientError carries the actionable code and message
    ("ValidationException: The provided model identifier is invalid");
    anything else is reported by type name only.
    """

    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    if isinstance(error, dict):
        code = str(error.get("Code") or "").strip()
        message = re.sub(r"\s+", " ", str(error.get("Message") or "")).strip()
        detail = ": ".join(part for part in (code, message) if part)
        if detail:
            return redact_text(detail)[:500]
    return type(exc).__name__


# Converse errors that mean nothing was generated, so the request may be resent.
_BEDROCK_TRANSIENT_ERRORS = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "ModelNotReadyException",
        "InternalServerException",
        "ModelTimeoutException",
    }
)


def _bedrock_failure(exc: BaseException) -> ProviderError:
    """Type a Converse failure so chat can compact and retry an overflow."""

    detail = _bedrock_error_detail(exc)
    message = f"Bedrock request failed: {detail}"
    if _context_length_error(detail, None):
        return ProviderContextLengthError(message)
    response = getattr(exc, "response", None)
    response = response if isinstance(response, dict) else {}
    error = response.get("Error")
    if isinstance(error, dict) and error.get("Code") in _BEDROCK_TRANSIENT_ERRORS:
        metadata = response.get("ResponseMetadata")
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        return ProviderOverloadedError(
            message, status_code=status if isinstance(status, int) else None
        )
    return ProviderError(message)


def _bedrock_alternating(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent same-role messages; Converse requires alternation."""

    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1] = {
                "role": message["role"],
                "content": [*merged[-1]["content"], *message["content"]],
            }
        else:
            merged.append(message)
    return merged


class BedrockProvider(ModelProvider):
    """AWS Bedrock Converse adapter using boto3 without leaking its types."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        model = self.require(request)
        wire_names = _wire_tool_names(request)
        kwargs: dict[str, Any] = {
            "modelId": model,
            "messages": [
                {
                    "role": "assistant" if msg.role == "assistant" else "user",
                    "content": _bedrock_content(msg.content),
                }
                for msg in request.messages
                if msg.role != "system"
            ],
        }
        for result in request.tool_results:
            kwargs["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": result.call_id,
                                    "name": wire_names.get(result.name, result.name),
                                    "input": result.arguments,
                                }
                            }
                        ],
                    },
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
                    },
                ]
            )
        kwargs["messages"] = _bedrock_alternating(kwargs["messages"])
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
                            "name": wire_names.get(tool.name, tool.name),
                            "description": tool.description,
                            "inputSchema": {"json": tool.input_schema},
                        }
                    }
                    for tool in request.tools
                ]
            }
            if request.tool_choice == ToolChoice.REQUIRED:
                if rejects_forced_tool_choice(model):
                    kwargs["toolConfig"]["toolChoice"] = {"auto": {}}
                else:
                    kwargs["toolConfig"]["toolChoice"] = {"any": {}}
                    if _claude_thinks_by_default(model):
                        # Bedrock accepts a forced choice only with thinking off.
                        kwargs["additionalModelRequestFields"] = {
                            "thinking": {"type": "disabled"}
                        }
        inference: dict[str, Any] = {}
        if request.max_output_tokens:
            inference["maxTokens"] = request.max_output_tokens
        if request.temperature is not None and not _claude_rejects_sampling(model):
            inference["temperature"] = request.temperature
        if inference:
            kwargs["inferenceConfig"] = inference
        # botocore would otherwise resend a whole generation up to five times
        # after its 60 s read timeout; Nebula's retry policy decides instead.
        client_config = BotocoreConfig(
            connect_timeout=_NATIVE_CONNECT_TIMEOUT_SECONDS,
            read_timeout=_native_request_timeout(self.config),
            retries={"mode": "standard", "max_attempts": 1},
        )

        def invoke() -> dict[str, Any]:
            client = boto3.client(
                "bedrock-runtime",
                region_name=self.config.options.get("region"),
                config=client_config,
            )
            return client.converse(**kwargs)

        policy = retry_policy(self.config)
        attempt = 1
        while True:
            try:
                data = await asyncio.to_thread(invoke)
                break
            except Exception as exc:
                record_caught_exception(
                    "providers",
                    "providers.providers.caught_failure_012",
                    "A handled providers operation raised an exception.",
                    exc,
                    stage="providers",
                )
                failure = _bedrock_failure(exc)
                if not isinstance(failure, ProviderOverloadedError):
                    raise failure from exc
                if attempt >= policy.attempts:
                    if attempt > 1:
                        raise _exhausted(failure, attempt) from exc
                    raise failure from exc
                delay = _retry_delay(policy, attempt, None)
                _record_retry(
                    self.config,
                    operation="converse",
                    attempt=attempt,
                    policy=policy,
                    delay=delay,
                    status_code=failure.status_code,
                )
            await asyncio.sleep(delay)
            attempt += 1
        blocks = data.get("output", {}).get("message", {}).get("content", [])
        calls = [
            _normalized_tool_call(
                id=block["toolUse"].get("toolUseId", ""),
                name=_decode_tool_name(request, block["toolUse"].get("name", "")),
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
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_013",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            return ProviderHealth(
                provider_id=self.config.id,
                healthy=False,
                detail=f"Bedrock health check failed: {_bedrock_error_detail(exc)}",
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
            flavor=ProviderFlavor.ORCAROUTER,
            adapter=ProviderKind.OPENAI_COMPATIBLE,
            display_name="OrcaRouter",
            default_base_url="https://api.orcarouter.ai/v1",
            suggested_key_env="ORCAROUTER_API_KEY",
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
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_014",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
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
    api_key_value: SecretStr | None = None,
    credential_ref: str | None = None,
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
        api_key_env=(
            api_key_env or entry.suggested_key_env
            if credential_ref is None
            else api_key_env
        ),
        api_key_value=api_key_value,
        credential_ref=credential_ref,
        local=local,
        capabilities=capabilities or ModelCapabilities(),
        options=options,
        **overrides,
    )


def provider_from_profile(
    profile: ProviderProfile,
    credential_resolver: Callable[[str], SecretStr] | None = None,
) -> ModelProvider:
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
        record_caught_exception(
            "providers",
            "providers.providers.caught_failure_015",
            "A handled providers operation raised an exception.",
            exc,
            stage="providers",
        )
        raise ValueError(
            f"unknown provider type {profile.provider_type!r}; "
            "use provider-catalog values"
        ) from exc
    secret_env: str | None = None
    secret_value: SecretStr | None = None
    credential_ref: str | None = None
    if profile.secret_ref:
        if profile.secret_ref.startswith("env:"):
            secret_env = profile.secret_ref.removeprefix("env:")
        elif profile.secret_ref.startswith(("vault:", "session:")):
            credential_ref = profile.secret_ref
            if credential_resolver is not None:
                secret_value = credential_resolver(profile.secret_ref)
        else:
            raise ValueError(
                "provider secret_ref must use env:NAME, vault:ID, or session:ID"
            )
    raw_options = profile.metadata.get("options", {})
    options = raw_options if isinstance(raw_options, dict) else {}
    context_window = options.get("context_window")
    max_output_tokens = options.get("max_output_tokens")
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
        context_window=(
            context_window
            if isinstance(context_window, int) and not isinstance(context_window, bool)
            else None
        ),
        max_output_tokens=(
            max_output_tokens
            if isinstance(max_output_tokens, int)
            and not isinstance(max_output_tokens, bool)
            else None
        ),
    )
    default_model = profile.metadata.get("default_model") or next(
        iter(profile.model_allowlist), None
    )
    config = config_from_catalog(
        provider_id=profile.id,
        flavor=flavor,
        base_url=profile.endpoint,
        api_key_env=secret_env,
        api_key_value=secret_value,
        credential_ref=credential_ref,
        default_model=default_model,
        model_allowlist=profile.model_allowlist,
        capabilities=capabilities,
        local=profile.is_local,
        enabled=profile.enabled,
        data_residency=(
            profile.privacy.residency[0] if profile.privacy.residency else None
        ),
        data_retention=profile.privacy.retention,
        options=options,
    )
    if profile.privacy.local_only and not config.local:
        raise ValueError("a local-only privacy profile cannot use a cloud provider")
    descriptors = profile.metadata.get("model_descriptors")
    if isinstance(descriptors, list):
        config = config.model_copy(
            update={
                "model_parameters": {
                    str(item["id"]): [
                        str(value) for value in item.get("supported_parameters") or []
                    ]
                    for item in descriptors
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                }
            }
        )
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
    "ProviderContextLengthError",
    "ProviderOverloadedError",
    "ProviderResponseError",
    "ProviderFlavor",
    "ProviderHealth",
    "ProviderKind",
    "ProviderRegistry",
    "ProviderRouteRequest",
    "RetryPolicy",
    "ToolDefinition",
    "UnsupportedCapability",
    "build_provider",
    "config_from_catalog",
    "provider_from_profile",
    "retry_policy",
]
