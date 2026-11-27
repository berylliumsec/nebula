"""Butler phone alerts: one APNs alert per open incident episode, per phone."""

from __future__ import annotations

from tests.v3.async_test_support import (
    anyio_backend as anyio_backend,
    close_database_handles as close_database_handles,
)

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import FastAPI, Request

from nebula.v3.domain import (
    ButlerIncident,
    Engagement,
    PairedDeviceSession,
    PushDevice,
    PushIncidentDelivery,
    PushNotificationSettings,
    utc_now,
)
from nebula.v3.push_notifications import (
    GROUP_THRESHOLD,
    PUSH_SETTINGS_ID,
    Alert,
    ApnsError,
    ApnsSender,
    ButlerPushDispatcher,
    alert_payload,
    provider_token,
    delivery_id,
    push_notifications_router,
)
from nebula.v3.storage import NebulaStore
from tests.v3.async_test_support import AsyncTestClient

pytestmark = pytest.mark.anyio

TOKEN = "ab" * 32
OTHER_TOKEN = "cd" * 32


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Alert]] = []
        self.fail_with: ApnsError | None = None

    async def send(self, device: PushDevice, alert: Alert) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((device.token, alert))


async def _paired(
    store: NebulaStore, *, name: str = "Operator iPhone", revoked: bool = False
) -> PairedDeviceSession:
    now = utc_now()
    return await store.create(
        PairedDeviceSession(
            name=name,
            token_sha256="0" * 64,
            csrf_sha256="1" * 64,
            idle_expires_at=now + timedelta(days=1),
            absolute_expires_at=now + timedelta(days=30),
            revoked_at=now if revoked else None,
        )
    )


async def _configure(store: NebulaStore) -> None:
    await store.create(
        PushNotificationSettings(
            id=PUSH_SETTINGS_ID,
            key_ref="vault:" + "a" * 32,
            key_id="ABC123DEFG",
            team_id="TEAM123456",
        )
    )


async def _phone(
    store: NebulaStore,
    paired: PairedDeviceSession,
    token: str = TOKEN,
    *,
    welcomed: bool = True,
) -> PushDevice:
    return await store.create(
        PushDevice(
            token=token,
            environment="development",
            topic="com.berylliumsec.nebula.ios",
            paired_device_id=paired.id,
            welcomed_at=utc_now() if welcomed else None,
        )
    )


async def _incident(
    store: NebulaStore,
    project: Engagement,
    key: str,
    summary: str = "Needs you: approve the probe.",
) -> ButlerIncident:
    return await store.create(
        ButlerIncident(
            id=f"incident-{key}",
            engagement_id=project.id,
            incident_key=key,
            source="operator_action",
            verification_state="core_observed",
            summary=summary,
            impact="The goal waits.",
            next_action="Approve it on the Butler board.",
            observed_at=utc_now(),
        )
    )


def _dispatcher(store: NebulaStore, sender: RecordingSender) -> ButlerPushDispatcher:
    return ButlerPushDispatcher(store, sender_factory=lambda settings: sender)


async def _ready(store: NebulaStore, sender: RecordingSender) -> ButlerPushDispatcher:
    """Configured, with the first-run baseline already taken."""
    await _configure(store)
    dispatcher = _dispatcher(store, sender)
    assert await dispatcher.run_pass()
    return dispatcher


