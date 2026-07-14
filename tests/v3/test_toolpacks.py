import asyncio
import base64
import json
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from nebula.v3.database import Database
from nebula.v3.artifacts import ArtifactStore
from nebula.v3.domain import (
    AgentRun,
    Engagement,
    EngagementToolAssignment,
    RiskClass,
    RunnerIsolation,
    RunnerProfile,
    RunnerRuntime,
    ScopePolicy,
    ToolPackInstallationStatus,
    ToolPackInstallation,
    ToolPackTrust,
    utc_now,
)
from nebula.v3.sandbox import ContainerSandboxRunner, PreparedContainerImage
from nebula.v3.storage import NebulaStore
from nebula.v3.tool_platform import (
    ToolPlatform,
    ToolPlatformError,
    default_tool_platform,
)
from nebula.v3.toolpacks import (
    CatalogLoadResult,
    Ed25519Keyring,
    ImmutableManifestStore,
    RuntimeImageInfo,
    RuntimeSmokeResult,
    SignatureEnvelope,
    SignatureVerificationError,
    ToolCatalogClient,
    ToolCatalogEntry,
    ToolCatalogV1,
    ToolPackInstallError,
    ToolPackInstaller,
    ParserContainerCommandTool,
    ToolPackManifestV1,
    ToolPackValidationError,
    build_tool_registry,
    canonical_catalog_json,
    canonical_manifest_json,
    compile_manifest_yaml,
    default_tool_pack_root,
    fetch_bounded_https,
    manifest_digest,
)
from nebula.v3.tools import (
    AnalysisTool,
    InvalidToolArguments,
    ToolRegistry,
    ToolSpec,
    build_declared_command,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DIGEST_C = "c" * 64


def manifest_source(*, publisher="example", second_platform=True):
    arm = (
        f"""
  - name: sample
    platform: linux/arm64
    image: example.invalid/tools/sample@sha256:{DIGEST_B}
    sbom: sbom-arm64.json
    provenance: provenance-arm64.json
"""
        if second_platform
        else ""
    )
    return f"""api_version: tools.nebula.security/v1
kind: ToolPack
metadata:
  publisher: {publisher}
  name: sample-pack
  version: 1.2.3
  minimum_nebula_version: 3.0.0a1
  description: A bounded sample pack.
  licenses: [BSD-3-Clause]
images:
  - name: sample
    platform: linux/amd64
    image: example.invalid/tools/sample@sha256:{DIGEST_A}
    sbom: sbom-amd64.json
    provenance: provenance-amd64.json
{arm}permissions:
  network: false
  workspace: none
  credentials: []
tools:
  - name: sample.query
    description: Query a local sample index.
    image: sample
    executable: /usr/bin/sample
    fixed_arguments: [--json]
    argument_bindings:
      - argument: query
        kind: value
        flag: --query
    input_schema:
      type: object
      properties:
        query: {{type: string}}
      required: [query]
      additionalProperties: false
    output_schema:
      type: object
      properties:
        result: {{type: string}}
      required: [result]
      additionalProperties: false
    policy:
      risk_class: local_read
      network_access: false
      filesystem_access: none
      requires_approval: false
      idempotency: safe
    parser:
      built_in: json/v1
    smoke_tests:
      - arguments: {{query: smoke}}
        expected_exit_code: 0
        timeout_seconds: 5
"""


def signing_material():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, Ed25519Keyring(
        {
            "test-key": {
                "public_key": public,
                "publishers": ["example", "berylliumsec"],
            }
        }
    )


def envelope(private, payload):
    return SignatureEnvelope(
        key_id="test-key",
        signature=base64.b64encode(private.sign(payload)).decode("ascii"),
    )


def test_manifest_compilation_is_canonical_and_builds_declarative_specs():
    manifest = compile_manifest_yaml(manifest_source())
    same = compile_manifest_yaml("\n" + manifest_source())

    assert canonical_manifest_json(manifest) == canonical_manifest_json(same)
    assert manifest_digest(manifest) == manifest_digest(same)
    [spec] = manifest.tool_specs("linux/amd64")
    assert spec.pack_id == "example/sample-pack@1.2.3"
    assert spec.image.endswith(DIGEST_A)
    assert build_declared_command(spec, {"query": "OpenSSH 9"}) == [
        "/usr/bin/sample",
        "--json",
        "--query",
        "OpenSSH 9",
    ]
    with pytest.raises(InvalidToolArguments):
        build_declared_command(spec, {"query": ["not", "scalar"]})
    positional = spec.model_copy(
        update={
            "argument_bindings": [
                spec.argument_bindings[0].model_copy(
                    update={"kind": "positional", "flag": None}
                )
            ]
        }
    )
    with pytest.raises(InvalidToolArguments, match="interpreted as an option"):
        build_declared_command(positional, {"query": "--arbitrary-flag"})


def test_catalog_collection_metadata_must_be_complete():
    common = {
        "publisher": "berylliumsec",
        "name": "nebula-toolbox",
        "version": "0.1.0",
        "description": "Network tools",
        "manifest_digest": DIGEST_A,
        "manifest_url": "https://catalog.example/network.json",
        "signature_url": "https://catalog.example/network.signature.json",
    }
    with pytest.raises(ValidationError, match="collection ID and name"):
        ToolCatalogEntry(**common, collection_id="nebula-toolbox")
    entry = ToolCatalogEntry(
        **common,
        collection_id="nebula-toolbox",
        collection_name="Nebula Toolbox",
        tool_names=["environment.run_network"],
        platforms=["linux/amd64", "linux/arm64"],
    )
    assert entry.collection_order == 0


@pytest.mark.parametrize(
    "old,new",
    [
        ("additionalProperties: false", "additionalProperties: true"),
        ("/usr/bin/sample", "/bin/sh"),
        (f"@sha256:{DIGEST_A}", ":latest@sha256:" + DIGEST_A),
        ("minimum_nebula_version: 3.0.0a1", "minimum_nebula_version: 99.0.0"),
    ],
)
def test_manifest_rejects_unsafe_or_incompatible_contracts(old, new):
    source = manifest_source().replace(old, new, 1)
    if "99.0.0" in new:
        manifest = compile_manifest_yaml(source)
        with pytest.raises(ToolPackValidationError, match="requires Nebula"):
            manifest.ensure_core_compatible()
    else:
        with pytest.raises(ToolPackValidationError):
            compile_manifest_yaml(source)


def test_catalog_urls_reject_credentials_queries_and_non_https():
    unsafe = (
        "http://catalog.example/manifest.json",
        "https://user:secret@catalog.example/manifest.json",
        "https://catalog.example/manifest.json?token=secret",
    )
    for url in unsafe:
        with pytest.raises(ValidationError, match="credential-free HTTPS"):
            ToolCatalogEntry(
                publisher="example",
                name="sample",
                version="1.0.0",
                description="sample",
                manifest_digest=DIGEST_A,
                manifest_url=url,
                signature_url="https://catalog.example/sample.sig.json",
            )


def test_yaml_rejects_duplicates_aliases_and_oversized_input():
    with pytest.raises(ToolPackValidationError, match="duplicate YAML key"):
        compile_manifest_yaml("kind: ToolPack\nkind: ToolPack\n")
    with pytest.raises(ToolPackValidationError, match="aliases"):
        compile_manifest_yaml("value: &one x\ncopy: *one\n")
    with pytest.raises(ToolPackValidationError, match="too large"):
        compile_manifest_yaml(b"x" * 2_000_001)


def test_ed25519_keyring_verifies_canonical_payload_and_fails_closed():
    private, keyring = signing_material()
    manifest = compile_manifest_yaml(manifest_source())
    payload = canonical_manifest_json(manifest)
    signature = envelope(private, payload)

    assert keyring.verify(payload, signature) == "test-key"
    with pytest.raises(SignatureVerificationError, match="invalid"):
        keyring.verify(payload + b" ", signature)
    with pytest.raises(SignatureVerificationError, match="unknown"):
        Ed25519Keyring({}).verify(payload, signature)


def test_pack_signing_key_is_bound_to_the_claimed_publisher(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    keyring = Ed25519Keyring(
        {
            "acme.release-1": {
                "public_key": public,
                "publishers": ["acme"],
            }
        }
    )
    manifest = compile_manifest_yaml(manifest_source(publisher="berylliumsec"))
    signature = SignatureEnvelope(
        key_id="acme.release-1",
        signature=base64.b64encode(
            private.sign(canonical_manifest_json(manifest))
        ).decode("ascii"),
    )
    service = ToolPackInstaller(
        store=NebulaStore(Database(tmp_path / "publisher.db")),
        manifests=ImmutableManifestStore(tmp_path / "packs"),
        runtime=FakeRuntime(),
        runtime_profile_id="runner-1",
        platform="linux/amd64",
        verifier=keyring,
    )

    with pytest.raises(SignatureVerificationError, match="not authorized"):
        asyncio.run(service.install(manifest, source="catalog", signature=signature))
    assert service.store.list_entities(ToolPackInstallation) == []


def test_immutable_manifest_store_detects_conflicts_and_tampering(tmp_path):
    manifest = compile_manifest_yaml(manifest_source())
    storage = ImmutableManifestStore(tmp_path / "packs")
    path = storage.put(manifest)
    assert storage.put(manifest) == path
    assert storage.get(manifest_digest(manifest)) == manifest

    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ToolPackInstallError, match="invalid"):
        storage.get(manifest_digest(manifest))
    with pytest.raises(ValueError):
        storage.manifest_path("../escape")


class FakeRuntime:
    def __init__(
        self,
        *,
        smoke_exit=0,
        digest=None,
        smoke_stdout='{"result":"ok"}',
        smoke_stderr="",
    ):
        self.smoke_exit = smoke_exit
        self.digest = digest
        self.smoke_stdout = smoke_stdout
        self.smoke_stderr = smoke_stderr
        self.pulled = []
        self.commands = []

    async def pull(self, image):
        self.pulled.append(image)

    async def inspect(self, image):
        digest = self.digest or image.rsplit("@", 1)[1].removeprefix("sha256:")
        return RuntimeImageInfo(
            image=image,
            digest="sha256:" + digest,
            platform="linux/amd64",
            user="10001:10001",
        )

    async def smoke_test(self, *, image, command, timeout_seconds):
        self.commands.append((image, command, timeout_seconds))
        return RuntimeSmokeResult(
            exit_code=self.smoke_exit,
            stdout=self.smoke_stdout,
            stderr=self.smoke_stderr,
        )


def installer(tmp_path, runtime, *, developer_mode=True, parser_executor=None):
    _, keyring = signing_material()
    return ToolPackInstaller(
        store=NebulaStore(Database(tmp_path / "nebula.db")),
        manifests=ImmutableManifestStore(tmp_path / "packs"),
        runtime=runtime,
        runtime_profile_id="runner-1",
        platform="linux/amd64",
        verifier=keyring,
        parser_executor=parser_executor,
        developer_mode=developer_mode,
    )


def test_unsigned_local_install_is_atomic_verified_and_registry_ready(tmp_path):
    manifest = compile_manifest_yaml(manifest_source(second_platform=False))
    runtime = FakeRuntime()
    service = installer(tmp_path, runtime)
    installed = asyncio.run(
        service.install(
            manifest,
            source="/tmp/sample.ntp",
            signature=None,
            local_file=True,
            confirm_unsigned_permissions=True,
        )
    )

    assert installed.status == ToolPackInstallationStatus.READY
    assert installed.trust == ToolPackTrust.LOCAL_TRUSTED
    assert installed.verified_at is not None
    assert runtime.pulled == [manifest.images[0].image]
    assert runtime.commands[0][1] == [
        "/usr/bin/sample",
        "--json",
        "--query",
        "smoke",
    ]
    registry = build_tool_registry(
        [installed],
        platform="linux/amd64",
        manifests=service.manifests,
    )
    assert [spec.name for spec in registry.specs()] == ["sample.query"]

    disabled = service.disable(installed.id)
    assert disabled.status == ToolPackInstallationStatus.DISABLED
    with pytest.raises(ToolPackInstallError, match="not ready"):
        build_tool_registry(
            [disabled],
            platform="linux/amd64",
            manifests=service.manifests,
        )
    with pytest.raises(ToolPackInstallError, match="cannot be re-enabled"):
        asyncio.run(service.verify(disabled.id))
    assert service.store.get(ToolPackInstallation, disabled.id).status == (
        ToolPackInstallationStatus.DISABLED
    )


def test_signed_verification_rechecks_signature_and_attribution(tmp_path):
    private, keyring = signing_material()
    manifest = compile_manifest_yaml(manifest_source())
    signature = envelope(private, canonical_manifest_json(manifest))
    service = ToolPackInstaller(
        store=NebulaStore(Database(tmp_path / "signed.db")),
        manifests=ImmutableManifestStore(tmp_path / "packs"),
        runtime=FakeRuntime(),
        runtime_profile_id="runner-1",
        platform="linux/amd64",
        verifier=keyring,
    )
    installed = asyncio.run(
        service.install(manifest, source="catalog", signature=signature)
    )
    assert installed.trust == ToolPackTrust.TRUSTED_PUBLISHER
    assert installed.publisher_key_id == "test-key"

    service.manifests.signature_path(installed.manifest_digest).unlink()
    with pytest.raises(SignatureVerificationError, match="not found"):
        asyncio.run(service.verify(installed.id))
    failed = service.store.get(ToolPackInstallation, installed.id)
    assert failed.status == ToolPackInstallationStatus.FAILED

    service.manifests.put(manifest, signature)
    recovered = asyncio.run(service.verify(installed.id))
    assert recovered.status == ToolPackInstallationStatus.READY


def test_installer_rejects_unsigned_remote_curated_and_failed_smoke(tmp_path):
    manifest = compile_manifest_yaml(manifest_source())
    service = installer(tmp_path, FakeRuntime(), developer_mode=False)
    with pytest.raises(SignatureVerificationError, match="unsigned"):
        asyncio.run(
            service.install(
                manifest,
                source="https://example.invalid/pack.json",
                signature=None,
            )
        )

    locally_trusted = asyncio.run(
        service.install(
            manifest,
            source="local.ntp",
            signature=None,
            local_file=True,
        )
    )
    assert locally_trusted.trust == ToolPackTrust.LOCAL_TRUSTED

    failed = installer(
        tmp_path / "failed",
        FakeRuntime(smoke_exit=2, smoke_stderr="invalid smoke request"),
    )
    with pytest.raises(
        ToolPackInstallError,
        match="smoke test failed.*exit 2: invalid smoke request",
    ):
        asyncio.run(
            failed.install(
                manifest,
                source="local.ntp",
                signature=None,
                local_file=True,
                confirm_unsigned_permissions=True,
            )
        )
    [record] = failed.store.list_entities(ToolPackInstallation)
    assert record.status == ToolPackInstallationStatus.FAILED


def test_curated_signed_pack_requires_both_platforms(tmp_path):
    private, keyring = signing_material()
    manifest = compile_manifest_yaml(
        manifest_source(publisher="berylliumsec", second_platform=False)
    )
    service = ToolPackInstaller(
        store=NebulaStore(Database(tmp_path / "nebula.db")),
        manifests=ImmutableManifestStore(tmp_path / "packs"),
        runtime=FakeRuntime(),
        runtime_profile_id="runner-1",
        platform="linux/amd64",
        verifier=keyring,
    )
    with pytest.raises(ToolPackValidationError, match="amd64 and arm64"):
        asyncio.run(
            service.install(
                manifest,
                source="catalog",
                signature=envelope(private, canonical_manifest_json(manifest)),
            )
        )


def test_signed_catalog_fetches_and_uses_only_a_verified_cache(tmp_path):
    private, keyring = signing_material()
    catalog = ToolCatalogV1(
        generated_at=utc_now(),
        entries=[
            ToolCatalogEntry(
                publisher="example",
                name="sample-pack",
                version="1.2.3",
                description="Sample",
                manifest_digest="c" * 64,
                manifest_url="https://catalog.example/sample.json",
                signature_url="https://catalog.example/sample.sig.json",
            )
        ],
    )
    signature = envelope(private, canonical_catalog_json(catalog))

    async def handler(request):
        if request.url.path.endswith("signature.json"):
            return httpx.Response(200, json=signature.model_dump(mode="json"))
        return httpx.Response(200, json=catalog.model_dump(mode="json"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = ToolCatalogClient(
        catalog_url="https://catalog.example/catalog.json",
        signature_url="https://catalog.example/signature.json",
        verifier=keyring,
        cache_path=tmp_path / "catalog-cache.json",
        client=client,
    )
    result = asyncio.run(service.fetch())
    assert isinstance(result, CatalogLoadResult)
    assert result.from_cache is False
    assert result.catalog == catalog
    asyncio.run(client.aclose())

    offline = ToolCatalogClient(
        catalog_url="https://catalog.example/catalog.json",
        signature_url="https://catalog.example/signature.json",
        verifier=keyring,
        cache_path=tmp_path / "catalog-cache.json",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        ),
    )
    cached = asyncio.run(offline.fetch())
    assert cached.from_cache is True


def test_catalog_stream_is_bounded_and_verified_cache_prevents_rollback(tmp_path):
    private, keyring = signing_material()
    now = utc_now()

    def make_catalog(generated_at, description):
        return ToolCatalogV1(
            generated_at=generated_at,
            entries=[
                ToolCatalogEntry(
                    publisher="example",
                    name="sample-pack",
                    version="1.2.3",
                    description=description,
                    manifest_digest="c" * 64,
                    manifest_url="https://catalog.example/sample.json",
                    signature_url="https://catalog.example/sample.sig.json",
                )
            ],
        )

    def transport_for(document, signature):
        def handler(request):
            payload = (
                signature.model_dump(mode="json")
                if request.url.path.endswith("signature.json")
                else document.model_dump(mode="json")
            )
            return httpx.Response(200, json=payload)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    current = make_catalog(now, "current")
    current_signature = envelope(private, canonical_catalog_json(current))
    cache = tmp_path / "rollback-cache.json"
    first = ToolCatalogClient(
        catalog_url="https://catalog.example/catalog.json",
        signature_url="https://catalog.example/signature.json",
        verifier=keyring,
        cache_path=cache,
        client=transport_for(current, current_signature),
    )
    assert asyncio.run(first.fetch()).from_cache is False

    stale = make_catalog(now - timedelta(days=1), "stale")
    stale_signature = envelope(private, canonical_catalog_json(stale))
    rollback = ToolCatalogClient(
        catalog_url="https://catalog.example/catalog.json",
        signature_url="https://catalog.example/signature.json",
        verifier=keyring,
        cache_path=cache,
        client=transport_for(stale, stale_signature),
    )
    loaded = asyncio.run(rollback.fetch())
    assert loaded.from_cache is True
    assert loaded.catalog == current

    oversized = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-length": "1000"}, content=b"x"
            )
        )
    )
    with pytest.raises(ToolPackValidationError, match="size limit"):
        asyncio.run(fetch_bounded_https(oversized, "https://catalog.example/x", 10))


