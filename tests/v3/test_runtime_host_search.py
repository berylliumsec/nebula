"""Real signed search RPC, project policy races and query-only ownership."""

import asyncio
from dataclasses import asdict
from datetime import timedelta
import secrets
from types import SimpleNamespace

import httpx
import pytest

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    ChatSession,
    ChatTurn,
    Engagement,
    ScopePolicy,
    ToolCall,
    ToolCallOrigin,
    WebSearchEngine,
    WebSearchSettings,
)
from nebula.v3.domain import utc_now
from nebula.v3.execution_scope import execution_context
from nebula.v3.memory_policy import MemoryBudgetConflict, MemoryEnforcementUnavailable
from nebula.v3.runtime_coordination import LEASE_MS, ProjectLease
from nebula.v3.runtime_host_search import (
    HOST_SEARCH_QUERY,
    RemoteProjectSearch,
    register_host_search,
)
from nebula.v3.runtime_ipc import RuntimeIpcError, RuntimeRpcClient, RuntimeRpcServer
from nebula.v3.runtime_platform import RuntimePlatform, default_runtime_platform
from nebula.v3.tools import ToolInvocation
from nebula.v3.web_search import (
    SETTINGS_ID,
    SearchRuntimeUnavailable,
    UnavailableProjectSearch,
    WebSearchError,
    WebSearchResult,
    WebSearchTool,
)
from tests.v3.test_runtime_coordination import cluster as cluster
from tests.v3.test_runtime_coordination import location as location

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class InertHostSearch:
    def __init__(self):
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error = None
        self.closed = False

    async def search(self, query, *, count=5, freshness="any"):
        self.calls.append((query, count, freshness))
        self.entered.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return [
            WebSearchResult(
                rank=1,
                title="Synthetic public advisory",
                url="https://public.example/advisory",
                snippet="Inert result from the canonical host service.",
                engine="brave",
            )
        ]

    async def close(self):
        self.closed = True


@pytest.fixture
async def search(cluster):
    (coordination, _), (store, _), clock = cluster
    scope = await store.create(
        ScopePolicy(id="search-scope", engagement_id="project", web_search=True)
    )
    await store.update(Engagement, "project", {"scope_policy_id": scope.id})
    lease = await coordination.claim_project("project", "old")
    runtime = InertHostSearch()
    key = secrets.token_bytes(32)
    rpc = RuntimeRpcServer(key)
    register_host_search(rpc, lease.runtime_id, coordination, runtime)
    client = RuntimeRpcClient(
        "unix:///synthetic/search.sock", key, transport=httpx.ASGITransport(app=rpc.app)
    )
    remote = RemoteProjectSearch(client, store, lease)
    try:
        yield SimpleNamespace(
            store=store,
            coordination=coordination,
            clock=clock,
            scope=scope,
            lease=lease,
            runtime=runtime,
            rpc=rpc,
            client=client,
            remote=remote,
        )
    finally:
        await remote.close()
        await client.close()


def payload(search, **changes):
    result = {
        "lease": asdict(search.lease),
        "scope_policy_id": search.scope.id,
        "scope_revision": search.scope.revision,
        "query": "public advisory",
        "count": 1,
        "freshness": "week",
    }
    result.update(changes)
    return result


def invocation(tmp_path):
    return ToolInvocation(
        engagement_id="project",
        run_id="synthetic-run",
        origin=ToolCallOrigin.CHAT,
        chat_session_id="synthetic-chat",
        chat_turn_id="synthetic-run",
        tool_name="web.search",
        workspace=tmp_path,
        arguments={"query": "public advisory", "count": 1, "freshness": "week"},
    )