async def test_unconfigured_core_sends_nothing(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Research"))
    await _phone(store, await _paired(store))
    await _incident(store, project, "a")
    sender = RecordingSender()
    assert await _dispatcher(store, sender).run_pass() is False
    assert sender.sent == []


async def test_incidents_open_before_the_first_pass_are_a_silent_baseline(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Research"))
    await _phone(store, await _paired(store))
    old = await _incident(store, project, "old")
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)
    assert sender.sent == []
    assert (
        await store.get(PushIncidentDelivery, delivery_id(old.id))
    ).delivery == "baseline"
    await dispatcher.run_pass()
    assert sender.sent == []


async def test_new_incident_alerts_each_phone_once_and_opens_oversight(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Research Lab"))
    await _phone(store, await _paired(store))
    await _phone(store, await _paired(store, name="iPad"), OTHER_TOKEN)
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)

    incident = await _incident(store, project, "probe")
    await dispatcher.run_pass()
    assert sorted(token for token, _ in sender.sent) == sorted([TOKEN, OTHER_TOKEN])
    alert = sender.sent[0][1]
    assert alert.title == "Research Lab needs you"
    assert "approve the probe" in alert.body and "Next: Approve it" in alert.body
    assert alert.path == f"/butler?project={project.id}&section=oversight"
    assert alert.collapse_id == incident.id

    # Updates while it stays open never alert again.
    await store.update(
        ButlerIncident, incident.id, {"summary": "Needs you: still waiting."}
    )
    await dispatcher.run_pass()
    assert len(sender.sent) == 2


async def test_reopened_incident_alerts_again_but_acknowledged_does_not(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Research"))
    await _phone(store, await _paired(store))
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)
    incident = await _incident(store, project, "loop")
    await dispatcher.run_pass()
    assert len(sender.sent) == 1

    await store.update(ButlerIncident, incident.id, {"status": "acknowledged"})
    await dispatcher.run_pass()
    await store.update(ButlerIncident, incident.id, {"status": "open"})
    await dispatcher.run_pass()
    assert len(sender.sent) == 1, (
        "acknowledging then reopening without resolving is the same episode"
    )

    await store.update(
        ButlerIncident, incident.id, {"status": "resolved", "resolved_at": utc_now()}
    )
    await dispatcher.run_pass()
    await store.update(
        ButlerIncident, incident.id, {"status": "open", "resolved_at": None}
    )
    await dispatcher.run_pass()
    assert len(sender.sent) == 2


async def test_a_burst_becomes_one_grouped_alert(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    first = await store.create(Engagement(name="Research"))
    second = await store.create(Engagement(name="Lab"))
    await _phone(store, await _paired(store))
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)
    for index in range(GROUP_THRESHOLD + 1):
        await _incident(store, first if index % 2 else second, f"burst-{index}")
    await dispatcher.run_pass()
    assert len(sender.sent) == 1
    alert = sender.sent[0][1]
    assert alert.title == f"{GROUP_THRESHOLD + 1} Butler items need you"
    assert "Lab" in alert.body and "Research" in alert.body
    await dispatcher.run_pass()
    assert len(sender.sent) == 1


async def test_revoked_pairing_paused_phone_and_dead_token_get_nothing(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Research"))
    await _phone(store, await _paired(store, revoked=True))
    paused = await _phone(store, await _paired(store), OTHER_TOKEN)
    await store.update(PushDevice, paused.id, {"enabled": False})
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)
    await _incident(store, project, "x")
    await dispatcher.run_pass()
    assert sender.sent == []

    live = await _phone(store, await _paired(store), "ef" * 32)
    sender.fail_with = ApnsError(410, "Unregistered")
    await _incident(store, project, "y")
    await dispatcher.run_pass()
    device = await store.get(PushDevice, live.id)
    assert device.unregistered_at is not None
    assert device.last_error == "Apple refused the alert: Unregistered"


async def test_new_phone_gets_one_welcome_with_the_open_count(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Research"))
    await _incident(store, project, "backlog")
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)
    phone = await _phone(store, await _paired(store), welcomed=False)
    await dispatcher.run_pass()
    await dispatcher.run_pass()
    assert [alert.title for _, alert in sender.sent] == ["Butler notifications are on"]
    assert sender.sent[0][1].body.startswith("1 Butler item already open")
    assert (await store.get(PushDevice, phone.id)).welcomed_at is not None


async def test_failed_alert_is_retried_then_given_up(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    project = await store.create(Engagement(name="Research"))
    await _phone(store, await _paired(store))
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)
    incident = await _incident(store, project, "flaky")
    sender.fail_with = ApnsError(500, "InternalServerError")
    for _ in range(5):
        await dispatcher.run_pass()
    assert (
        await store.get(PushIncidentDelivery, delivery_id(incident.id))
    ).attempts == 3
    sender.fail_with = None
    await dispatcher.run_pass()
    assert sender.sent == []


async def test_provider_token_is_a_valid_es256_jwt():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    token = provider_token(pem, "ABC123DEFG", "TEAM123456", 1_700_000_000)
    header, claims, signature = token.split(".")

    def decode(part: str) -> bytes:
        import base64

        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    assert json.loads(decode(header)) == {"alg": "ES256", "kid": "ABC123DEFG"}
    assert json.loads(decode(claims)) == {"iss": "TEAM123456", "iat": 1_700_000_000}
    raw = decode(signature)
    assert len(raw) == 64
    key.public_key().verify(
        encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        ),
        f"{header}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )
    with pytest.raises(ValueError):
        provider_token(
            ec.generate_private_key(ec.SECP384R1())
            .private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            .decode(),
            "ABC123DEFG",
            "TEAM123456",
            1,
        )


async def test_apns_sender_posts_to_the_sandbox_with_apple_headers():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith(OTHER_TOKEN):
            return httpx.Response(400, json={"reason": "BadDeviceToken"})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = ApnsSender(pem, "ABC123DEFG", "TEAM123456", client)
        device = PushDevice(
            token=TOKEN,
            environment="development",
            topic="com.berylliumsec.nebula.ios",
            paired_device_id="d",
        )
        alert = Alert(
            title="Research needs you",
            body="Approve it.",
            path="/butler?project=p&section=oversight",
            thread="p",
            collapse_id="i",
        )
        await sender.send(device, alert)
        with pytest.raises(ApnsError) as refused:
            await sender.send(device.model_copy(update={"token": OTHER_TOKEN}), alert)
    assert refused.value.reason == "BadDeviceToken"
    request = seen[0]
    assert str(request.url) == f"https://api.sandbox.push.apple.com/3/device/{TOKEN}"
    assert request.headers["apns-topic"] == "com.berylliumsec.nebula.ios"
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-collapse-id"] == "i"
    assert request.headers["authorization"].startswith("bearer ey")
    body = json.loads(request.content)
    assert body == alert_payload(alert)
    assert body["nebula_path"] == "/butler?project=p&section=oversight"
    assert body["aps"]["alert"]["title"] == "Research needs you"


def _app(store: NebulaStore, device_id: str | None) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any):
        request.state.auth_device_id = device_id
        return await call_next(request)

    app.include_router(push_notifications_router(store), prefix="/api/v1")
    return app