def test_persisted_tool_entities_and_scope_urls_are_strict():
    profile = RunnerProfile(
        name="Podman",
        runtime=RunnerRuntime.PODMAN,
        executable="/opt/podman/bin/podman",
        platform="linux/amd64",
        isolation=RunnerIsolation.ROOTLESS,
        egress_helper_image=f"example.invalid/nebula/egress@sha256:{DIGEST_A}",
        seccomp_profile="/etc/nebula/seccomp.json",
    )
    assert profile.executable.startswith("/")
    assignment = EngagementToolAssignment(
        engagement_id="eng-1",
        manifest_digest=DIGEST_A,
        allowed_tool_names=["sample.query", "sample.query"],
        assigned_by="operator-1",
    )
    assert assignment.allowed_tool_names == ["sample.query"]
    run = AgentRun(
        engagement_id="eng-1",
        objective="test",
        tool_pack_digests=[DIGEST_A, DIGEST_A],
        tool_interface_catalog_digests=[DIGEST_B, DIGEST_B],
    )
    assert run.tool_pack_digests == [DIGEST_A]
    assert run.tool_interface_catalog_digests == [DIGEST_B]
    scope = ScopePolicy(
        engagement_id="eng-1",
        allowed_urls=["HTTPS://Example.COM"],
    )
    assert scope.allowed_urls == ["https://example.com/"]
    with pytest.raises(ValidationError):
        ScopePolicy(
            engagement_id="eng-1",
            allowed_urls=["https://user:pass@example.com/#fragment"],
        )
    with pytest.raises(ValidationError):
        RunnerProfile(
            name="bad",
            runtime=RunnerRuntime.DOCKER,
            executable="docker",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
        )
    with pytest.raises(ValidationError, match="runtime must match"):
        RunnerProfile(
            name="mismatch",
            runtime=RunnerRuntime.PODMAN,
            executable="/usr/bin/docker",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
        )