async def test_platform_tool_uses_the_signed_host_query_and_durable_receipt(
    search, tmp_path
):
    await search.store.create(
        ChatSession(
            id="synthetic-chat",
            engagement_id="project",
            title="Public research",
            provider_profile_id="synthetic-provider",
            model="inert",
        )
    )
    await search.store.create(
        ChatTurn(
            id="synthetic-run",
            engagement_id="project",
            session_id="synthetic-chat",
            provider_profile_id="synthetic-provider",
            model="inert",
            tools_enabled=True,
            max_tool_calls=1,
        )
    )
    platform = default_runtime_platform(
        store=search.store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "platform",
        search_runtime=search.remote,
    )
    with execution_context(search.lease):
        components = await platform.chat_components(
            engagement_id="project",
            turn_id="synthetic-run",
            provider=None,
            model="inert",
        )
        result = await components.broker.execute(
            invocation(components.workspace), components.scope
        )
        assert result.exit_code == 0
        assert result.output["schema"] == "nebula.tool-result/v2"
        observation = next(
            item
            for item in result.output["observations"]
            if item["kind"] == "web_result"
        )
        assert observation["url"] == "https://public.example/advisory"
        assert observation["snippet"].startswith("Inert result")
    assert platform.search_runtime is search.remote
    assert search.runtime.calls == [("public advisory", 1, "week")]
    calls = await search.store.list_entities(ToolCall, engagement_id="project")
    assert len(calls) == 1
    assert calls[0].status.value == "complete"
    assert calls[0].chat_turn_id == "synthetic-run"
    assert calls[0].result == result.output
    assert calls[0].result_artifact_id == result.result_artifact_id


async def test_remote_requires_the_exact_bound_project_context_before_rpc(search):
    grants = [
        None,
        ProjectLease("other", search.lease.runtime_id, search.lease.fencing_token),
        ProjectLease("project", "new", search.lease.fencing_token),
        ProjectLease("project", "old", search.lease.fencing_token + 1),
    ]
    for lease in grants:
        with execution_context(lease):
            with pytest.raises(SearchRuntimeUnavailable, match="execution grant"):
                await search.remote.search("public advisory")
    assert search.runtime.calls == []


async def test_host_rejects_unbounded_untyped_and_cross_grant_requests(search):
    invalid = [
        {"count": True},
        {"count": 11},
        {"query": "x" * 257},
        {"freshness": "forever"},
        {"scope_revision": search.scope.revision + 1},
        {"scope_policy_id": "other"},
        {"unexpected": "not accepted"},
        {"lease": {**asdict(search.lease), "runtime_id": "new"}},
        {"lease": {**asdict(search.lease), "project_id": "other"}},
    ]
    for changes in invalid:
        with pytest.raises(RuntimeIpcError, match="403"):
            await search.client.call(HOST_SEARCH_QUERY, payload(search, **changes))
    assert search.runtime.calls == []


@pytest.mark.parametrize(
    "changes,query",
    [
        ({"web_search": False}, "public advisory"),
        ({"local_only": True}, "public advisory"),
        ({"allowed_domains": ["client.example"]}, "client.example advisory"),
        ({"prohibited_actions": ["web.search"]}, "public advisory"),
        ({"not_after": utc_now() - timedelta(seconds=1)}, "public advisory"),
    ],
)
async def test_host_rechecks_authoritative_policy_before_any_egress(
    search, changes, query
):
    scope = await search.store.update(ScopePolicy, search.scope.id, changes)
    with pytest.raises(RuntimeIpcError, match="403"):
        await search.client.call(
            HOST_SEARCH_QUERY,
            payload(search, scope_revision=scope.revision, query=query),
        )
    with execution_context(search.lease):
        with pytest.raises(SearchRuntimeUnavailable, match="current project policy"):
            await search.remote.search(query)
    assert search.runtime.calls == []


