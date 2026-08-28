"""Versioned execution contract for Security Browser desktop adapters.

Core persists normalized readiness and receipts. Secret-bearing browser state and
the live process remain owned by the loopback-authenticated desktop adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import ipaddress
import os
from typing import Any, Literal
from urllib.parse import urlsplit
from urllib.parse import quote

import httpx
from pydantic import Field

from .domain import (
    BrowserEngineCapability,
    BrowserEngineState,
    NebulaModel,
)


BROWSER_ENGINE_CONTRACT_VERSION = "1"


class BrowserEngineUnavailableError(RuntimeError):
    """A normalized local-runtime failure that never includes bearer material."""


class BrowserEngineAction(NebulaModel):
    action_token: str = Field(min_length=1, max_length=300)
    assessment_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    identity_id: str = Field(min_length=1, max_length=200)
    tab_id: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "navigate",
        "click",
        "fill",
        "select",
        "press",
        "upload",
        "wait",
        "snapshot",
        "screenshot",
    ]
    locator: dict[str, str] = Field(default_factory=dict)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_page_url: str | None = Field(default=None, max_length=16_384)
    pre_fingerprint: str | None = Field(default=None, max_length=500)
    side_effect: Literal["none", "possible", "known"] = "possible"


class BrowserEngineReceipt(NebulaModel):
    action_token: str = Field(min_length=1, max_length=300)
    state: Literal["complete", "failed", "ambiguous", "cancelled"]
    page_url: str | None = Field(default=None, max_length=16_384)
    pre_fingerprint: str | None = Field(default=None, max_length=500)
    post_fingerprint: str | None = Field(default=None, max_length=500)
    trace_ids: list[str] = Field(default_factory=list, max_length=100)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    failure_code: str | None = Field(default=None, max_length=120)
    operator_message: str | None = Field(default=None, max_length=1_000)
    recovery_action: str | None = Field(default=None, max_length=1_000)


class BrowserEngineAdapter(ABC):
    """Conformance boundary implemented by managed Chromium and scanners."""

    contract_version = BROWSER_ENGINE_CONTRACT_VERSION

    @abstractmethod
    async def readiness(self) -> BrowserEngineCapability:
        raise NotImplementedError

    @abstractmethod
    async def ensure_identity(self, identity_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def execute(self, action: BrowserEngineAction) -> BrowserEngineReceipt:
        raise NotImplementedError

    @abstractmethod
    async def pause(self, assessment_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resume(self, assessment_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self, assessment_id: str) -> None:
        raise NotImplementedError


class LocalBrowserdAdapter(BrowserEngineAdapter):
    """Authenticated loopback client for the desktop-owned managed Chromium process."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        parsed = urlsplit(base_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "browserd URL must use a literal loopback address"
            ) from exc
        if (
            parsed.scheme != "http"
            or not address.is_loopback
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "browserd URL must be credential-free HTTP on a literal loopback address and explicit port"
            )
        if not token or any(character.isspace() for character in token):
            raise ValueError("browserd authentication token must be opaque")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = client
        self._timeout = timeout_seconds

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }
        if self._client is not None:
            response = await self._client.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=payload,
                timeout=self._timeout,
            )
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=payload,
                )
        return response

    async def readiness(self) -> BrowserEngineCapability:
        try:
            response = await self._request("GET", "/v1/readiness")
            response.raise_for_status()
            capability = BrowserEngineCapability.model_validate(response.json())
            if (
                capability.adapter != "managed-chromium"
                or capability.contract_version != self.contract_version
            ):
                raise ValueError("browserd returned an incompatible adapter contract")
            return capability
        except Exception:
            return BrowserEngineCapability(
                adapter="managed-chromium",
                display_name="Managed Chromium",
                state=BrowserEngineState.UNAVAILABLE,
                unavailability_reason=(
                    "The authenticated browserd readiness receipt was unavailable or incompatible. "
                    "Manual legacy browsing remains usable."
                ),
                recovery_action=(
                    "Restart or prepare the managed browser runtime, then retry preflight."
                ),
            )

    async def ensure_identity(self, identity_id: str) -> None:
        response = await self._request(
            "POST", "/v1/identities/ensure", {"identity_id": identity_id}
        )
        if response.status_code >= 400:
            raise BrowserEngineUnavailableError(
                "Managed Chromium could not prepare the selected identity. Retry preparation."
            )

    async def execute(self, action: BrowserEngineAction) -> BrowserEngineReceipt:
        try:
            response = await self._request(
                "POST", "/v1/actions", action.model_dump(mode="json")
            )
            response.raise_for_status()
            receipt = BrowserEngineReceipt.model_validate(response.json())
            if receipt.action_token != action.action_token:
                raise ValueError("browserd returned a receipt for another action")
            return receipt
        except Exception:
            ambiguous = action.side_effect != "none"
            return BrowserEngineReceipt(
                action_token=action.action_token,
                state="ambiguous" if ambiguous else "failed",
                pre_fingerprint=action.pre_fingerprint,
                failure_code=(
                    "browserd_receipt_ambiguous"
                    if ambiguous
                    else "browserd_unavailable"
                ),
                operator_message=(
                    "The managed browser action may have occurred, but its durable receipt was not recovered."
                    if ambiguous
                    else "The managed browser action did not return an authenticated receipt."
                ),
                recovery_action=(
                    "Review the live page and trace before choosing a new action; Nebula will not replay automatically."
                    if ambiguous
                    else "Retry after Managed Chromium reports ready."
                ),
            )

    async def _lifecycle(self, assessment_id: str, action: str) -> None:
        response = await self._request(
            "POST",
            f"/v1/assessments/{quote(assessment_id, safe='')}/{action}",
            {"assessment_id": assessment_id},
        )
        if response.status_code >= 400:
            raise BrowserEngineUnavailableError(
                f"Managed Chromium could not {action} the assessment. Use emergency stop if it remains active."
            )

    async def pause(self, assessment_id: str) -> None:
        await self._lifecycle(assessment_id, "pause")

    async def resume(self, assessment_id: str) -> None:
        await self._lifecycle(assessment_id, "resume")

    async def stop(self, assessment_id: str) -> None:
        await self._lifecycle(assessment_id, "stop")