def test_registry_can_wire_an_isolated_parser_container(tmp_path):
    manifest = compile_manifest_yaml(manifest_source(second_platform=False))
    payload = manifest.model_dump(mode="python")
    payload["tools"][0]["parser"] = {
        "container": {
            "image": f"example.invalid/parser/sample@sha256:{DIGEST_B}",
            "executable": "/parser/bin/parse",
            "output_schema": payload["tools"][0]["output_schema"],
        }
    }
    manifest = ToolPackManifestV1.model_validate(payload)

    class ParserExecutor:
        async def parse(self, contract, raw_output):
            return {"result": "parsed"}

    parser_executor = ParserExecutor()
    service = installer(tmp_path, FakeRuntime(), parser_executor=parser_executor)
    installed = asyncio.run(
        service.install(
            manifest,
            source="local.ntp",
            signature=None,
            local_file=True,
            confirm_unsigned_permissions=True,
        )
    )

    registry = build_tool_registry(
        [installed],
        platform="linux/amd64",
        manifests=service.manifests,
        parser_executor=parser_executor,
    )
    assert isinstance(registry.get("sample.query"), ParserContainerCommandTool)
    assert set(service.runtime.pulled) == {
        f"example.invalid/tools/sample@sha256:{DIGEST_A}",
        f"example.invalid/parser/sample@sha256:{DIGEST_B}",
    }