@pytest.mark.parametrize("change", ["scope", "engines", "lease"])
async def test_query_wait_releases_sql_and_rejects_revocation_before_return(
    search, change
):
    search.runtime.release.clear()
    task = asyncio.create_task(search.client.call(HOST_SEARCH_QUERY, payload(search)))
    await asyncio.wait_for(search.runtime.entered.wait(), timeout=3)
    try:
        # These writes must finish while the host query is blocked. Holding the
        # ownership transaction through admission/HTTP would deadlock here.
        async with asyncio.timeout(3):
            if change == "scope":
                await search.store.update(
                    ScopePolicy, search.scope.id, {"web_search": False}
                )
            elif change == "engines":
                await search.store.create(
                    WebSearchSettings(id=SETTINGS_ID, engines=[WebSearchEngine.GITHUB])
                )
            else:
                search.clock[0] += LEASE_MS + 1
                await search.coordination.register_runtime("replacement", "synthetic")
                await search.coordination.claim_project("project", "replacement")
        search.runtime.release.set()
        with pytest.raises(RuntimeIpcError, match="403"):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(search.runtime.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        MemoryBudgetConflict("Search memory admission is unavailable"),
        MemoryEnforcementUnavailable("Search enforcement is unavailable"),
        WebSearchError("Search could not become ready; retry explicitly"),
    ],
)
async def test_failed_host_admission_never_falls_back_and_preserves_job_evidence(
    search, tmp_path, error
):
    search.runtime.error = error
    error.memory_job_id = "synthetic-memory-job"
    error.memory_state = "finished"
    error.memory_outcome = "memory_limit_exceeded"
    with execution_context(search.lease):
        result = await WebSearchTool(search.remote, search.scope).execute(
            invocation(tmp_path), None
        )
    assert result.exit_code == 1
    assert str(error) in result.output["detail"]
    assert result.execution["memory_job_id"] == "synthetic-memory-job"
    assert result.execution["memory_outcome"] == "memory_limit_exceeded"
    assert len(search.runtime.calls) == 1
    assert not search.runtime.closed


async def test_cancelled_host_queue_stays_cancelled_without_retry_or_local_owner(
    search,
):
    search.runtime.release.clear()
    with execution_context(search.lease):
        task = asyncio.create_task(search.remote.search("public advisory"))
    await asyncio.wait_for(search.runtime.entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(search.runtime.calls) == 1
    assert not search.runtime.closed


async def test_remote_rejects_failed_transport_and_invalid_signed_result(search):
    class BrokenClient:
        value = None

        async def call(self, operation, body):
            assert operation == HOST_SEARCH_QUERY
            if isinstance(self.value, Exception):
                raise self.value
            return self.value

    client = BrokenClient()
    remote = RemoteProjectSearch(client, search.store, search.lease)
    valid = {
        **payload(search),
        "results": [],
        "failure": None,
    }
    for key in ("query", "count", "freshness"):
        valid.pop(key)
    cases = [
        httpx.ConnectError("synthetic host unavailable"),
        RuntimeIpcError("synthetic missing capability"),
        {**valid, "lease": {**valid["lease"], "fencing_token": 99}},
        {**valid, "scope_revision": 99},
        {**valid, "results": [{"rank": 1, "title": "bad", "url": "file:///private"}]},
        {
            **valid,
            "results": [
                {"rank": 1, "title": "x" * 201, "url": "https://public.example"}
            ],
        },
    ]
    with execution_context(search.lease):
        for value in cases:
            client.value = value
            with pytest.raises(SearchRuntimeUnavailable, match="could not answer"):
                await remote.search("public advisory")
    assert search.runtime.calls == []


async def test_project_close_does_not_close_the_shared_host_client(search):
    await search.remote.close()
    with execution_context(search.lease):
        assert (await search.remote.search("public advisory"))[0].rank == 1
    assert not search.runtime.closed


async def test_default_platform_never_constructs_a_search_service(
    search, tmp_path, monkeypatch
):
    from nebula.v3 import web_search

    def forbidden_owner(*args, **kwargs):
        raise AssertionError(
            "A project must never construct or adopt the host search owner"
        )

    monkeypatch.setattr(web_search, "SearchRuntime", forbidden_owner)
    for factory in (RuntimePlatform, default_runtime_platform):
        platform = factory(
            store=search.store,
            artifact_store=ArtifactStore(tmp_path / "artifacts"),
            data_root=tmp_path / factory.__name__,
        )
        assert isinstance(platform.search_runtime, UnavailableProjectSearch)
        result = await WebSearchTool(platform.search_runtime, search.scope).execute(
            invocation(tmp_path), None
        )
        assert result.exit_code == 1
        assert "host search service is unavailable" in result.output["detail"]
    assert search.runtime.calls == []
