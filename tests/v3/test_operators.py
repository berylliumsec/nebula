from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from nebula.v3.api import create_app
from nebula.v3.domain import Engagement, Evidence, OperatorProfile
from nebula.v3.operators import OperatorProfileService
from nebula.v3.storage import NebulaStore


def _auth():
    return {"Authorization": "Bearer test-token"}


def test_operator_profile_api_maintains_exactly_one_active_profile(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, auth_token="test-token"))

    assert client.get("/api/v1/operator-profiles", headers=_auth()).json() == []
    assert (
        client.get("/api/v1/operator-profiles/active", headers=_auth()).status_code
        == 404
    )

    first = client.post(
        "/api/v1/operator-profiles",
        headers=_auth(),
        json={
            "display_name": "  Jordan   Diaz ",
            "email": "jordan@example.test",
            "role": "Lead operator",
            "metadata": {"theme": "dark"},
        },
    )
    assert first.status_code == 201
    jordan = first.json()
    assert jordan["display_name"] == "Jordan Diaz"
    assert jordan["active"] is True
    assert jordan["activated_at"] is not None

    second = client.post(
        "/api/v1/operator-profiles",
        headers=_auth(),
        json={"display_name": "Alex Morgan", "role": "Reviewer"},
    )
    assert second.status_code == 201
    alex = second.json()
    assert alex["active"] is False
    assert [
        profile["id"]
        for profile in client.get("/api/v1/operator-profiles", headers=_auth()).json()
    ] == [jordan["id"], alex["id"]]

    updated = client.patch(
        f"/api/v1/operator-profiles/{alex['id']}",
        headers=_auth(),
        json={"role": "Senior reviewer", "expected_revision": 1},
    )
    assert updated.status_code == 200
    alex = updated.json()
    assert alex["revision"] == 2
    assert alex["role"] == "Senior reviewer"
    stale = client.patch(
        f"/api/v1/operator-profiles/{alex['id']}",
        headers=_auth(),
        json={"role": "Stale", "expected_revision": 1},
    )
    assert stale.status_code == 409

    activated = client.post(
        f"/api/v1/operator-profiles/{alex['id']}/activate",
        headers=_auth(),
        json={"expected_revision": 2},
    )
    assert activated.status_code == 200
    alex = activated.json()
    assert alex["active"] is True
    assert alex["revision"] == 3
    persisted_jordan = store.get(OperatorProfile, jordan["id"])
    assert persisted_jordan.active is False
    assert persisted_jordan.revision == 2
    assert (
        client.get("/api/v1/operator-profiles/active", headers=_auth()).json()["id"]
        == alex["id"]
    )

    idempotent = client.post(
        f"/api/v1/operator-profiles/{alex['id']}/activate",
        headers=_auth(),
        json={"expected_revision": 3},
    )
    assert idempotent.status_code == 200
    assert idempotent.json()["revision"] == 3
    assert (
        client.delete(
            f"/api/v1/operator-profiles/{alex['id']}",
            headers={**_auth(), "If-Match": "3"},
        ).status_code
        == 409
    )

    deleted = client.delete(
        f"/api/v1/operator-profiles/{jordan['id']}",
        headers={**_auth(), "If-Match": "2"},
    )
    assert deleted.status_code == 204
    assert (
        client.delete(
            f"/api/v1/operator-profiles/{alex['id']}", headers=_auth()
        ).status_code
        == 409
    )
    assert (
        client.put(
            f"/api/v1/operator-profiles/{alex['id']}",
            headers=_auth(),
            json=alex,
        ).status_code
        == 405
    )


def test_operator_profile_requests_cannot_write_activation_or_invalid_identity(
    tmp_path,
):
    store = NebulaStore(tmp_path / "nebula.db")
    client = TestClient(create_app(store, auth_token="test-token"))

    explicit_active = client.post(
        "/api/v1/operator-profiles",
        headers=_auth(),
        json={"display_name": "Injected", "active": True},
    )
    assert explicit_active.status_code == 422
    invalid_email = client.post(
        "/api/v1/operator-profiles",
        headers=_auth(),
        json={"display_name": "Invalid", "email": "not-an-email"},
    )
    assert invalid_email.status_code == 422
    assert store.count(OperatorProfile) == 0


def test_concurrent_first_profiles_still_have_one_active_operator(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    service = OperatorProfileService(store)

    with ThreadPoolExecutor(max_workers=2) as executor:
        profiles = list(
            executor.map(
                lambda name: service.create_profile(display_name=name),
                ["First", "Second"],
            )
        )

    persisted = service.list_profiles()
    assert {profile.id for profile in profiles} == {profile.id for profile in persisted}
    assert sum(profile.active for profile in persisted) == 1


def test_explicit_activation_repairs_a_legacy_incoherent_profile_set(tmp_path):
    store = NebulaStore(tmp_path / "nebula.db")
    first = store.create(OperatorProfile(display_name="Legacy one"))
    second = store.create(OperatorProfile(display_name="Legacy two"))
    service = OperatorProfileService(store)

    try:
        service.list_profiles()
    except Exception as exc:
        assert "exactly one active" in str(exc)
    else:
        raise AssertionError("incoherent profiles should not be presented as valid")

    active = service.activate_profile(second.id)
    assert active.id == second.id
    assert active.active is True
    assert store.get(OperatorProfile, first.id).active is False
    assert sum(profile.active for profile in service.list_profiles()) == 1


def test_operator_with_durable_attribution_cannot_be_deleted(tmp_path):
    store = NebulaStore(tmp_path / "attribution.db")
    service = OperatorProfileService(store)
    original = service.create_profile(display_name="Original operator")
    replacement = service.create_profile(display_name="Replacement operator")
    engagement = store.create(Engagement(name="Attributed work"))
    store.create(
        Evidence(
            engagement_id=engagement.id,
            evidence_type="operator-note",
            title="Attributed evidence",
            captured_by=original.id,
        )
    )
    service.activate_profile(replacement.id)

    response = TestClient(create_app(store, auth_token="test-token")).delete(
        f"/api/v1/operator-profiles/{original.id}",
        headers=_auth(),
    )

    assert response.status_code == 409
    assert "durable attribution" in response.json()["detail"]
    assert store.get(OperatorProfile, original.id).id == original.id