def test_parser_container_pack_cannot_be_ready_without_parser_verification(tmp_path):
    manifest = compile_manifest_yaml(manifest_source(second_platform=False))
    payload = manifest.model_dump(mode="python")
    payload["tools"][0]["parser"] = {
        "container": {
            "image": f"example.invalid/parser/sample@sha256:{DIGEST_B}",
            "executable": "/parser/bin/parse",
            "output_schema": payload["tools"][0]["output_schema"],
        }
    }
    manifest = ToolPackManifestV1.model_validate(payload)
    service = installer(tmp_path, FakeRuntime())
    with pytest.raises(ToolPackInstallError, match="not configured"):
        asyncio.run(
            service.install(
                manifest,
                source="local.ntp",
                signature=None,
                local_file=True,
                confirm_unsigned_permissions=True,
            )
        )
    [failed] = service.store.list_entities(ToolPackInstallation)
    assert failed.status == ToolPackInstallationStatus.FAILED


def test_runtime_image_uid_zero_is_rejected_even_with_non_root_group():
    with pytest.raises(ValidationError, match="non-root"):
        RuntimeImageInfo(
            image=f"example.invalid/tool/sample@sha256:{DIGEST_A}",
            digest=f"sha256:{DIGEST_A}",
            platform="linux/amd64",
            user="0:10001",
        )