async def test_phone_registers_pauses_tests_and_reads_status(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    paired = await _paired(store)
    registration = {
        "token": TOKEN,
        "environment": "development",
        "topic": "com.berylliumsec.nebula.ios",
        "platform": "ios",
    }
    async with AsyncTestClient(_app(store, paired.id)) as client:
        status = (await client.get("/api/v1/notifications/push")).json()
        assert status["configured"] is False
        assert status["missing"] == ["APNs key", "key ID", "team ID"]

        first = await client.put(
            "/api/v1/notifications/push/devices", json=registration
        )
        assert first.status_code == 200, first.text
        again = await client.put(
            "/api/v1/notifications/push/devices", json=registration
        )
        assert again.json()["id"] == first.json()["id"]
        assert (
            first.json()["this_device"] is True
            and first.json()["label"] == "Operator iPhone"
        )

        # A new token from the same phone replaces its old row.
        replaced = await client.put(
            "/api/v1/notifications/push/devices",
            json={**registration, "token": OTHER_TOKEN},
        )
        devices = (await client.get("/api/v1/notifications/push")).json()["devices"]
        assert [item["id"] for item in devices] == [replaced.json()["id"]]

        refused = await client.post(
            f"/api/v1/notifications/push/devices/{replaced.json()['id']}/test"
        )
        assert refused.status_code == 409

        saved = await client.put(
            "/api/v1/notifications/push/settings",
            json={
                "key_ref": "vault:" + "b" * 32,
                "key_id": "ABC123DEFG",
                "team_id": "TEAM123456",
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["configured"] is True
        bad = await client.put(
            "/api/v1/notifications/push/settings", json={"key_ref": "/etc/key.p8"}
        )
        assert bad.status_code == 422

        queued = await client.post(
            f"/api/v1/notifications/push/devices/{replaced.json()['id']}/test"
        )
        assert queued.status_code == 202 and queued.json()["test_pending"] is True
        paused = await client.patch(
            f"/api/v1/notifications/push/devices/{replaced.json()['id']}",
            json={"enabled": False},
        )
        assert paused.json()["enabled"] is False


async def test_unpaired_caller_cannot_register(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    async with AsyncTestClient(_app(store, None)) as client:
        answer = await client.put(
            "/api/v1/notifications/push/devices",
            json={
                "token": TOKEN,
                "environment": "production",
                "topic": "com.berylliumsec.nebula.ios",
            },
        )
    assert answer.status_code == 403


async def test_queued_test_alert_is_sent_once(tmp_path):
    store = NebulaStore(tmp_path / "core.db")
    phone = await _phone(store, await _paired(store))
    sender = RecordingSender()
    dispatcher = await _ready(store, sender)
    await store.update(PushDevice, phone.id, {"test_requested_at": utc_now()})
    await dispatcher.run_pass()
    await dispatcher.run_pass()
    assert [alert.title for _, alert in sender.sent] == ["Test from Butler"]
    assert (await store.get(PushDevice, phone.id)).last_sent_at is not None


def test_butler_opens_phone_notification_settings_without_the_key():
    """Butler can find and open Push notifications; the APNs key stays a device secret."""
    from nebula.v3.settings_assistant_tools import catalog, open_setting

    entry = next(
        item for item in catalog()["settings"] if item["id"] == "push-notifications"
    )
    assert entry["url"] == "/settings#push-notification-settings"
    action = open_setting({"setting_id": "push-notifications"})["client_action"]
    assert action["setting_id"] == "push-notifications"


def test_product_help_explains_butler_push_notifications(monkeypatch):
    """Butler's product help (bundled index) answers how to get alerts on the phone."""
    from nebula.v3.product_knowledge import search_product_knowledge

    monkeypatch.setattr("nebula.v3.product_knowledge._dense_query", lambda _: None)
    for question, expected in (
        (
            "How do I get Butler notifications on my iPhone when something is blocked?",
            "PUSH-NOTIFICATIONS",
        ),
        ("Where do I add the APNs key for push notifications?", "PUSH-NOTIFICATIONS"),
    ):
        results = search_product_knowledge(question, limit=5)
        assert any(expected in str(item.get("source", "")) for item in results), (
            question
        )
