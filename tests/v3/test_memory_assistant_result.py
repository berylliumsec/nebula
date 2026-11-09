"""Large valid memory inventories retain usable structured Butler observations."""

import json

from fastapi import FastAPI
import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.butler_capabilities import ButlerCapabilities
from nebula.v3.domain import RunnerProfile
from nebula.v3.execution_scope import execution_context
from nebula.v3.memory_tools import memory_budget_tools
from nebula.v3.settings_assistant_api import SettingsAssistantApi
from nebula.v3.tools import ToolInvocation
from tests.v3.test_memory_api import core as core, PATH, TOKEN
from tests.v3.test_memory_budget import capability
from tests.v3.test_runtime_coordination import location as location
from tests.v3.test_runtime_memory_control import controls as controls, private_peer
from tests.v3.test_runtime_processes import managed as managed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def seed_jobs(service, project_id, count=100):
    for index in range(count):
        await service.create_job(
            f"job-{index:03}",
            project_id=project_id,
            resource_kind="inert",
            resource_id=f"synthetic-{index}",
            capabilities=capability(),
        )


async def test_large_real_core_job_page_remains_structured_and_partial_for_butler(
    core, tmp_path
):
    store, service, app, client = core
    await seed_jobs(service, "project")
    original = await client.get(PATH + "/jobs")
    assert len(original.content) > 30_000 and len(original.json()["items"]) == 100
    butler = await ButlerCapabilities.create(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        "project",
        settings_api=SettingsAssistantApi(app, TOKEN),
    )
    result = await butler.invoke(
        {"operation": "nebula.settings.memory_usage", "arguments": {}}, change=False
    )
    assert 0 < len(result["jobs"]) < 100
    assert result["at_limit"] is True
    assert result["display_limit"] == len(result["jobs"])
    assert all(
        job["usage_ratio"] is None and job["approaching_limit"] is None
        for job in result["jobs"]
    )
    assert len(json.dumps(result["jobs"])) < 30_000
    # Presentation does not remove the durable records or change HTTP models.
    assert len((await client.get(PATH + "/jobs")).json()["items"]) == 100


async def test_large_profile_metadata_preserves_every_policy_override_on_save(
    core, tmp_path
):
    store, _service, app, client = core
    profiles = [
        RunnerProfile(
            id="profile-" + "x" * 170 + f"-{index:03}",
            name="Synthetic " + "x" * 190,
            runtime="podman",
            executable="/inert/podman",
            platform="linux/amd64",
            isolation="rootless",
        )
        for index in range(90)
    ]
    await store.create_many(profiles)
    before = (await client.get(PATH)).json()
    policy = before["policy"]
    policy["profile_overrides"] = {
        profile.id: dict(policy["default_job"]) for profile in profiles
    }
    response = await client.put(
        PATH, json={"expected_revision": before["revision"], "policy": policy}
    )
    assert response.status_code == 200
    assert len(response.content) > 30_000
    butler = await ButlerCapabilities.create(
        store,
        ArtifactStore(tmp_path / "artifacts"),
        "project",
        settings_api=SettingsAssistantApi(app, TOKEN),
    )
    current = await butler.invoke(
        {"operation": "nebula.settings.memory_budget", "arguments": {}}, change=False
    )
    assert current["read_back"] is True and "result" in current
    assert current["result"]["profiles_at_limit"] is True
    assert (
        current["result"]["policy"]["profile_overrides"] == policy["profile_overrides"]
    )
    preserved = dict(current["result"]["policy"]["profile_overrides"])
    current["result"]["policy"]["default_job"]["max_bytes"] = 4 * 1024**3
    saved = await butler.invoke(
        {
            "operation": "nebula.settings.set_memory_budget",
            "arguments": {
                "expected_revision": current["result"]["revision"],
                "policy": current["result"]["policy"],
            },
        },
        change=True,
    )
    assert saved["read_back"] is True
    assert saved["result"]["policy"]["profile_overrides"] == preserved
    assert (await client.get(PATH)).json()["policy"] == saved["result"]["policy"]


async def test_large_memory_page_survives_actual_private_host_delegation(
    controls, tmp_path
):
    lease, client, remote = await private_peer(controls)
    try:
        await seed_jobs(controls.budgets, lease.project_id)
        gateway = SettingsAssistantApi(
            controls.root.shared_services.memory_control.app,
            controls.root.shared_services.memory_control.token,
            memory_request=remote.request,
        )
        butler = await ButlerCapabilities.create(
            controls.managed.store,
            ArtifactStore(tmp_path / "artifacts"),
            lease.project_id,
            settings_api=gateway,
        )
        with execution_context(lease):
            result = await butler.invoke(
                {"operation": "nebula.settings.memory_usage", "arguments": {}},
                change=False,
            )
        assert 0 < len(result["jobs"]) < 100 and result["at_limit"] is True
        assert all(job["snapshot"]["policy_revision"] >= 1 for job in result["jobs"])
        assert all(
            job["evidence"] is None and job["usage_ratio"] is None
            for job in result["jobs"]
        )
    finally:
        await client.close()


async def test_generic_large_get_still_reports_successful_readback_without_a_memory_projection():
    app = FastAPI()

    @app.get("/api/v1/voice/settings")
    async def large():
        return {"synthetic": "x" * 40_000}

    result = await SettingsAssistantApi(app, "inert-token").request(
        "project", method="GET", path="voice/settings"
    )
    assert result["read_back"] is True and result["truncated"] is True
    assert "result" not in result and len(result["result_excerpt"]) == 30_000


@pytest.mark.parametrize(
    "large_path", ["/api/v1/memory-budgets", "/api/v1/memory-budgets/jobs"]
)
async def test_typed_usage_reports_an_explicit_partial_result_for_an_oversized_gateway_envelope(
    large_path, tmp_path
):
    class Gateway:
        async def request(self, project_id, *, path, **kwargs):
            if "/api/v1/" + path == large_path:
                return {
                    "status": 200,
                    "read_back": True,
                    "truncated": True,
                    "result_excerpt": "inert bounded excerpt",
                }
            return {
                "status": 200,
                "read_back": True,
                "result": {"items": [], "policy": {}},
            }

    tool = next(
        item
        for item in memory_budget_tools(Gateway(), "project")
        if item.spec.name == "nebula.settings.memory_usage"
    )
    result = (
        await tool.execute(
            ToolInvocation(
                engagement_id="project",
                run_id="inert",
                tool_name=tool.spec.name,
                workspace=tmp_path,
            ),
            None,
        )
    ).output
    assert result["jobs"] == []
    if large_path.endswith("/jobs"):
        assert result["at_limit"] is True and result["display_limit"] == 0
    else:
        assert result["settings"]["truncated"] is True