def test_tool_platform_uses_fixed_pack_root_with_test_override(tmp_path, monkeypatch):
    platform = ToolPlatform(
        store=NebulaStore(Database(tmp_path / "platform.db")),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "fixed-pack-root",
    )
    assert platform.manifests.root == (tmp_path / "fixed-pack-root").resolve()
    assert platform.catalog_client.cache_path.parent == platform.manifests.root

    monkeypatch.setattr("nebula.v3.toolpacks.sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_tool_pack_root() == (tmp_path / "xdg/io.nebula.security/tool-packs")

    monkeypatch.setattr("nebula.v3.toolpacks.sys.platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert default_tool_pack_root() == (
        tmp_path / "home/Library/Application Support/io.nebula.security/tool-packs"
    )


def test_chat_components_ignore_stale_assignment_when_ready_digest_exists(
    tmp_path, monkeypatch
):
    store = NebulaStore(Database(tmp_path / "chat-platform.db"))
    engagement = store.create(Engagement(name="Chat tools"))
    scope = store.create(ScopePolicy(engagement_id=engagement.id))
    engagement = store.update(
        Engagement,
        engagement.id,
        {"scope_policy_id": scope.id},
        expected_revision=engagement.revision,
    )
    store.create(
        RunnerProfile(
            id="local",
            name="Local",
            runtime=RunnerRuntime.DOCKER,
            executable="/usr/bin/docker",
            context="rootless",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            healthy=True,
        )
    )
    store.create(
        ToolPackInstallation(
            publisher="example",
            name="ready-pack",
            version="1.0.0",
            manifest_digest=DIGEST_B,
            source="test",
            trust=ToolPackTrust.LOCAL_UNSIGNED,
            runtime_profile_id="local",
            status=ToolPackInstallationStatus.READY,
            manifest_path=str(tmp_path / "ready-pack.json"),
            installed_at=utc_now(),
            verified_at=utc_now(),
        )
    )
    for digest in (DIGEST_A, DIGEST_B):
        store.create(
            EngagementToolAssignment(
                engagement_id=engagement.id,
                manifest_digest=digest,
                allowed_tool_names=["sample.query"],
                assigned_by="operator",
            )
        )

    async def query(_arguments):
        return {"result": "ok"}

    registry = ToolRegistry()
    registry.register(
        AnalysisTool(
            ToolSpec(
                name="sample.query",
                description="Query the ready pack",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                output_schema={"type": "object", "additionalProperties": True},
                risk_class=RiskClass.LOCAL_READ,
            ),
            query,
        )
    )
    monkeypatch.setattr(
        "nebula.v3.tool_platform.build_tool_registry",
        lambda *_args, **_kwargs: registry,
    )
    platform = ToolPlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "packs",
        execution_enabled=True,
    )

    components = platform.chat_components(
        engagement_id=engagement.id,
        turn_id="turn-1",
        provider=object(),
        model="model-1",
    )

    assert components.tool_pack_digests == (DIGEST_B,)
    assert set(components.specs) == {"sample.query"}