class BrowserEngineRegistry:
    """Process-local adapter catalog; production adapters register at startup."""

    def __init__(self, adapters: list[BrowserEngineAdapter] | None = None) -> None:
        if adapters is None:
            endpoint = os.environ.get("NEBULA_BROWSERD_URL")
            token = os.environ.get("NEBULA_BROWSERD_TOKEN")
            adapters = (
                [LocalBrowserdAdapter(endpoint, token)]
                if endpoint is not None and token is not None
                else []
            )
        self._adapters = list(adapters)

    def register(self, adapter: BrowserEngineAdapter) -> None:
        self._adapters.append(adapter)

    async def adapter(self, name: str) -> BrowserEngineAdapter | None:
        for adapter in self._adapters:
            capability = await adapter.readiness()
            if (
                capability.adapter == name
                and capability.state == BrowserEngineState.READY
            ):
                return adapter
        return None

    async def capabilities(self) -> list[BrowserEngineCapability]:
        receipts = [await adapter.readiness() for adapter in self._adapters]
        if not any(receipt.adapter == "managed-chromium" for receipt in receipts):
            receipts.append(
                BrowserEngineCapability(
                    adapter="managed-chromium",
                    display_name="Managed Chromium",
                    state=BrowserEngineState.UNAVAILABLE,
                    unavailability_reason=(
                        "The project-scoped browserd runtime has not reported ready "
                        "on this desktop. Manual legacy browsing remains usable."
                    ),
                    recovery_action="Prepare the managed browser runtime, then retry preflight.",
                )
            )
        if not any(receipt.adapter == "zap" for receipt in receipts):
            receipts.append(
                BrowserEngineCapability(
                    adapter="zap",
                    display_name="OWASP ZAP scanner",
                    state=BrowserEngineState.UNAVAILABLE,
                    unavailability_reason=(
                        "The digest-pinned scanner runtime has not been prepared. "
                        "Manual browsing remains usable without scanning."
                    ),
                    recovery_action="Prepare the scanner runtime or choose Explore.",
                )
            )
        receipts.append(
            BrowserEngineCapability(
                adapter="legacy-webview",
                display_name="Legacy system WebView",
                state=BrowserEngineState.DEGRADED,
                actions=["manual.navigate", "manual.takeover"],
                protocols=["http", "https"],
                unavailability_reason=(
                    "Legacy mode is manual-only and does not claim autonomous reliability."
                ),
                recovery_action="Prepare Managed Chromium for guided execution.",
            )
        )
        return receipts
