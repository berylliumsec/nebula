"""Resolve, sign, and assemble an immutable Nebula tool-pack catalog.

The private Ed25519 key is read from an operator-owned file and is never
written into the output tree. Image digests and release attachments must
already exist; this script does not manufacture supply-chain evidence.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nebula.v3.toolpacks import (
    SignatureEnvelope,
    ToolCatalogEntry,
    ToolCatalogV1,
    canonical_catalog_json,
    canonical_manifest_json,
    compile_manifest_yaml,
    manifest_digest,
)
from nebula.v3.toolpack_sdk import pack_tool_pack


PLACEHOLDER = re.compile(r"\{\{sha256:([a-z0-9._-]+)\}\}")
KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
SHA256 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


class CatalogBuildError(RuntimeError):
    """Release input is incomplete, unsafe, or internally inconsistent."""


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        raw = path.expanduser().read_bytes()
        key = serialization.load_pem_private_key(raw, password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise CatalogBuildError("cannot load the Ed25519 release key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise CatalogBuildError("release key is not Ed25519")
    return key


def _signature(key: Ed25519PrivateKey, key_id: str, payload: bytes) -> bytes:
    envelope = SignatureEnvelope(
        key_id=key_id,
        signature=base64.b64encode(key.sign(payload)).decode("ascii"),
    )
    return (
        json.dumps(
            envelope.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _digest_map(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogBuildError("digest map is not valid JSON") from exc
    if not isinstance(payload, dict) or not payload:
        raise CatalogBuildError("digest map must contain named image digests")
    result: dict[str, str] = {}
    for name, value in payload.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise CatalogBuildError("digest map keys and values must be strings")
        match = SHA256.fullmatch(value)
        if match is None:
            raise CatalogBuildError(f"invalid image digest for {name}")
        result[name] = match.group(1)
    return result


def _resolve_manifest(source: str, digests: dict[str, str]) -> str:
    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        digest = digests.get(name)
        if digest is None:
            raise CatalogBuildError(f"missing release digest: {name}")
        used.add(name)
        return f"sha256:{digest}"

    resolved = PLACEHOLDER.sub(replace, source)
    if PLACEHOLDER.search(resolved):
        raise CatalogBuildError("manifest contains unresolved image digests")
    return resolved


def _safe_attachment(pack_root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise CatalogBuildError("release attachment escapes the pack directory")
    path = (pack_root / Path(*pure.parts)).resolve()
    root = pack_root.resolve()
    if root not in path.parents or path.is_symlink() or not path.is_file():
        raise CatalogBuildError(f"release attachment is missing: {relative}")
    return path


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_bytes(payload)


def _public_keyring(key: Ed25519PrivateKey, key_id: str) -> bytes:
    public = key.public_key()
    assert isinstance(public, Ed25519PublicKey)
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return (
        json.dumps(
            {
                "keys": {
                    key_id: {
                        "public_key": base64.b64encode(raw).decode("ascii"),
                        "publishers": ["berylliumsec"],
                    }
                }
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def build_catalog(
    *,
    source_root: Path,
    output_root: Path,
    digest_file: Path,
    private_key_file: Path,
    key_id: str,
    generated_at: datetime,
    base_url: str,
) -> Path:
    if not KEY_ID.fullmatch(key_id):
        raise CatalogBuildError("invalid release key ID")
    if generated_at.tzinfo is None:
        raise CatalogBuildError("catalog timestamp must include a timezone")
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise CatalogBuildError("catalog output directory must be empty")
    if not base_url.startswith("https://") or not base_url.endswith("/"):
        raise CatalogBuildError("catalog base URL must be absolute HTTPS and end in /")
    config = yaml.safe_load((source_root / "catalog-build.yaml").read_text("utf-8"))
    configured = config.get("packs") if isinstance(config, dict) else None
    if not isinstance(configured, list) or not configured:
        raise CatalogBuildError("catalog-build.yaml has no configured packs")
    digests = _digest_map(digest_file)
    key = _load_private_key(private_key_file)
    entries: list[ToolCatalogEntry] = []
    output_root.mkdir(parents=True, exist_ok=True, mode=0o755)

    with tempfile.TemporaryDirectory(prefix="nebula-tool-catalog-") as temporary_name:
        temporary_root = Path(temporary_name)
        for order, relative in enumerate(configured):
            if not isinstance(relative, str):
                raise CatalogBuildError("pack paths must be strings")
            manifest_path = (source_root / relative).resolve()
            if source_root not in manifest_path.parents or manifest_path.is_symlink():
                raise CatalogBuildError("pack manifest escapes the release root")
            source = manifest_path.read_text(encoding="utf-8")
            resolved_source = _resolve_manifest(source, digests)
            manifest = compile_manifest_yaml(resolved_source)
            canonical = canonical_manifest_json(manifest)
            digest = manifest_digest(manifest)
            relative_root = Path(
                "packs",
                manifest.metadata.publisher,
                manifest.metadata.name,
                manifest.metadata.version,
            )
            destination = output_root / relative_root
            _write(destination / "manifest.json", canonical + b"\n")
            _write(
                destination / "manifest.signature.json",
                _signature(key, key_id, canonical),
            )
            resolved_pack = temporary_root / manifest.metadata.name
            shutil.copytree(manifest_path.parent, resolved_pack)
            (resolved_pack / "nebula-tool-pack.yaml").write_text(
                resolved_source, encoding="utf-8", newline="\n"
            )
            for image in manifest.images:
                for attachment in (image.sbom, image.provenance):
                    source_attachment = _safe_attachment(
                        manifest_path.parent, attachment
                    )
                    target = destination / Path(*PurePosixPath(attachment).parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    shutil.copyfile(source_attachment, target)

            bundle = (
                output_root
                / "bundles"
                / f"{manifest.metadata.name}-{manifest.metadata.version}.nebula-toolpack"
            )
            bundle.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            pack_tool_pack(resolved_pack, bundle)

            permissions = []
            if manifest.permissions.network:
                permissions.append("network")
            if manifest.permissions.workspace != "none":
                permissions.append(f"workspace_{manifest.permissions.workspace}")
            permissions.extend(
                f"credential_{value}" for value in manifest.permissions.credentials
            )
            entries.append(
                ToolCatalogEntry(
                    publisher=manifest.metadata.publisher,
                    name=manifest.metadata.name,
                    version=manifest.metadata.version,
                    description=manifest.metadata.description,
                    manifest_digest=digest,
                    manifest_url=urljoin(
                        base_url, f"{relative_root.as_posix()}/manifest.json"
                    ),
                    signature_url=urljoin(
                        base_url,
                        f"{relative_root.as_posix()}/manifest.signature.json",
                    ),
                    collection_id="safe-foundation",
                    collection_name="Safe Foundation",
                    collection_order=order,
                    minimum_nebula_version=manifest.metadata.minimum_nebula_version,
                    licenses=sorted(manifest.metadata.licenses),
                    platforms=sorted({image.platform for image in manifest.images}),
                    tool_names=sorted(tool.name for tool in manifest.tools),
                    permissions=sorted(permissions),
                )
            )

    catalog = ToolCatalogV1(
        generated_at=generated_at.astimezone(timezone.utc), entries=entries
    )
    canonical_catalog = canonical_catalog_json(catalog)
    _write(output_root / "catalog-v1.json", canonical_catalog + b"\n")
    _write(
        output_root / "catalog-v1.json.signature.json",
        _signature(key, key_id, canonical_catalog),
    )
    _write(
        output_root / "berylliumsec-tool-pack-keys.json", _public_keyring(key, key_id)
    )
    return output_root


def generate_key(destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        if destination.exists():
            raise CatalogBuildError("refusing to overwrite an existing release key")
        key = Ed25519PrivateKey.generate()
        payload = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        return destination
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate-key")
    generate.add_argument("destination", type=Path)
    build = subparsers.add_parser("build")
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--digests", type=Path, required=True)
    build.add_argument("--private-key", type=Path, required=True)
    build.add_argument("--key-id", required=True)
    build.add_argument("--generated-at", required=True)
    build.add_argument("--base-url", required=True)
    args = parser.parse_args()
    if args.command == "generate-key":
        generate_key(args.destination)
        return 0
    try:
        generated_at = datetime.fromisoformat(args.generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SystemExit("--generated-at must be ISO-8601") from exc
    build_catalog(
        source_root=args.source_root,
        output_root=args.output,
        digest_file=args.digests,
        private_key_file=args.private_key,
        key_id=args.key_id,
        generated_at=generated_at,
        base_url=args.base_url,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