def test_human_terminal_runner_prefers_local_and_rejects_ambiguity(tmp_path):
    store = NebulaStore(Database(tmp_path / "human-runner.db"))
    engagement = store.create(Engagement(name="Kali lab"))
    platform = ToolPlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "packs",
        execution_enabled=True,
    )

    def profile(profile_id):
        return store.create(
            RunnerProfile(
                id=profile_id,
                name=profile_id,
                runtime=RunnerRuntime.PODMAN,
                executable="/usr/bin/podman",
                platform="linux/amd64",
                isolation=RunnerIsolation.ROOTLESS,
                healthy=True,
            )
        )

    other = profile("other")
    local = profile("local")
    assert platform.resolve_human_terminal_profile(engagement.id).id == local.id

    store.update(
        RunnerProfile,
        local.id,
        {"enabled": False},
        expected_revision=local.revision,
    )
    assert platform.resolve_human_terminal_profile(engagement.id).id == other.id
    profile("third")
    with pytest.raises(ToolPlatformError, match="ambiguous"):
        platform.resolve_human_terminal_profile(engagement.id)


def test_terminal_startup_cleanup_never_executes_unhealthy_or_unverified_profiles(
    tmp_path, monkeypatch, caplog
):
    runtime = tmp_path / "bin" / "docker"
    runtime.parent.mkdir()
    runtime.write_text("test runtime", encoding="utf-8")
    runtime.chmod(0o755)
    store = NebulaStore(Database(tmp_path / "unsafe-cleanup.db"))
    for profile_id, healthy, checked_at, detail in (
        (
            "persisted-remote",
            False,
            utc_now(),
            "Docker context must use a local absolute Unix socket",
        ),
        (
            "persisted-rootful",
            False,
            utc_now(),
            "Docker daemon is not operating in rootless mode",
        ),
        ("persisted-unverified", True, None, None),
        ("persisted-untrusted-path", True, utc_now(), "previously healthy"),
    ):
        store.create(
            RunnerProfile(
                id=profile_id,
                name=profile_id,
                runtime=RunnerRuntime.DOCKER,
                executable=str(runtime),
                context=profile_id,
                platform="linux/amd64",
                isolation=RunnerIsolation.ROOTLESS,
                enabled=True,
                healthy=healthy,
                last_health_at=checked_at,
                last_health_detail=detail,
            )
        )
    platform = ToolPlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "packs",
        execution_enabled=True,
    )
    runtime_commands: list[tuple[str, ...]] = []

    async def capture(_runner, *arguments):
        runtime_commands.append(arguments)
        return "", "", 1

    monkeypatch.setattr(ContainerSandboxRunner, "_capture", capture)
    caplog.set_level(logging.WARNING, logger="nebula.v3.tool_platform")

    asyncio.run(platform.cleanup_operator_terminals())

    assert runtime_commands == []
    for profile_id in (
        "persisted-remote",
        "persisted-rootful",
        "persisted-unverified",
        "persisted-untrusted-path",
    ):
        assert profile_id in caplog.text
    assert caplog.text.count("profile is not verified healthy") == 3
    assert "outside the fixed-path allowlist" in caplog.text


