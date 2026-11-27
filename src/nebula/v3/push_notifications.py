"""Butler alerts on the operator's iPhone through Apple Push Notification service.

Every Butler incident (an operator action request, a blocked or stuck goal, a
failed recovery, a resource or harness limit, a review finding) already lands
in ``ButlerIncident``. The dispatcher here watches those rows across every
project and sends one alert per open episode to each registered phone:

* an incident that opens, or reopens after it was resolved, alerts once;
  updates to it while it stays open do not alert again;
* incidents already open the first time the dispatcher runs are recorded as a
  baseline and not sent one by one; a phone's first registration instead gets
  one summary alert that also proves delivery works;
* more than ``GROUP_THRESHOLD`` new incidents in one pass become one grouped
  alert, so a burst does not bury the phone.

Exactly one dispatcher runs per install: in the supervisor, or in a
single-process Core (no execution runtime). Project workers never send.
The APNs key stays in the credential store and is read only by the
dispatcher. See docs/design/butler-push-notifications.md.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import Field

from .credentials import CredentialStore
from .diagnostics import record_caught_exception
from .domain import (
    ButlerIncident,
    Engagement,
    NebulaModel,
    PairedDeviceSession,
    PushDevice,
    PushIncidentDelivery,
    PushNotificationSettings,
    utc_now,
)
from .storage import ConflictError, NebulaStore, NotFoundError

logger = logging.getLogger(__name__)

PUSH_SETTINGS_ID = "push-notification-settings"
PASS_SECONDS = 5.0
UNCONFIGURED_PASS_SECONDS = 30.0
GROUP_THRESHOLD = 3
MAX_ATTEMPTS = 3
TEST_WINDOW_SECONDS = 120
JWT_LIFETIME_SECONDS = 45 * 60
APNS_HOSTS = {
    "production": "https://api.push.apple.com",
    "development": "https://api.sandbox.push.apple.com",
}


def apns_base_url(environment: str) -> str:
    """Apple's endpoint, or ``NEBULA_APNS_BASE_URL`` for a disposable test Core
    whose alerts go to a local fake push service instead of Apple."""
    override = os.environ.get("NEBULA_APNS_BASE_URL", "").strip().rstrip("/")
    return override or APNS_HOSTS[environment]


# Apple's answers that mean this token will never work again.
DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}
# Answers that mean the provider key or settings are wrong for every phone.
CONFIG_REASONS = {
    "InvalidProviderToken",
    "ExpiredProviderToken",
    "MissingProviderToken",
    "BadTopic",
    "TopicDisallowed",
    "BadCertificateEnvironment",
}


# ---------------------------------------------------------------------------
# Settings and status


async def read_settings(store: NebulaStore) -> PushNotificationSettings:
    try:
        return await store.get(PushNotificationSettings, PUSH_SETTINGS_ID)
    except NotFoundError:
        # diagnostic-expected: an install that never set up push has no row;
        # the defaults describe it as unconfigured.
        return PushNotificationSettings(id=PUSH_SETTINGS_ID)


def missing_settings(settings: PushNotificationSettings) -> list[str]:
    missing = []
    if not settings.key_ref:
        missing.append("APNs key")
    if not settings.key_id:
        missing.append("key ID")
    if not settings.team_id:
        missing.append("team ID")
    return missing


def _device_view(device: PushDevice, caller: str | None) -> dict[str, Any]:
    return {
        "id": device.id,
        "label": device.label,
        "environment": device.environment,
        "topic": device.topic,
        "enabled": device.enabled and device.unregistered_at is None,
        "this_device": caller is not None and device.paired_device_id == caller,
        "created_at": device.created_at.isoformat(),
        "last_sent_at": device.last_sent_at.isoformat()
        if device.last_sent_at
        else None,
        "last_attempt_at": device.last_attempt_at.isoformat()
        if device.last_attempt_at
        else None,
        "last_error": device.last_error,
        "test_pending": device.test_requested_at is not None,
        "unregistered": device.unregistered_at is not None,
        "revision": device.revision,
    }


async def _devices(store: NebulaStore) -> list[PushDevice]:
    return await store.find_entities(PushDevice, {}, bulk=True)


async def push_status(store: NebulaStore, caller: str | None) -> dict[str, Any]:
    settings = await read_settings(store)
    missing = missing_settings(settings)
    devices = [
        _device_view(device, caller)
        for device in sorted(await _devices(store), key=lambda item: item.created_at)
    ]
    return {
        "configured": not missing and settings.enabled,
        "enabled": settings.enabled,
        "missing": missing,
        "key_saved": bool(settings.key_ref),
        "key_id": settings.key_id,
        "team_id": settings.team_id,
        "topic": settings.topic,
        "last_error": settings.last_error,
        "last_error_at": settings.last_error_at.isoformat()
        if settings.last_error_at
        else None,
        "devices": devices,
        "revision": settings.revision,
    }


# ---------------------------------------------------------------------------
# APNs


class ApnsError(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(f"APNs {status_code}: {reason}")
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class Alert:
    title: str
    body: str
    path: str
    thread: str
    collapse_id: str | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def provider_token(key_pem: str, key_id: str, team_id: str, issued_at: int) -> str:
    """The ES256 JWT APNs accepts as the provider's authorization."""
    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise ValueError("The APNs key must be Apple's P-256 .p8 signing key.")
    header = _b64(
        json.dumps({"alg": "ES256", "kid": key_id}, separators=(",", ":")).encode()
    )
    claims = _b64(
        json.dumps({"iss": team_id, "iat": issued_at}, separators=(",", ":")).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    return f"{header}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def alert_payload(alert: Alert) -> dict[str, Any]:
    return {
        "aps": {
            "alert": {"title": alert.title[:120], "body": alert.body[:600]},
            "sound": "default",
            "thread-id": alert.thread[:64],
            "interruption-level": "time-sensitive",
        },
        "nebula_path": alert.path,
    }


@dataclass
class ApnsSender:
    """Sends alerts with one provider token, refreshed before Apple expires it."""

    key_pem: str
    key_id: str
    team_id: str
    client: httpx.AsyncClient
    _token: str | None = None
    _issued_at: float = 0.0

    def authorization(self) -> str:
        now = time.time()
        if self._token is None or now - self._issued_at > JWT_LIFETIME_SECONDS:
            self._token = provider_token(
                self.key_pem, self.key_id, self.team_id, int(now)
            )
            self._issued_at = now
        return self._token

    async def send(self, device: PushDevice, alert: Alert) -> None:
        headers = {
            "authorization": f"bearer {self.authorization()}",
            "apns-topic": device.topic,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        if alert.collapse_id:
            headers["apns-collapse-id"] = alert.collapse_id[:64]
        response = await self.client.post(
            f"{apns_base_url(device.environment)}/3/device/{device.token}",
            headers=headers,
            content=json.dumps(alert_payload(alert)).encode(),
        )
        if response.status_code == 200:
            return
        try:
            reason = str(response.json().get("reason") or response.status_code)
        except ValueError:
            # diagnostic-expected: Apple answered without its JSON reason
            # body; the status code is the reason shown to the operator.
            reason = str(response.status_code)
        raise ApnsError(response.status_code, reason)


def default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(http2=True, timeout=httpx.Timeout(15.0, connect=10.0))


# ---------------------------------------------------------------------------
# Dispatcher


def incident_alert(incident: ButlerIncident, project_name: str) -> Alert:
    lines = [incident.summary.strip()]
    if incident.next_action and incident.next_action.strip():
        lines.append(f"Next: {incident.next_action.strip()}")
    return Alert(
        title=f"{project_name} needs you",
        body="\n".join(lines),
        path=f"/butler?project={incident.engagement_id}&section=oversight",
        thread=incident.engagement_id,
        collapse_id=incident.id,
    )


def grouped_alert(incidents: list[ButlerIncident], names: dict[str, str]) -> Alert:
    projects = sorted(
        {names.get(item.engagement_id, "a project") for item in incidents}
    )
    shown = ", ".join(projects[:3]) + (
        f" and {len(projects) - 3} more" if len(projects) > 3 else ""
    )
    one_project = len({item.engagement_id for item in incidents}) == 1
    path = (
        f"/butler?project={incidents[0].engagement_id}&section=oversight"
        if one_project
        else "/butler?section=oversight"
    )
    return Alert(
        title=f"{len(incidents)} Butler items need you",
        body=f"In {shown}. Open Oversight to review them.",
        path=path,
        thread="butler-summary",
    )


def welcome_alert(open_count: int) -> Alert:
    body = (
        "Butler will alert this phone when a project needs you or is blocked."
        if open_count == 0
        else f"{open_count} Butler item{'s' if open_count != 1 else ''} already open. Butler alerts this phone when more need you."
    )
    return Alert(
        title="Butler notifications are on",
        body=body,
        path="/butler?section=oversight",
        thread="butler-summary",
    )


WELCOME_RETRY_SECONDS = 600


def delivery_id(incident_id: str) -> str:
    """Entity ids are global, so the delivery row cannot reuse the incident's."""
    return f"push-{incident_id}"[:200]


def _may_retry(device: PushDevice) -> bool:
    """A failed first alert is retried every ten minutes, not every pass."""
    if device.last_error is None or device.last_attempt_at is None:
        return True
    return (utc_now() - device.last_attempt_at).total_seconds() >= WELCOME_RETRY_SECONDS


@dataclass
class PassResult:
    sent: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class ButlerPushDispatcher:
    """The install's single sender. ``run_pass`` is one bounded, idempotent pass."""

    def __init__(
        self,
        store: NebulaStore,
        *,
        credentials: CredentialStore | None = None,
        client_factory: Any = default_client,
        sender_factory: Any = None,
    ) -> None:
        self.store = store
        self.credentials = credentials
        self._client_factory = client_factory
        self._sender_factory = sender_factory
        self._client: httpx.AsyncClient | None = None
        self._sender: Any = None
        self._sender_key: tuple[str, str, str, int] | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(self) -> None:
        while not self._stop.is_set():
            delay = PASS_SECONDS
            try:
                configured = await self.run_pass()
                if not configured:
                    delay = UNCONFIGURED_PASS_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as error:
                record_caught_exception(
                    "butler",
                    "butler.push.pass_failed",
                    "A Butler notification pass failed; the next pass retries.",
                    error,
                    stage="push-dispatch",
                    operation_failed=True,
                )
                delay = UNCONFIGURED_PASS_SECONDS
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                # diagnostic-expected: the pause between passes elapsed.
                continue

    async def _sender_for(self, settings: PushNotificationSettings) -> Any:
        assert settings.key_ref and settings.key_id and settings.team_id
        identity = (
            settings.key_ref,
            settings.key_id,
            settings.team_id,
            settings.revision,
        )
        if self._sender is not None and self._sender_key == identity:
            return self._sender
        if self._sender_factory is not None:
            self._sender = self._sender_factory(settings)
        else:
            credentials = self.credentials or CredentialStore()
            self.credentials = credentials
            key = credentials.resolve(settings.key_ref).get_secret_value()
            if self._client is None:
                self._client = self._client_factory()
            self._sender = ApnsSender(
                key, settings.key_id, settings.team_id, self._client
            )
        self._sender_key = identity
        return self._sender

    async def _live_devices(self) -> list[PushDevice]:
        now = utc_now()
        live: list[PushDevice] = []
        for device in await _devices(self.store):
            if not device.enabled or device.unregistered_at is not None:
                continue
            try:
                paired = await self.store.get(
                    PairedDeviceSession, device.paired_device_id
                )
            except NotFoundError:
                # diagnostic-expected: the pairing was deleted; its phone no
                # longer receives Butler alerts.
                continue
            if paired.revoked_at is not None or paired.absolute_expires_at <= now:
                continue
            live.append(device)
        return live

    async def _record_setting_error(
        self, settings: PushNotificationSettings, message: str | None
    ) -> None:
        if settings.last_error == message:
            return
        try:
            await self.store.update(
                PushNotificationSettings,
                PUSH_SETTINGS_ID,
                {
                    "last_error": message,
                    "last_error_at": utc_now() if message else None,
                },
                expected_revision=settings.revision,
            )
        except (ConflictError, NotFoundError):
            # diagnostic-expected: the operator saved settings meanwhile; the
            # next pass records the error against the new revision.
            return

    async def _send(
        self, sender: Any, device: PushDevice, alert: Alert, result: PassResult
    ) -> bool:
        now = utc_now()
        changes: dict[str, Any] = {"last_attempt_at": now}
        ok = False
        try:
            await sender.send(device, alert)
            changes.update(last_sent_at=now, last_error=None)
            result.sent += 1
            ok = True
        except ApnsError as error:
            # diagnostic-expected: Apple's refusal is the phone's recorded
            # last_error, shown in Butler settings, and decides retry or retire.
            result.failed += 1
            result.errors.append(error.reason)
            changes["last_error"] = f"Apple refused the alert: {error.reason}"[:500]
            if error.reason in DEAD_TOKEN_REASONS or error.status_code == 410:
                changes["unregistered_at"] = now
            if error.reason == "ExpiredProviderToken" and isinstance(
                sender, ApnsSender
            ):
                sender._token = None
        except (httpx.HTTPError, OSError) as error:
            record_caught_exception(
                "butler",
                "butler.push.send_failed",
                "A Butler notification could not reach Apple.",
                error,
                stage="push-send",
            )
            result.failed += 1
            result.errors.append("Apple's push service could not be reached.")
            changes["last_error"] = "Apple's push service could not be reached."
        try:
            await self.store.update(PushDevice, device.id, changes)
        except NotFoundError:
            # diagnostic-expected: the phone was removed while its alert was in
            # flight; nothing is left to record against.
            pass
        return ok

    async def run_pass(self) -> bool:
        """Send what is due. Returns whether push is configured."""
        settings = await read_settings(self.store)
        if missing_settings(settings) or not settings.enabled:
            return False
        open_incidents = await self.store.find_entities(
            ButlerIncident, {"status": "open"}, bulk=True
        )
        open_ids = {item.id for item in open_incidents}
        deliveries = {
            item.incident_id: item
            for item in await self.store.find_entities(
                PushIncidentDelivery, {}, bulk=True
            )
        }
        # An incident that resolved (or was deleted) ends its episode, so a
        # reopen alerts again. Acknowledged incidents keep theirs.
        for incident_id in list(deliveries):
            if incident_id in open_ids:
                continue
            try:
                incident = await self.store.get(ButlerIncident, incident_id)
            except NotFoundError:
                # diagnostic-expected: a deleted incident has no episode left.
                incident = None
            if incident is None or incident.status == "resolved":
                await self._forget(deliveries.pop(incident_id).id)

        if settings.baseline_at is None:
            for incident in open_incidents:
                if incident.id not in deliveries:
                    await self._claim(incident, "baseline", notified=False)
            try:
                await self.store.update(
                    PushNotificationSettings,
                    PUSH_SETTINGS_ID,
                    {"baseline_at": utc_now()},
                    expected_revision=settings.revision,
                )
            except (ConflictError, NotFoundError):
                # diagnostic-expected: settings changed during the baseline;
                # the next pass reads them again and finishes it.
                return True
            return True

        devices = await self._live_devices()
        sender = await self._sender_for(settings)
        result = PassResult()

        # Tests the operator asked for, and the first alert to a new phone.
        for device in devices:
            if device.test_requested_at is not None:
                age = (utc_now() - device.test_requested_at).total_seconds()
                if age <= TEST_WINDOW_SECONDS:
                    await self._send(
                        sender,
                        device,
                        Alert(
                            title="Test from Butler",
                            body="Butler notifications reach this phone.",
                            path="/butler?section=oversight",
                            thread="butler-summary",
                        ),
                        result,
                    )
                await self.store.update(
                    PushDevice, device.id, {"test_requested_at": None}
                )
            if device.welcomed_at is None and _may_retry(device):
                if await self._send(
                    sender, device, welcome_alert(len(open_incidents)), result
                ):
                    await self.store.update(
                        PushDevice, device.id, {"welcomed_at": utc_now()}
                    )

        due = [
            item
            for item in open_incidents
            if item.id not in deliveries
            or (
                deliveries[item.id].notified_at is None
                and deliveries[item.id].delivery == "alert"
                and deliveries[item.id].attempts < MAX_ATTEMPTS
            )
        ]
        if not due:
            await self._record_setting_error(settings, self._config_error(result))
            return True
        due.sort(key=lambda item: item.observed_at)
        if not devices:
            for incident in due:
                await self._claim(incident, "no_devices", notified=False)
            return True
        names = await self._project_names({item.engagement_id for item in due})
        if len(due) > GROUP_THRESHOLD:
            alert = grouped_alert(due, names)
            delivered = False
            for device in devices:
                delivered = await self._send(sender, device, alert, result) or delivered
            for incident in due:
                await self._claim(incident, "summary", notified=delivered)
        else:
            for incident in due:
                alert = incident_alert(
                    incident, names.get(incident.engagement_id, "A project")
                )
                delivered = False
                for device in devices:
                    delivered = (
                        await self._send(sender, device, alert, result) or delivered
                    )
                await self._claim(
                    incident,
                    "alert",
                    notified=delivered,
                    existing=deliveries.get(incident.id),
                )
        await self._record_setting_error(settings, self._config_error(result))
        return True

    @staticmethod
    def _config_error(result: PassResult) -> str | None:
        reasons = [reason for reason in result.errors if reason in CONFIG_REASONS]
        if reasons:
            return f"Apple refused the push key or settings: {reasons[0]}"
        return None

    async def _project_names(self, project_ids: set[str]) -> dict[str, str]:
        names: dict[str, str] = {}
        for project_id in project_ids:
            try:
                names[project_id] = (await self.store.get(Engagement, project_id)).name
            except NotFoundError:
                # diagnostic-expected: a deleted project's incident is sent
                # under a generic name.
                continue
        return names

    async def _claim(
        self,
        incident: ButlerIncident,
        delivery: Literal["baseline", "summary", "alert", "no_devices"],
        *,
        notified: bool,
        existing: PushIncidentDelivery | None = None,
    ) -> None:
        now = utc_now()
        if existing is not None:
            await self.store.update(
                PushIncidentDelivery,
                existing.id,
                {
                    "notified_at": now if notified else None,
                    "attempts": existing.attempts + 1,
                    "delivery": delivery,
                },
            )
            return
        try:
            await self.store.create(
                PushIncidentDelivery(
                    id=delivery_id(incident.id),
                    incident_id=incident.id,
                    engagement_id=incident.engagement_id,
                    notified_at=now if notified else None,
                    delivery=delivery,
                    attempts=1 if delivery in ("alert", "summary") else 0,
                )
            )
        except ConflictError:
            # diagnostic-expected: an earlier pass already recorded this
            # episode; it is not alerted twice.
            return

    async def _forget(self, delivery_id: str) -> None:
        try:
            await self.store.delete(PushIncidentDelivery, delivery_id)
        except NotFoundError:
            # diagnostic-expected: already removed by an earlier pass.
            return


def start_push_dispatcher(
    store: NebulaStore,
    *,
    credentials: CredentialStore | None = None,
    name: str,
) -> tuple[ButlerPushDispatcher, asyncio.Task[Any] | None]:
    """Start the install's sender; ``NEBULA_BUTLER_PUSH=0`` turns it off."""
    from .diagnostics import create_diagnostic_task

    dispatcher = ButlerPushDispatcher(store, credentials=credentials)
    if os.environ.get("NEBULA_BUTLER_PUSH", "1").strip().lower() in {
        "0",
        "false",
        "off",
        "no",
    }:
        return dispatcher, None
    task = create_diagnostic_task(
        dispatcher.run(),
        feature="butler",
        event_code="butler.push.dispatcher",
        failure_message="Butler phone notifications stopped.",
        name=name,
    )
    return dispatcher, task


# ---------------------------------------------------------------------------
# API


class PushDeviceRegistration(NebulaModel):
    token: str = Field(pattern=r"^[0-9a-f]{16,200}$")
    environment: Literal["development", "production"]
    topic: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9.-]+$")
    platform: Literal["ios"] = "ios"


class PushDevicePatch(NebulaModel):
    enabled: bool


class PushSettingsUpdate(NebulaModel):
    key_ref: str | None = Field(default=None, max_length=300)
    key_id: str | None = Field(default=None, pattern=r"^[A-Z0-9]{10}$")
    team_id: str | None = Field(default=None, pattern=r"^[A-Z0-9]{10}$")
    topic: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9.-]+$"
    )
    enabled: bool | None = None
    expected_revision: int | None = Field(default=None, ge=1)


