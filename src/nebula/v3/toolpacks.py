"""Signed, immutable, per-user tool-pack contracts and installation service."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import yaml
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from yaml.tokens import AliasToken

from .domain import (
    RiskClass,
    ToolPackInstallation,
    ToolPackInstallationStatus,
    ToolPackTrust,
    utc_now,
)
from .storage import NebulaStore
from .toolparsers import (
    BUILTIN_PARSERS,
    ParserContainerContract,
    ParserContainerExecutor,
)
from .tools import (
    IdempotencyBehavior,
    OutputParser,
    SandboxCommandTool,
    ToolArgumentBinding,
    ToolRegistry,
    ToolSpec,
    ToolExecutionResult,
    ToolInvocation,
    ToolPlugin,
    _is_digest_pinned_image,
    build_declared_command,
)
from .sandbox import SandboxRunner
from .version import __version__


TOOL_PACK_API_VERSION = "tools.nebula.security/v1"
MAX_MANIFEST_BYTES = 2_000_000
MAX_STORED_MANIFEST_BYTES = 5_000_000


class ToolPackError(RuntimeError):
    pass


class ToolPackValidationError(ToolPackError):
    pass


class SignatureVerificationError(ToolPackError):
    pass


class ToolPackInstallError(ToolPackError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolPackMetadata(StrictModel):
    publisher: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,127}$")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    version: str = Field(min_length=1, max_length=100)
    minimum_nebula_version: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    licenses: list[str] = Field(min_length=1)
    homepage: str | None = Field(default=None, max_length=2048)

    @field_validator("version", "minimum_nebula_version")
    @classmethod
    def versions_are_valid(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError("tool-pack versions must be valid versions") from exc
        return value

    @field_validator("licenses")
    @classmethod
    def licenses_are_explicit(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("license identifiers cannot be blank")
        return list(dict.fromkeys(values))


class ToolPackImage(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,127}$")
    platform: Literal["linux/amd64", "linux/arm64"]
    image: str
    sbom: str = Field(min_length=1, max_length=2048)
    provenance: str = Field(min_length=1, max_length=2048)

    @field_validator("image")
    @classmethod
    def image_is_digest_pinned(cls, value: str) -> str:
        if not _is_digest_pinned_image(value):
            raise ValueError("OCI image must be pinned by SHA-256 without a tag")
        return value


class ToolPackPermissions(StrictModel):
    network: bool = False
    workspace: Literal["none", "read", "workspace_write"] = "none"
    credentials: list[str] = Field(default_factory=list)


class ToolPolicyContract(StrictModel):
    risk_class: RiskClass
    target_argument: str | None = None
    port_argument: str | None = None
    path_arguments: list[str] = Field(default_factory=list)
    network_access: bool = False
    filesystem_access: Literal["none", "read", "workspace_write"] = "none"
    requires_approval: bool = False
    idempotency: IdempotencyBehavior = IdempotencyBehavior.SAFE

    @model_validator(mode="after")
    def active_tools_require_approval(self) -> "ToolPolicyContract":
        if (
            self.risk_class
            in {
                RiskClass.ACTIVE_SCAN,
                RiskClass.CREDENTIAL_USE,
                RiskClass.EXPLOITATION,
                RiskClass.PERSISTENCE,
                RiskClass.DESTRUCTIVE,
            }
            and not self.requires_approval
        ):
            raise ValueError("active or invasive tools must require approval")
        return self


class ToolParserReference(StrictModel):
    built_in: str | None = None
    container: ParserContainerContract | None = None

    @model_validator(mode="after")
    def exactly_one_parser_is_selected(self) -> "ToolParserReference":
        if (self.built_in is None) == (self.container is None):
            raise ValueError("select exactly one built-in or container parser")
        if self.built_in is not None and self.built_in not in BUILTIN_PARSERS:
            raise ValueError(f"unknown built-in parser: {self.built_in}")
        return self


class ToolSmokeTest(StrictModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    expected_exit_code: int = Field(default=0, ge=0, le=255)
    timeout_seconds: int = Field(default=30, ge=1, le=300)


class ToolPackTool(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(default="1", min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    image: str = Field(pattern=r"^[a-z][a-z0-9._-]{0,127}$")
    executable: str
    fixed_arguments: list[str] = Field(default_factory=list)
    argument_bindings: list[ToolArgumentBinding] = Field(default_factory=list)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    policy: ToolPolicyContract
    parser: ToolParserReference
    smoke_tests: list[ToolSmokeTest] = Field(min_length=1)
    timeout_seconds: int = Field(default=300, ge=1, le=86_400)

    @field_validator("executable")
    @classmethod
    def executable_is_not_a_shell(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("tool executable must be an absolute container path")
        if Path(value).name.lower() in {
            "sh",
            "bash",
            "dash",
            "zsh",
            "fish",
            "cmd",
            "powershell",
            "pwsh",
        }:
            raise ValueError("shell interpreters cannot be tool executables")
        return value

    @field_validator("fixed_arguments")
    @classmethod
    def fixed_argv_has_no_nul(cls, values: list[str]) -> list[str]:
        if any("\x00" in value for value in values):
            raise ValueError("fixed arguments cannot contain NUL bytes")
        return values

    @field_validator("input_schema", "output_schema")
    @classmethod
    def schemas_are_strict_objects(cls, schema: dict[str, Any]) -> dict[str, Any]:
        _validate_strict_schema(schema)
        return schema

    @model_validator(mode="after")
    def mappings_and_policy_are_consistent(self) -> "ToolPackTool":
        properties = self.input_schema.get("properties", {})
        mapped = [
            value
            for value in [
                self.policy.target_argument,
                self.policy.port_argument,
                *self.policy.path_arguments,
            ]
            if value
        ]
        mapped.extend(binding.argument for binding in self.argument_bindings)
        unknown = sorted(set(mapped).difference(properties))
        if unknown:
            raise ValueError(
                f"tool argument mappings are absent from schema: {unknown}"
            )
        if self.policy.network_access and not self.policy.target_argument:
            raise ValueError("network tools require a target_argument")
        if (
            self.policy.target_argument
            and self.policy.target_argument not in self.input_schema.get("required", [])
        ):
            raise ValueError("target_argument must be required")
        for smoke_test in self.smoke_tests:
            errors = list(
                Draft202012Validator(self.input_schema).iter_errors(
                    smoke_test.arguments
                )
            )
            if errors:
                raise ValueError(
                    f"smoke-test arguments violate input schema: {errors[0].message}"
                )
        return self


class ToolPackManifestV1(StrictModel):
    api_version: Literal["tools.nebula.security/v1"] = "tools.nebula.security/v1"
    kind: Literal["ToolPack"] = "ToolPack"
    metadata: ToolPackMetadata
    images: list[ToolPackImage] = Field(min_length=1)
    permissions: ToolPackPermissions
    tools: list[ToolPackTool] = Field(min_length=1)

    @model_validator(mode="after")
    def references_and_platforms_are_complete(self) -> "ToolPackManifestV1":
        image_keys = [(image.name, image.platform) for image in self.images]
        if len(image_keys) != len(set(image_keys)):
            raise ValueError("pack image name/platform pairs must be unique")
        tool_names = [tool.name for tool in self.tools]
        if len(tool_names) != len(set(tool_names)):
            raise ValueError("tool names must be unique within a pack")
        for tool in self.tools:
            platforms = {
                image.platform for image in self.images if image.name == tool.image
            }
            if not platforms:
                raise ValueError(
                    f"tool references unknown image component {tool.image!r}"
                )
            if tool.policy.network_access and not self.permissions.network:
                raise ValueError("network tool exceeds pack network permission")
            workspace_rank = {"none": 0, "read": 1, "workspace_write": 2}
            if (
                workspace_rank[tool.policy.filesystem_access]
                > workspace_rank[self.permissions.workspace]
            ):
                raise ValueError("tool filesystem access exceeds pack permission")
        return self

    def ensure_curated_multiarch(self) -> None:
        for tool in self.tools:
            platforms = {
                image.platform for image in self.images if image.name == tool.image
            }
            if platforms != {"linux/amd64", "linux/arm64"}:
                raise ToolPackValidationError(
                    f"curated image {tool.image!r} requires amd64 and arm64 variants"
                )

    @property
    def identity(self) -> str:
        return f"{self.metadata.publisher}/{self.metadata.name}@{self.metadata.version}"

    def ensure_core_compatible(self, core_version: str = __version__) -> None:
        if Version(core_version) < Version(self.metadata.minimum_nebula_version):
            raise ToolPackValidationError(
                f"{self.identity} requires Nebula "
                f">={self.metadata.minimum_nebula_version}"
            )

    def image_for(self, component: str, platform: str) -> ToolPackImage:
        for image in self.images:
            if image.name == component and image.platform == platform:
                return image
        raise ToolPackValidationError(
            f"pack has no {platform} image for component {component}"
        )

    def tool_specs(self, platform: str) -> list[ToolSpec]:
        digest = manifest_digest(self)
        result: list[ToolSpec] = []
        for tool in self.tools:
            parser_contract = tool.parser.model_dump(mode="json", exclude_none=True)
            result.append(
                ToolSpec(
                    name=tool.name,
                    version=tool.version,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                    risk_class=tool.policy.risk_class,
                    network_access=tool.policy.network_access,
                    filesystem_access=tool.policy.filesystem_access,
                    timeout_seconds=tool.timeout_seconds,
                    parser=tool.parser.built_in,
                    idempotency=tool.policy.idempotency,
                    target_argument=tool.policy.target_argument,
                    port_argument=tool.policy.port_argument,
                    path_arguments=tool.policy.path_arguments,
                    pack_id=self.identity,
                    manifest_digest=digest,
                    image=self.image_for(tool.image, platform).image,
                    executable=tool.executable,
                    fixed_arguments=tool.fixed_arguments,
                    argument_bindings=tool.argument_bindings,
                    parser_contract=parser_contract,
                    smoke_test_fixture=tool.smoke_tests[0].model_dump(mode="json"),
                )
            )
        return result


def _validate_strict_schema(schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
    if schema.get("type") != "object":
        raise ValueError("tool schemas must describe an object")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object" or "properties" in value:
                if value.get("additionalProperties") is not False:
                    raise ValueError(
                        "every object schema must set additionalProperties=false"
                    )
            reference = value.get("$ref")
            if isinstance(reference, str) and not reference.startswith("#/"):
                raise ValueError("tool schemas cannot use external references")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(schema)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ToolPackValidationError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def compile_manifest_yaml(source: str | bytes) -> ToolPackManifestV1:
    raw = source.encode("utf-8") if isinstance(source, str) else source
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ToolPackValidationError("tool-pack manifest is too large")
    try:
        text = raw.decode("utf-8")
        if any(isinstance(token, AliasToken) for token in yaml.scan(text)):
            raise ToolPackValidationError("YAML aliases are not permitted")
        payload = yaml.load(text, Loader=_UniqueKeyLoader)
    except ToolPackValidationError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ToolPackValidationError("tool-pack YAML is invalid") from exc
    if not isinstance(payload, dict):
        raise ToolPackValidationError("tool-pack YAML must contain one object")
    try:
        return ToolPackManifestV1.model_validate(payload)
    except Exception as exc:
        raise ToolPackValidationError("tool-pack manifest failed validation") from exc


def canonical_manifest_json(manifest: ToolPackManifestV1) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def manifest_digest(manifest: ToolPackManifestV1) -> str:
    return hashlib.sha256(canonical_manifest_json(manifest)).hexdigest()


def _validated_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("catalog URLs must be credential-free HTTPS URLs")
    return value


class ToolCatalogEntry(StrictModel):
    publisher: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{0,127}$")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    version: str
    description: str = Field(min_length=1, max_length=2000)
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_url: str
    signature_url: str

    @field_validator("version")
    @classmethod
    def catalog_version_is_valid(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError("catalog versions must be valid versions") from exc
        return value

    @field_validator("manifest_url", "signature_url")
    @classmethod
    def catalog_url_is_https(cls, value: str) -> str:
        return _validated_https_url(value)


class ToolCatalogV1(StrictModel):
    api_version: Literal["tools.nebula.security/catalog/v1"] = (
        "tools.nebula.security/catalog/v1"
    )
    generated_at: datetime
    entries: list[ToolCatalogEntry] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def generated_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog generated_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def entries_are_unique(self) -> "ToolCatalogV1":
        identities = [
            (entry.publisher, entry.name, entry.version) for entry in self.entries
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("catalog entries must have unique identities")
        return self


def canonical_catalog_json(catalog: ToolCatalogV1) -> bytes:
    return json.dumps(
        catalog.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class SignatureEnvelope(StrictModel):
    algorithm: Literal["ed25519"] = "ed25519"
    key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,200}$")
    signature: str

    @field_validator("signature")
    @classmethod
    def signature_is_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("signature must be base64") from exc
        if len(decoded) != 64:
            raise ValueError("Ed25519 signatures must be 64 bytes")
        return value


class SignatureVerifier(Protocol):
    def verify(self, payload: bytes, envelope: SignatureEnvelope) -> str: ...

    def verify_publisher(
        self, payload: bytes, envelope: SignatureEnvelope, publisher: str
    ) -> str: ...


class Ed25519Keyring:
    """A key-id indexed, release-embedded Ed25519 public-key ring."""

    def __init__(
        self,
        keys: Mapping[str, bytes | str | Mapping[str, Any]],
    ) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._publishers: dict[str, frozenset[str]] = {}
        for key_id, configured in keys.items():
            if isinstance(configured, Mapping):
                material = configured.get("public_key")
                publishers = configured.get("publishers")
                if (
                    not isinstance(material, (bytes, str))
                    or not isinstance(publishers, list)
                    or any(not isinstance(value, str) for value in publishers)
                ):
                    raise ValueError(
                        "publisher key entries require public_key and publishers"
                    )
                allowed = frozenset(publishers)
            else:
                material = configured
                # Compact release keyrings may encode ownership in the key ID,
                # e.g. berylliumsec.2026. Explicit mappings are preferred.
                allowed = frozenset({key_id.split(".", 1)[0]})
            if not allowed:
                raise ValueError("publisher key entries require at least one publisher")
            self._keys[key_id] = self._load_key(material)
            self._publishers[key_id] = allowed

    @staticmethod
    def _load_key(material: bytes | str) -> Ed25519PublicKey:
        raw = material.encode("ascii") if isinstance(material, str) else material
        if raw.startswith(b"-----BEGIN"):
            key = serialization.load_pem_public_key(raw)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError("public key is not Ed25519")
            return key
        if len(raw) != 32:
            try:
                raw = base64.b64decode(raw, validate=True)
            except ValueError as exc:
                raise ValueError(
                    "public key must be raw or base64 Ed25519 bytes"
                ) from exc
        if len(raw) != 32:
            raise ValueError("Ed25519 public keys must be 32 bytes")
        return Ed25519PublicKey.from_public_bytes(raw)

    def verify(self, payload: bytes, envelope: SignatureEnvelope) -> str:
        key = self._keys.get(envelope.key_id)
        if key is None:
            raise SignatureVerificationError(
                f"signature uses unknown publisher key: {envelope.key_id}"
            )
        try:
            key.verify(base64.b64decode(envelope.signature), payload)
        except InvalidSignature as exc:
            raise SignatureVerificationError("tool-pack signature is invalid") from exc
        return envelope.key_id

    def verify_publisher(
        self, payload: bytes, envelope: SignatureEnvelope, publisher: str
    ) -> str:
        key_id = self.verify(payload, envelope)
        if publisher not in self._publishers[key_id]:
            raise SignatureVerificationError(
                f"publisher {publisher!r} is not authorized by key {key_id!r}"
            )
        return key_id


class CatalogLoadResult(StrictModel):
    catalog: ToolCatalogV1
    signature: SignatureEnvelope
    from_cache: bool = False


async def fetch_bounded_https(
    client: httpx.AsyncClient, url: str, max_bytes: int
) -> bytes:
    """Stream one HTTPS response and stop before buffering beyond its limit."""

    _validated_https_url(url)
    if max_bytes < 1:
        raise ValueError("download size limit must be positive")
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise ToolPackValidationError(
                    "download has an invalid content-length"
                ) from exc
            if declared < 0 or declared > max_bytes:
                raise ToolPackValidationError("download exceeds its size limit")
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            if len(payload) + len(chunk) > max_bytes:
                raise ToolPackValidationError("download exceeds its size limit")
            payload.extend(chunk)
    return bytes(payload)


class ToolCatalogClient:
    """Fetch a bounded signed catalog over HTTPS with a verified local cache."""

    def __init__(
        self,
        *,
        catalog_url: str,
        signature_url: str,
        verifier: SignatureVerifier,
        cache_path: Path,
        client: httpx.AsyncClient | None = None,
        max_bytes: int = 5_000_000,
        catalog_publisher: str = "berylliumsec",
    ) -> None:
        self.catalog_url = _validated_https_url(catalog_url)
        self.signature_url = _validated_https_url(signature_url)
        self.verifier = verifier
        self.cache_path = cache_path.expanduser()
        self.client = client
        self.catalog_publisher = catalog_publisher
        if not 1 <= max_bytes <= 20_000_000:
            raise ValueError("catalog size limit must be between 1 and 20000000")
        self.max_bytes = max_bytes

    async def fetch(self, *, allow_verified_cache: bool = True) -> CatalogLoadResult:
        try:
            catalog_bytes, signature_bytes = await self._fetch_remote()
            catalog = self._parse_catalog(catalog_bytes)
            signature = self._parse_signature(signature_bytes)
            self.verifier.verify_publisher(
                canonical_catalog_json(catalog),
                signature,
                self.catalog_publisher,
            )
            try:
                cached = self._read_cache()
            except ToolPackValidationError:
                cached = None
            if cached is not None:
                if catalog.generated_at < cached.catalog.generated_at:
                    raise ToolPackValidationError(
                        "signed catalog is older than the verified cache"
                    )
                if (
                    catalog.generated_at == cached.catalog.generated_at
                    and canonical_catalog_json(catalog)
                    != canonical_catalog_json(cached.catalog)
                ):
                    raise ToolPackValidationError(
                        "signed catalog changed without advancing generated_at"
                    )
            self._write_cache(catalog, signature)
            return CatalogLoadResult(catalog=catalog, signature=signature)
        except Exception:
            if not allow_verified_cache:
                raise
            return self._read_cache()

    async def _fetch_remote(self) -> tuple[bytes, bytes]:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0), follow_redirects=False
        )
        try:
            catalog_bytes = await fetch_bounded_https(
                client, self.catalog_url, self.max_bytes
            )
            signature_bytes = await fetch_bounded_https(
                client, self.signature_url, 100_000
            )
        finally:
            if owns_client:
                await client.aclose()
        return catalog_bytes, signature_bytes

    @staticmethod
    def _parse_catalog(payload: bytes) -> ToolCatalogV1:
        try:
            return ToolCatalogV1.model_validate_json(payload)
        except Exception as exc:
            raise ToolPackValidationError("catalog response is invalid") from exc

    @staticmethod
    def _parse_signature(payload: bytes) -> SignatureEnvelope:
        try:
            return SignatureEnvelope.model_validate_json(payload)
        except Exception as exc:
            raise SignatureVerificationError("catalog signature is invalid") from exc

    def _write_cache(
        self, catalog: ToolCatalogV1, signature: SignatureEnvelope
    ) -> None:
        payload = json.dumps(
            {
                "catalog": catalog.model_dump(mode="json"),
                "signature": signature.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.cache_path.name}.",
            suffix=".tmp",
            dir=self.cache_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.cache_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read_cache(self) -> CatalogLoadResult:
        try:
            if self.cache_path.stat().st_size > self.max_bytes + 200_000:
                raise ToolPackValidationError("verified catalog cache is too large")
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            catalog = ToolCatalogV1.model_validate(payload["catalog"])
            signature = SignatureEnvelope.model_validate(payload["signature"])
            self.verifier.verify_publisher(
                canonical_catalog_json(catalog),
                signature,
                self.catalog_publisher,
            )
        except Exception as exc:
            raise ToolPackValidationError(
                "catalog fetch failed and no valid signed cache is available"
            ) from exc
        return CatalogLoadResult(catalog=catalog, signature=signature, from_cache=True)


def default_tool_pack_root() -> Path:
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "io.nebula.security"
            / "tool-packs"
        )
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local/share"
    return base / "io.nebula.security" / "tool-packs"


class ImmutableManifestStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_tool_pack_root()).expanduser()

    def manifest_path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("manifest digest must be lowercase SHA-256")
        return self.root / "manifests" / "sha256" / f"{digest}.json"

    def signature_path(self, digest: str) -> Path:
        return self.manifest_path(digest).with_suffix(".signature.json")

    def put(
        self,
        manifest: ToolPackManifestV1,
        signature: SignatureEnvelope | None = None,
    ) -> Path:
        payload = canonical_manifest_json(manifest)
        if len(payload) > MAX_STORED_MANIFEST_BYTES:
            raise ToolPackInstallError("canonical manifest exceeds its size limit")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.manifest_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._put_immutable(path, payload + b"\n")
        if signature is not None:
            signature_payload = json.dumps(
                signature.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self._put_immutable(self.signature_path(digest), signature_payload + b"\n")
        return path

    @staticmethod
    def _put_immutable(path: Path, payload: bytes) -> None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != payload:
                raise ToolPackInstallError(
                    f"immutable tool-pack content conflicts at {path}"
                )
            return
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def get(self, digest: str) -> ToolPackManifestV1:
        path = self.manifest_path(digest)
        try:
            if path.stat().st_size > MAX_STORED_MANIFEST_BYTES:
                raise ToolPackInstallError("stored manifest exceeds its size limit")
            payload = json.loads(path.read_text(encoding="utf-8"))
            manifest = ToolPackManifestV1.model_validate(payload)
        except FileNotFoundError as exc:
            raise ToolPackInstallError(f"stored manifest not found: {digest}") from exc
        except Exception as exc:
            raise ToolPackInstallError(f"stored manifest is invalid: {digest}") from exc
        if manifest_digest(manifest) != digest:
            raise ToolPackInstallError(f"stored manifest digest mismatch: {digest}")
        return manifest

    def get_signature(self, digest: str) -> SignatureEnvelope:
        path = self.signature_path(digest)
        try:
            if path.stat().st_size > 100_000:
                raise SignatureVerificationError(
                    f"stored manifest signature is too large: {digest}"
                )
            return SignatureEnvelope.model_validate_json(path.read_bytes())
        except FileNotFoundError as exc:
            raise SignatureVerificationError(
                f"stored manifest signature not found: {digest}"
            ) from exc
        except Exception as exc:
            raise SignatureVerificationError(
                f"stored manifest signature is invalid: {digest}"
            ) from exc


class ParserContainerCommandTool(SandboxCommandTool):
    """Execute a tool, then parse its raw stdout in a second isolated container."""

    def __init__(
        self,
        spec: ToolSpec,
        *,
        parser_contract: ParserContainerContract,
        parser_executor: ParserContainerExecutor,
    ) -> None:
        super().__init__(spec, output_parser=lambda stdout, stderr, exit_code: {})
        self.parser_container_contract = parser_contract
        self.parser_executor = parser_executor

    async def execute(
        self, invocation: ToolInvocation, runner: SandboxRunner
    ) -> ToolExecutionResult:
        result = await super().execute(invocation, runner)
        try:
            result.output = await self.parser_executor.parse(
                self.parser_container_contract, result.stdout.encode("utf-8")
            )
        except Exception as exc:
            result.output = {}
            detail = " ".join(str(exc).split()) or exc.__class__.__name__
            result.parser_error = f"{exc.__class__.__name__}: {detail}"[:1_000]
        return result


def build_tool_registry(
    installations: list[ToolPackInstallation],
    *,
    platform: Literal["linux/amd64", "linux/arm64"],
    manifests: ImmutableManifestStore,
    parser_registry: Mapping[str, OutputParser] = BUILTIN_PARSERS,
    parser_executor: ParserContainerExecutor | None = None,
) -> ToolRegistry:
    """Build executable plugins only from ready, digest-consistent installations."""

    registry = ToolRegistry()
    for installation in installations:
        if installation.status != ToolPackInstallationStatus.READY:
            raise ToolPackInstallError(
                f"tool pack is not ready: {installation.publisher}/{installation.name}"
            )
        manifest = manifests.get(installation.manifest_digest)
        if manifest_digest(manifest) != installation.manifest_digest:
            raise ToolPackInstallError(
                "installed manifest digest does not match storage"
            )
        expected_locks = {
            tool.image: manifest.image_for(tool.image, platform).image
            for tool in manifest.tools
        }
        expected_locks.update(
            {
                f"parser:{tool.name}": tool.parser.container.image
                for tool in manifest.tools
                if tool.parser.container is not None
            }
        )
        if installation.image_locks != expected_locks:
            raise ToolPackInstallError("installed image locks do not match manifest")
        for tool, spec in zip(
            manifest.tools, manifest.tool_specs(platform), strict=True
        ):
            try:
                plugin: ToolPlugin
                if tool.parser.container is not None:
                    if parser_executor is None:
                        raise ToolPackInstallError(
                            "parser-container execution is not configured for "
                            f"{tool.name}"
                        )
                    plugin = ParserContainerCommandTool(
                        spec,
                        parser_contract=tool.parser.container,
                        parser_executor=parser_executor,
                    )
                else:
                    assert tool.parser.built_in is not None
                    try:
                        parser = parser_registry[tool.parser.built_in]
                    except KeyError as exc:
                        raise ToolPackInstallError(
                            f"parser is unavailable: {tool.parser.built_in}"
                        ) from exc
                    plugin = SandboxCommandTool(spec, output_parser=parser)
                registry.register(plugin)
            except ValueError as exc:
                raise ToolPackInstallError(str(exc)) from exc
    return registry


class RuntimeImageInfo(StrictModel):
    image: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/amd64", "linux/arm64"]
    user: str

    @field_validator("user")
    @classmethod
    def image_user_is_non_root(cls, value: str) -> str:
        normalized = value.strip().lower()
        uid = normalized.split(":", 1)[0]
        if not normalized or uid in {"0", "root"}:
            raise ValueError("tool images must declare a non-root user")
        return value


class RuntimeSmokeResult(StrictModel):
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class ToolPackRuntimeAdapter(Protocol):
    async def pull(self, image: str) -> None: ...

    async def inspect(self, image: str) -> RuntimeImageInfo: ...

    async def smoke_test(
        self,
        *,
        image: str,
        command: list[str],
        timeout_seconds: int,
    ) -> RuntimeSmokeResult: ...


class ToolPackInstaller:
    """Transactional state machine around an explicitly selected OCI runtime."""

    _transitions = {
        ToolPackInstallationStatus.PENDING: {
            ToolPackInstallationStatus.PULLING,
            ToolPackInstallationStatus.FAILED,
            ToolPackInstallationStatus.DISABLED,
        },
        ToolPackInstallationStatus.PULLING: {
            ToolPackInstallationStatus.VERIFYING,
            ToolPackInstallationStatus.FAILED,
            ToolPackInstallationStatus.DISABLED,
        },
        ToolPackInstallationStatus.VERIFYING: {
            ToolPackInstallationStatus.READY,
            ToolPackInstallationStatus.FAILED,
            ToolPackInstallationStatus.DISABLED,
        },
        ToolPackInstallationStatus.READY: {
            ToolPackInstallationStatus.VERIFYING,
            ToolPackInstallationStatus.DISABLED,
        },
        ToolPackInstallationStatus.FAILED: {
            ToolPackInstallationStatus.VERIFYING,
            ToolPackInstallationStatus.DISABLED,
        },
        ToolPackInstallationStatus.DISABLED: {
            ToolPackInstallationStatus.VERIFYING,
        },
    }

    def __init__(
        self,
        *,
        store: NebulaStore,
        manifests: ImmutableManifestStore,
        runtime: ToolPackRuntimeAdapter,
        runtime_profile_id: str,
        platform: Literal["linux/amd64", "linux/arm64"],
        verifier: SignatureVerifier,
        parser_executor: ParserContainerExecutor | None = None,
        core_version: str = __version__,
        developer_mode: bool = False,
    ) -> None:
        self.store = store
        self.manifests = manifests
        self.runtime = runtime
        self.runtime_profile_id = runtime_profile_id
        self.platform = platform
        self.verifier = verifier
        self.parser_executor = parser_executor
        self.core_version = core_version
        self.developer_mode = developer_mode

    async def install(
        self,
        manifest: ToolPackManifestV1,
        *,
        source: str,
        signature: SignatureEnvelope | None,
        local_file: bool = False,
        confirm_unsigned_permissions: bool = False,
    ) -> ToolPackInstallation:
        manifest.ensure_core_compatible(self.core_version)
        publisher_key_id: str | None = None
        if signature is not None:
            publisher_key_id = self.verifier.verify_publisher(
                canonical_manifest_json(manifest),
                signature,
                manifest.metadata.publisher,
            )
            trust = (
                ToolPackTrust.CURATED
                if manifest.metadata.publisher == "berylliumsec"
                else ToolPackTrust.TRUSTED_PUBLISHER
            )
        else:
            if not (
                self.developer_mode and local_file and confirm_unsigned_permissions
            ):
                raise SignatureVerificationError(
                    "unsigned packs require developer mode, a local file, and "
                    "explicit permission confirmation"
                )
            trust = ToolPackTrust.LOCAL_UNSIGNED

        if trust == ToolPackTrust.CURATED:
            manifest.ensure_curated_multiarch()
        for tool in manifest.tools:
            manifest.image_for(tool.image, self.platform)

        digest = manifest_digest(manifest)
        path = self.manifests.put(manifest, signature)
        images = self._manifest_image_locks(manifest)
        installation = self.store.create(
            ToolPackInstallation(
                publisher=manifest.metadata.publisher,
                name=manifest.metadata.name,
                version=manifest.metadata.version,
                manifest_digest=digest,
                source=source,
                trust=trust,
                publisher_key_id=publisher_key_id,
                runtime_profile_id=self.runtime_profile_id,
                image_locks=images,
                manifest_path=str(path),
            )
        )
        try:
            installation = self._transition(
                installation, ToolPackInstallationStatus.PULLING
            )
            for image in sorted(set(images.values())):
                await self.runtime.pull(image)
            installation = self._transition(
                installation, ToolPackInstallationStatus.VERIFYING
            )
            await self._verify_runtime(manifest)
            return self._transition(
                installation,
                ToolPackInstallationStatus.READY,
                installed_at=utc_now(),
                verified_at=utc_now(),
                failure_detail=None,
            )
        except Exception as exc:
            self._transition(
                installation,
                ToolPackInstallationStatus.FAILED,
                failure_detail=str(exc)[:4000],
            )
            if isinstance(exc, ToolPackError):
                raise
            raise ToolPackInstallError(f"installation failed: {exc}") from exc

    async def verify(self, installation_id: str) -> ToolPackInstallation:
        installation = self.store.get(ToolPackInstallation, installation_id)
        if installation.status == ToolPackInstallationStatus.DISABLED:
            raise ToolPackInstallError(
                "disabled tool packs cannot be re-enabled by verification"
            )
        installation = self._transition(
            installation, ToolPackInstallationStatus.VERIFYING
        )
        try:
            manifest = self.manifests.get(installation.manifest_digest)
            self._verify_installation_record(installation, manifest)
            await self._verify_runtime(manifest)
            return self._transition(
                installation,
                ToolPackInstallationStatus.READY,
                verified_at=utc_now(),
                failure_detail=None,
            )
        except Exception as exc:
            self._transition(
                installation,
                ToolPackInstallationStatus.FAILED,
                failure_detail=str(exc)[:4000],
            )
            if isinstance(exc, ToolPackError):
                raise
            raise ToolPackInstallError(f"verification failed: {exc}") from exc

    def disable(self, installation_id: str) -> ToolPackInstallation:
        installation = self.store.get(ToolPackInstallation, installation_id)
        return self._transition(installation, ToolPackInstallationStatus.DISABLED)

    async def _verify_runtime(self, manifest: ToolPackManifestV1) -> None:
        for component, image in sorted(self._manifest_image_locks(manifest).items()):
            del component
            info = await self.runtime.inspect(image)
            expected_digest = image.rsplit("@", 1)[1]
            if info.image != image or info.digest != expected_digest:
                raise ToolPackInstallError(f"runtime image digest mismatch: {image}")
            if info.platform != self.platform:
                raise ToolPackInstallError(f"runtime image platform mismatch: {image}")
        specs = {spec.name: spec for spec in manifest.tool_specs(self.platform)}
        for tool in manifest.tools:
            image = manifest.image_for(tool.image, self.platform).image
            spec = specs[tool.name]
            for smoke in tool.smoke_tests:
                result = await self.runtime.smoke_test(
                    image=image,
                    command=build_declared_command(spec, smoke.arguments),
                    timeout_seconds=smoke.timeout_seconds,
                )
                if result.exit_code != smoke.expected_exit_code:
                    raise ToolPackInstallError(
                        f"smoke test failed for {tool.name}: exit {result.exit_code}"
                    )
                parsed = await self._parse_smoke_output(tool, result.stdout)
                errors = list(
                    Draft202012Validator(tool.output_schema).iter_errors(parsed)
                )
                if errors:
                    raise ToolPackInstallError(
                        f"smoke-test output violates {tool.name} schema: "
                        f"{errors[0].message}"
                    )

    async def _parse_smoke_output(
        self, tool: ToolPackTool, stdout: str
    ) -> dict[str, Any]:
        if tool.parser.built_in is not None:
            try:
                return BUILTIN_PARSERS[tool.parser.built_in](stdout, "", 0)
            except Exception as exc:
                raise ToolPackInstallError(
                    f"smoke-test output could not be parsed for {tool.name}"
                ) from exc
        if self.parser_executor is None:
            raise ToolPackInstallError(
                f"parser-container verification is not configured for {tool.name}"
            )
        assert tool.parser.container is not None
        try:
            return await self.parser_executor.parse(
                tool.parser.container, stdout.encode("utf-8")
            )
        except Exception as exc:
            raise ToolPackInstallError(
                f"parser-container smoke test failed for {tool.name}"
            ) from exc

    def _manifest_image_locks(self, manifest: ToolPackManifestV1) -> dict[str, str]:
        locks = {
            tool.image: manifest.image_for(tool.image, self.platform).image
            for tool in manifest.tools
        }
        for tool in manifest.tools:
            if tool.parser.container is not None:
                locks[f"parser:{tool.name}"] = tool.parser.container.image
        return locks

    def _verify_installation_record(
        self,
        installation: ToolPackInstallation,
        manifest: ToolPackManifestV1,
    ) -> None:
        expected_path = self.manifests.manifest_path(installation.manifest_digest)
        if Path(installation.manifest_path) != expected_path:
            raise ToolPackInstallError("installed manifest path is not canonical")
        if (
            installation.publisher != manifest.metadata.publisher
            or installation.name != manifest.metadata.name
            or installation.version != manifest.metadata.version
        ):
            raise ToolPackInstallError(
                "installed pack identity does not match manifest"
            )
        if installation.runtime_profile_id != self.runtime_profile_id:
            raise ToolPackInstallError(
                "tool pack belongs to a different runner profile"
            )
        if installation.image_locks != self._manifest_image_locks(manifest):
            raise ToolPackInstallError("installed image locks do not match manifest")
        if installation.trust == ToolPackTrust.LOCAL_UNSIGNED:
            if installation.publisher_key_id is not None:
                raise SignatureVerificationError(
                    "unsigned pack cannot contain publisher-key attribution"
                )
            return
        signature = self.manifests.get_signature(installation.manifest_digest)
        key_id = self.verifier.verify_publisher(
            canonical_manifest_json(manifest), signature, manifest.metadata.publisher
        )
        if not installation.publisher_key_id or key_id != installation.publisher_key_id:
            raise SignatureVerificationError(
                "stored publisher-key attribution does not match signature"
            )
        expected_trust = (
            ToolPackTrust.CURATED
            if manifest.metadata.publisher == "berylliumsec"
            else ToolPackTrust.TRUSTED_PUBLISHER
        )
        if installation.trust != expected_trust:
            raise SignatureVerificationError(
                "stored tool-pack trust classification does not match publisher"
            )

    def _transition(
        self,
        installation: ToolPackInstallation,
        status: ToolPackInstallationStatus,
        **changes: Any,
    ) -> ToolPackInstallation:
        if status not in self._transitions[installation.status]:
            raise ToolPackInstallError(
                f"invalid installation transition: {installation.status} -> {status}"
            )
        changes["status"] = status
        return self.store.update(
            ToolPackInstallation,
            installation.id,
            changes,
            expected_revision=installation.revision,
        )