def test_terminal_startup_cleanup_revalidates_live_remote_and_rootful_profiles(
    tmp_path, monkeypatch, caplog
):
    runtime = tmp_path / "bin" / "docker"
    runtime.parent.mkdir()
    runtime.write_text("test runtime", encoding="utf-8")
    runtime.chmod(0o755)
    monkeypatch.setattr(ContainerSandboxRunner, "_trusted_runtime_paths", (runtime,))
    store = NebulaStore(Database(tmp_path / "revalidate-cleanup.db"))
    checked_at = utc_now()
    for profile_id in ("remote-now", "rootful-now"):
        store.create(
            RunnerProfile(
                id=profile_id,
                name=profile_id,
                runtime=RunnerRuntime.DOCKER,
                executable=str(runtime),
                context=profile_id,
                platform="linux/amd64",
                isolation=RunnerIsolation.ROOTLESS,
                enabled=True,
                healthy=True,
                last_health_at=checked_at,
                last_health_detail="previously verified rootless",
            )
        )
    platform = ToolPlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts-live"),
        data_root=tmp_path / "core-data-live",
        tool_pack_root=tmp_path / "packs-live",
        execution_enabled=True,
    )
    runtime_commands: list[tuple[str, tuple[str, ...]]] = []
    removals: list[str] = []

    async def capture(runner, *arguments):
        assert runner.profile is not None
        context = runner.profile.context or ""
        runtime_commands.append((context, arguments))
        if arguments[:2] == ("context", "inspect"):
            endpoint = (
                "tcp://runner.example:2376"
                if context == "remote-now"
                else "unix:///run/user/1000/docker.sock"
            )
            return json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}]), "", 0
        if arguments == ("info", "--format", "{{json .}}"):
            return (
                json.dumps({"OSType": "linux", "SecurityOptions": ["name=seccomp"]}),
                "",
                0,
            )
        if arguments[0] == "ps":
            return "nebula-terminal-must-not-remove", "", 0
        raise AssertionError(f"unexpected runtime arguments: {arguments!r}")

    async def remove(_runner, name):
        removals.append(name)

    monkeypatch.setattr(ContainerSandboxRunner, "_capture", capture)
    monkeypatch.setattr(ContainerSandboxRunner, "_force_remove", remove)
    caplog.set_level(logging.WARNING, logger="nebula.v3.sandbox")

    asyncio.run(platform.cleanup_operator_terminals())

    assert removals == []
    assert all(arguments[0] != "ps" for _context, arguments in runtime_commands)
    assert ("remote-now", ("context", "inspect", "remote-now")) in runtime_commands
    assert ("rootful-now", ("info", "--format", "{{json .}}")) in runtime_commands
    assert "local absolute Unix socket" in caplog.text
    assert "not operating in rootless mode" in caplog.text