KEY_REF_PREFIXES = ("vault:", "store:", "systemd:", "env:")


def push_notifications_router(store: NebulaStore, actor: Any = None) -> APIRouter:
    """``actor`` is the global API's ContextVar (``device:<id>`` or ``operator``)
    where the request state does not carry the paired device."""
    router = APIRouter(tags=["notifications"])

    def caller(request: Request) -> str | None:
        value = getattr(request.state, "auth_device_id", None)
        if isinstance(value, str) and value:
            return value
        principal = actor.get() if actor is not None else ""
        if isinstance(principal, str) and principal.startswith("device:"):
            return principal.removeprefix("device:") or None
        return None

    @router.get("/notifications/push", response_model=None)
    async def read_push(request: Request, response: Response) -> dict[str, Any]:
        """Whether Core can send Butler alerts, and every registered phone."""
        response.headers["Cache-Control"] = "no-store"
        return await push_status(store, caller(request))

    @router.put("/notifications/push/settings", response_model=None)
    async def save_push_settings(
        request: Request, body: PushSettingsUpdate
    ) -> dict[str, Any]:
        """Save the APNs key reference, key ID, team ID, topic or the master switch.

        The key itself is saved first through ``POST /credentials``; only its
        reference is stored here.
        """
        if (
            body.key_ref is not None
            and body.key_ref
            and not body.key_ref.startswith(KEY_REF_PREFIXES)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "key_ref must be a vault:, store:, systemd: or env: credential reference.",
            )
        changes = {
            key: value
            for key, value in body.model_dump(exclude={"expected_revision"}).items()
            if key in body.model_fields_set
        }
        if "key_ref" in changes and not changes["key_ref"]:
            changes["key_ref"] = None
        changes["last_error"] = None
        changes["last_error_at"] = None
        try:
            current = await store.get(PushNotificationSettings, PUSH_SETTINGS_ID)
        except NotFoundError:
            # diagnostic-expected: the first save creates the settings row.
            current = None
        if current is None:
            try:
                await store.create(
                    PushNotificationSettings(id=PUSH_SETTINGS_ID, **changes)
                )
            except ConflictError:
                # diagnostic-expected: another save created the row first.
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Push settings changed. Reload and try again.",
                ) from None
        else:
            try:
                await store.update(
                    PushNotificationSettings,
                    PUSH_SETTINGS_ID,
                    changes,
                    expected_revision=body.expected_revision,
                )
            except ConflictError:
                # diagnostic-expected: a stale editor; the operator reloads.
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Push settings changed. Reload and try again.",
                ) from None
        return await push_status(store, caller(request))

    @router.put("/notifications/push/devices", response_model=None)
    async def register_push_device(
        request: Request, body: PushDeviceRegistration
    ) -> dict[str, Any]:
        """Register this phone's APNs token for Butler alerts (idempotent).

        The phone is tied to the calling paired device, so revoking that
        pairing stops its alerts. A token Apple retired is reactivated when
        the phone reports it again.
        """
        device_id = caller(request)
        if device_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Pair this phone with Nebula before turning on notifications.",
            )
        try:
            paired = await store.get(PairedDeviceSession, device_id)
            label = paired.name
        except NotFoundError:
            # diagnostic-expected: a host session without a pairing row.
            label = "iPhone"
        existing = await store.find_entity(PushDevice, "token", body.token)
        if existing is None:
            # One row per phone: a new token from the same pairing replaces the old.
            for old in await store.find_entities(
                PushDevice, {"paired_device_id": device_id}, bulk=True
            ):
                await store.delete(PushDevice, old.id)
            device = await store.create(
                PushDevice(
                    token=body.token,
                    environment=body.environment,
                    topic=body.topic,
                    paired_device_id=device_id,
                    label=label,
                )
            )
        else:
            changes: dict[str, Any] = {}
            for key, value in (
                ("environment", body.environment),
                ("topic", body.topic),
                ("paired_device_id", device_id),
                ("label", label),
            ):
                if getattr(existing, key) != value:
                    changes[key] = value
            if existing.unregistered_at is not None:
                changes.update(unregistered_at=None, last_error=None)
            device = (
                await store.update(PushDevice, existing.id, changes)
                if changes
                else existing
            )
        return _device_view(device, device_id)

    async def owned(device_id: str) -> PushDevice:
        try:
            return await store.get(PushDevice, device_id)
        except NotFoundError:
            # diagnostic-expected: an unknown or removed phone.
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "That phone is not registered for Butler notifications.",
            ) from None

    @router.patch("/notifications/push/devices/{device_id}", response_model=None)
    async def update_push_device(
        request: Request, device_id: str, body: PushDevicePatch
    ) -> dict[str, Any]:
        """Pause or resume Butler alerts for one phone."""
        device = await owned(device_id)
        updated = await store.update(PushDevice, device.id, {"enabled": body.enabled})
        return _device_view(updated, caller(request))

    @router.delete("/notifications/push/devices/{device_id}", status_code=204)
    async def delete_push_device(device_id: str) -> Response:
        """Stop sending Butler alerts to a phone; it registers again on next launch."""
        device = await owned(device_id)
        await store.delete(PushDevice, device.id)
        return Response(status_code=204)

    @router.post(
        "/notifications/push/devices/{device_id}/test",
        status_code=202,
        response_model=None,
    )
    async def test_push_device(request: Request, device_id: str) -> dict[str, Any]:
        """Queue one test alert. The dispatcher sends it within seconds and
        records Apple's answer as the phone's ``last_sent_at`` or ``last_error``."""
        device = await owned(device_id)
        settings = await read_settings(store)
        missing = missing_settings(settings)
        if missing or not settings.enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Core cannot send yet: missing {', '.join(missing) or 'the master switch'}.",
            )
        updated = await store.update(
            PushDevice, device.id, {"test_requested_at": utc_now()}
        )
        return _device_view(updated, caller(request))

    return router
