"""Application Model API, tools and reset inside a guarded execution worker."""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI

from nebula.v3.application_model.api import model_router
from nebula.v3.application_model.service import ApplicationModelService
from nebula.v3.application_model.tools import ModelBroker
from nebula.v3.domain import Observation, ToolCallOrigin
from nebula.v3.execution_scope import execution_context
from nebula.v3.storage import NotFoundError
from nebula.v3.tools import ToolInvocation
from tests.v3.async_test_support import AsyncTestClient
from tests.v3.test_execution_scope import (
    anyio_backend as anyio_backend,
    execution as execution,
)
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


def _model_object(identifier, revision, key):
    return {
        "expected_revision": revision,
        "idempotency_key": key,
        "operations": [
            {
                "op": "put_object",
                "id": identifier,
                "properties": {
                    "purpose": {
                        "value": "Define the application boundary for its login workflow."
                    }
                },
                "label": identifier.title(),
                "authentication_context": "anonymous",
                "classification": {"value": "Application"},
            }
        ],
    }


async def test_application_model_api_and_tools_round_trip_in_a_worker(
    execution, tmp_path
):
    control, runtime, _, lease = execution
    for project in ("mine", "other"):
        await control.create(
            Observation(
                id=f"capture-{project}",
                engagement_id=project,
                title="Capture",
                observation_type="browser_capture",
                source="browser_companion",
            )
        )
    service = ApplicationModelService(runtime)
    runtime.application_model_service = service
    app = FastAPI()
    app.include_router(model_router(service))
    client = AsyncTestClient(app)
    base = "/engagements/mine/application-model"
    with execution_context(lease):
        assert (await client.get(base + "/graph")).json()["objects"] == []
        saved = await client.post(
            base + "/transactions", json=_model_object("site", 0, "save")
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == 1
        retried = await client.post(
            base + "/transactions", json=_model_object("site", 0, "save")
        )
        assert retried.json() == saved.json()
        for path in (
            "/schema",
            "/view",
            "/search",
            "/objects/site/neighborhood",
            "/evidence",
            "/updates",
            "/reset-preview",
        ):
            response = await client.get(base + path)
            assert response.status_code == 200, (path, response.text)
        assert [
            item["id"] for item in (await client.get(base + "/graph")).json()["objects"]
        ] == ["site"]
        assert (await client.get(base + "/updates")).json()["edits"][0]["revision"] == 1
        assert (await client.get(base + "/reset-preview")).json() == {
            "revision": 1,
            "objects": 1,
            "relationships": 0,
            "captures": 1,
            "custom_definitions": 0,
        }

        broker = ModelBroker(runtime, engagement_id="mine")

        async def invoke(name, arguments):
            return await broker.execute(
                ToolInvocation(
                    engagement_id="mine",
                    run_id="turn-mine",
                    origin=ToolCallOrigin.CHAT,
                    chat_session_id="chat-mine",
                    chat_turn_id="turn-mine",
                    tool_name=name,
                    arguments=arguments,
                    workspace=tmp_path,
                ),
                Mock(engagement_id="mine"),
            )

        transacted = await invoke("model.transact", _model_object("page", 1, "tool"))
        assert transacted.output["revision"] == 2
        assert sorted(
            item["id"] for item in (await invoke("model.search", {})).output["objects"]
        ) == ["page", "site"]
        assert [
            edit["producer"]
            for edit in (await invoke("model.get_updates", {})).output["edits"]
        ] == ["operator", "assistant"]
        assert len((await invoke("model.discover_schema", {})).output["types"]) > 0

        cleared = await client.post(
            base + "/reset",
            json={
                "expected_revision": 2,
                "idempotency_key": "clear",
                "confirmation": "clear-model-and-browser-captures",
            },
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json() == {"revision": 3, "cleared_captures": 1}
        assert (await client.get(base + "/graph")).json()["objects"] == []
        foreign = "/engagements/other/application-model"
        assert (await client.get(foreign + "/graph")).status_code == 404
        assert (await client.get(foreign + "/reset-preview")).status_code == 404
    # The worker cleared only its own project's captures.
    with pytest.raises(NotFoundError):
        await control.get(Observation, "capture-mine")
    assert (await control.get(Observation, "capture-other")).engagement_id == "other"