def test_human_terminal_image_is_prepared_once_per_runner_revision(
    tmp_path, monkeypatch
):
    store = NebulaStore(Database(tmp_path / "human-image.db"))
    engagement = store.create(Engagement(name="Kali lab"))
    store.create(
        RunnerProfile(
            id="local",
            name="Local",
            runtime=RunnerRuntime.PODMAN,
            executable="/usr/bin/podman",
            platform="linux/amd64",
            isolation=RunnerIsolation.ROOTLESS,
            healthy=True,
        )
    )
    platform = ToolPlatform(
        store=store,
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "packs",
        execution_enabled=True,
    )
    calls = 0

    class FakePreparer:
        def __init__(self, **kwargs):
            assert kwargs["source_reference"] == (
                "docker.io/kalilinux/kali-rolling:latest"
            )
            assert kwargs["expected_repository"] == ("docker.io/kalilinux/kali-rolling")

        async def prepare(self):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return PreparedContainerImage(
                source_reference="docker.io/kalilinux/kali-rolling:latest",
                base_resolved_reference=(
                    "docker.io/kalilinux/kali-rolling@sha256:" + DIGEST_A
                ),
                base_digest="sha256:" + DIGEST_A,
                resolved_reference="sha256:" + DIGEST_B,
                digest="sha256:" + DIGEST_B,
                platform="linux/amd64",
                configured_user="",
                installed_packages=("kali-linux-headless", "iputils-ping"),
                refreshed=True,
                detail="pulled",
                security_tools=("hashcat", "nmap"),
                security_tool_packages=("hashcat", "nmap"),
                security_tool_provenance=(
                    ("hashcat", ("hashcat",)),
                    ("nmap", ("nmap",)),
                ),
                security_tool_manifest_sha256=DIGEST_C,
            )

    monkeypatch.setattr("nebula.v3.tool_platform.ContainerImagePreparer", FakePreparer)

    async def resolve_twice():
        return await asyncio.gather(
            platform.resolve_human_terminal_runtime(engagement.id),
            platform.resolve_human_terminal_runtime(engagement.id),
        )

    first, second = asyncio.run(resolve_twice())
    assert calls == 1
    assert first.image == second.image
    assert first.image.base_resolved_reference.endswith(DIGEST_A)
    assert first.image.resolved_reference.endswith(DIGEST_B)
    metadata = json.loads(
        platform.human_terminal_image_metadata_path.read_text(encoding="utf-8")
    )
    assert metadata == {
        "schema": "nebula.human-terminal-image/v2",
        "verified_at": metadata["verified_at"],
        "runner_profile_id": "local",
        "runner_profile_revision": 1,
        "source_reference": "docker.io/kalilinux/kali-rolling:latest",
        "source_is_digest_pinned": False,
        "base_resolved_reference": (
            "docker.io/kalilinux/kali-rolling@sha256:" + DIGEST_A
        ),
        "base_digest": "sha256:" + DIGEST_A,
        "resolved_reference": "sha256:" + DIGEST_B,
        "image_digest": "sha256:" + DIGEST_B,
        "platform": "linux/amd64",
        "installed_packages": ["kali-linux-headless", "iputils-ping"],
        "security_tools": ["hashcat", "nmap"],
        "security_tool_packages": ["hashcat", "nmap"],
        "security_tool_provenance": {
            "hashcat": ["hashcat"],
            "nmap": ["nmap"],
        },
        "security_tool_manifest_sha256": DIGEST_C,
        "registry_refreshed": True,
    }
    assert platform.last_human_terminal_security_inventory() == (
        "sha256:" + DIGEST_B,
        DIGEST_C,
        ("hashcat", "nmap"),
    )
    assert platform.human_terminal_image_metadata_path.stat().st_mode & 0o777 == 0o600


def test_tool_platform_includes_declared_shell_capabilities_implicitly(tmp_path):
    manifest = compile_manifest_yaml(manifest_source(second_platform=False))
    manifest = manifest.model_copy(
        update={
            "tools": [
                manifest.tools[0],
                manifest.tools[0].model_copy(
                    update={"name": "environment.shell_local"}
                ),
                manifest.tools[0].model_copy(
                    update={"name": "environment.shell_network"}
                ),
            ]
        }
    )
    digest = manifest_digest(manifest)
    platform = ToolPlatform(
        store=NebulaStore(Database(tmp_path / "implicit-shell.db")),
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        data_root=tmp_path / "core-data",
        tool_pack_root=tmp_path / "packs",
    )
    platform.manifests.put(manifest)

    assert platform.normalize_assignment(digest, ["sample.query"]) == [
        "environment.shell_local",
        "environment.shell_network",
        "sample.query",
    ]

    legacy_assignment = EngagementToolAssignment(
        engagement_id="engagement-1",
        manifest_digest=digest,
        allowed_tool_names=["sample.query"],
        enabled=True,
        assigned_by="operator-1",
    )
    assert platform._assignment_allows_operator_shell(
        legacy_assignment, "environment.shell_local"
    )
    assert platform._assignment_allows_operator_shell(
        legacy_assignment, "environment.shell_network"
    )


def test_default_tool_platform_enables_brokered_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nebula.v3.tool_platform.default_tool_pack_root",
        lambda: tmp_path / "packs",
    )
    platform = default_tool_platform(
        store=NebulaStore(tmp_path / "public.db"),
        artifact_store=ArtifactStore(tmp_path / "public-artifacts"),
        data_root=tmp_path / "public-core",
    )
    assert platform.execution_enabled is True
    assert platform.has_trusted_keys is True


def test_default_tool_platform_accepts_only_official_digest_image_override(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "nebula.v3.tool_platform.default_tool_pack_root",
        lambda: tmp_path / "packs",
    )
    pinned = "docker.io/kalilinux/kali-rolling@sha256:" + DIGEST_A
    monkeypatch.setenv("NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE", pinned)
    platform = default_tool_platform(
        store=NebulaStore(tmp_path / "pinned.db"),
        artifact_store=ArtifactStore(tmp_path / "pinned-artifacts"),
        data_root=tmp_path / "pinned-core",
    )
    assert platform.human_terminal_source_image == pinned

    monkeypatch.setenv(
        "NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE",
        "attacker.invalid/workstation@sha256:" + DIGEST_A,
    )
    with pytest.raises(ToolPlatformError, match="digest-pinned official"):
        default_tool_platform(
            store=NebulaStore(tmp_path / "rejected.db"),
            artifact_store=ArtifactStore(tmp_path / "rejected-artifacts"),
            data_root=tmp_path / "rejected-core",
        )
