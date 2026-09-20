"""Provider-neutral model gateway for Nebula 3.

The domain only depends on the types in this module.  Provider SDK objects are
never exposed to orchestration, policy, storage, or the UI.  Credentials are
resolved lazily from environment references and are deliberately excluded from
model serialization and repr output.
"""

from __future__ import annotations

from .diagnostics import record_caught_exception, record_diagnostic

import asyncio
import ipaddress
import hashlib
import json
import os
import random
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any
from urllib.parse import quote, urlsplit

import boto3  # type: ignore[import-untyped]
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
# retry would only repeat.
_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
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
) -> httpx.Response:
    """Send a provider request, retrying only transient upstream failures.

    The caller still raises for the returned response; only an exhausted
    transient failure is raised here, so its message can name the attempts.
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
                raise
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_016",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            delay = _retry_delay(policy, attempt, None)
        else:
            if not response.is_error:
                return response
            error = _safe_error(response)
            if not isinstance(error, ProviderOverloadedError):
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


@asynccontextmanager
async def _stream_with_retry(
    config: ProviderConfig,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: dict[str, Any],
    operation: str,
) -> AsyncIterator[httpx.Response]:
    """Open a streaming provider response, retrying before the first token.

    Once any token has been yielded the stream is never replayed: a partial
    answer must not be silently restarted underneath the operator.
    """

    policy = retry_policy(config)
    attempt = 1
    while True:
        started = False
        status_code: int | None = None
        error: ProviderError | None = None
        try:
            async with client.stream(method, url, json=json) as response:
                if not response.is_error:
                    started = True
                    yield response
                    return
                await response.aread()
                error = _safe_error(response)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if started or attempt >= policy.attempts:
                raise
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_017",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            delay = _retry_delay(policy, attempt, None)
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


def _safe_error(response: httpx.Response) -> ProviderError:
    request_id = response.headers.get("x-request-id") or response.headers.get(
        "request-id"
    )
    error_code: str | None = None
    try:
        detail, error_code = _error_detail(response.json())
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
    frame ends the stream as an empty, successful reply.
    """

    if not isinstance(data, dict) or not data.get("error"):
        return None
    detail, error_code = _error_detail(data)
    message = "provider reported an error while streaming" + (
        f": {detail}" if detail else ""
    )
    if _context_length_error(detail, error_code):
        return ProviderContextLengthError(message)
    return ProviderError(message)


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        record_caught_exception(
            "providers",
            "providers.providers.caught_failure_005",
            "A handled providers operation raised an exception.",
            exc,
            stage="providers",
        )
        raise ProviderError("provider returned malformed tool arguments") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("provider returned non-object tool arguments")
    return parsed


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
            "input": [
                _openai_responses_message(message) for message in request.messages
            ],
        }
        for result in request.tool_results:
            payload["input"].extend(
                [
                    {
                        "type": "function_call",
                        "call_id": result.call_id,
                        "name": result.name,
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
            if request.tool_choice == ToolChoice.REQUIRED:
                payload["tool_choice"] = "required"
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
    """Map Nebula tool names to names every Chat Completions vendor accepts.

    Nebula names may contain dots (``tool_output.search``); OpenAI and
    Anthropic accept only ``[a-zA-Z0-9_-]{1,64}``. The mapping is derived from
    the request alone, so responses and replayed history decode identically.
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
        if request.max_output_tokens:
            payload["max_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        openrouter = self.config.flavor == ProviderFlavor.OPENROUTER
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
                        "strict": tool.strict,
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
                    "strict": True,
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
            payload["reasoning"] = {"exclude": False}
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
        calls = [
            _normalized_tool_call(
                id=item.get("id", ""),
                name=_decode_tool_name(
                    request, item.get("function", {}).get("name", "")
                ),
                arguments=_arguments(item.get("function", {}).get("arguments")),
            )
            for item in message.get("tool_calls", [])
        ]
        usage = data.get("usage") or {}
        return ModelResponse(
            provider_id=self.config.id,
            model=data.get("model", model),
            text=_openai_message_content(message).strip(),
            reasoning=_openai_message_reasoning(message).strip(),
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
            models = [item["id"] for item in response.json().get("data", [])]
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
                    )
                    directory: httpx.Response | None = None
                    try:
                        if not response.is_error:
                            directory = await client.get(
                                self._path("/v1/providers"), timeout=10.0
                            )
                    except httpx.HTTPError:  # diagnostic-expected: the allowlist picker stays empty; routing is unaffected
                        directory = None
                    upstream: list[UpstreamProvider] = []
                    try:
                        if directory is not None and not directory.is_error:
                            upstream = openrouter_upstream_providers(directory.json())
                    except ValueError:  # diagnostic-expected: the allowlist picker stays empty; routing is unaffected
                        upstream = []
                    served: set[str] | None = None
                    if not response.is_error and self.openrouter_allowed_providers:
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
            TimeoutError,
            ValueError,
        ):  # diagnostic-expected: failure is reported as unhealthy; raw text may contain credentials
            # Transport and upstream payload exceptions may contain credentials.
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
    try:
        async with provider._client(provider._headers()) as client:
            async with _stream_with_retry(
                provider.config,
                client,
                "POST",
                provider._path("/v1/chat/completions"),
                json=payload,
                operation="chat_completions_stream",
            ) as response:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    encoded = line.removeprefix("data:").strip()
                    if not encoded or encoded == "[DONE]":
                        continue
                    data = json.loads(encoded)
                    failure = _stream_error_frame(data)
                    if failure is not None:
                        raise failure
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
            _normalized_tool_call(
                id=value["id"],
                name=_decode_tool_name(request, value["name"]),
                arguments=_arguments(value["arguments"]),
            )
            for _, value in sorted(call_parts.items())
        ]
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
                                "name": result.name,
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
            if request.tool_choice == ToolChoice.REQUIRED:
                payload["tool_choice"] = {
                    "type": "any",
                    "disable_parallel_tool_use": not request.parallel_tool_calls,
                }
        async with self._client(self._headers()) as client:
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
                                    "id": result.call_id,
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
                                    "id": result.call_id,
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
                            "parameters": tool.input_schema,
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
        async with self._client(self._headers()) as client:
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
        calls = [
            _normalized_tool_call(
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
        for result in request.tool_results:
            kwargs["messages"].extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "toolUse": {
                                    "toolUseId": result.call_id,
                                    "name": result.name,
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
            if request.tool_choice == ToolChoice.REQUIRED:
                kwargs["toolConfig"]["toolChoice"] = {"any": {}}
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
            record_caught_exception(
                "providers",
                "providers.providers.caught_failure_012",
                "A handled providers operation raised an exception.",
                exc,
                stage="providers",
            )
            raise ProviderError(
                f"Bedrock request failed: {_bedrock_error_detail(exc)}"
            ) from exc
        blocks = data.get("output", {}).get("message", {}).get("content", [])
        calls = [
            _normalized_tool_call(
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
