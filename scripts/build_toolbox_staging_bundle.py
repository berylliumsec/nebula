"""Build an unsigned, digest-resolved Toolbox bundle for pre-release testing."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Sequence

import yaml

from nebula.v3.tool_interfaces import load_interface_catalog
from nebula.v3.toolpack_sdk import pack_tool_pack, read_tool_pack
from nebula.v3.toolpacks import manifest_digest


DEFAULT_SOURCE_ROOT = Path("src/nebula/v3/tool_pack_assets/toolbox/environment")
DEFAULT_STAGING_NAME = "nebula-toolbox-staging"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REGISTRY = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)


class StagingBundleError(RuntimeError):
    """Raised when staging inputs cannot produce an installable local bundle."""


def _digest(value: str, platform: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise StagingBundleError(
            f"{platform} digest must be lowercase sha256:<64 hexadecimal characters>"
        )
    return value


def _registry(value: str) -> str:
    candidate = value.rstrip("/")
    if not _REGISTRY.fullmatch(candidate) or "@" in candidate:
        raise StagingBundleError(
            "image registry must be an untagged lowercase OCI repository"
        )
    return candidate


def _source_manifest(path: Path) -> dict[str, object]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StagingBundleError("cannot read the Toolbox source manifest") from exc
    if not isinstance(payload, dict):
        raise StagingBundleError("Toolbox source manifest must be an object")
    return payload


def build_staging_bundle(
    *,
    source_root: Path,
    output: Path,
    image_registry: str,
    amd64_digest: str,
    arm64_digest: str,
    interface_catalog: Path,
    version: str,
    source_revision: str,
    staging_name: str = DEFAULT_STAGING_NAME,
) -> dict[str, object]:
    """Resolve the source manifest and pack an unsigned developer artifact."""

    source = source_root.expanduser().resolve()
    if not source.is_dir():
        raise StagingBundleError(f"Toolbox source root does not exist: {source}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", staging_name):
        raise StagingBundleError("staging name must be a canonical tool-pack name")
    if not version or len(version) > 100:
        raise StagingBundleError("staging version must contain 1 to 100 characters")
    if not re.fullmatch(r"[0-9a-f]{7,64}", source_revision):
        raise StagingBundleError("source revision must be a lowercase Git commit id")

    registry = _registry(image_registry)
    digests = {
        "linux/amd64": _digest(amd64_digest, "linux/amd64"),
        "linux/arm64": _digest(arm64_digest, "linux/arm64"),
    }
    try:
        raw_catalog = interface_catalog.expanduser().resolve().read_bytes()
        catalog = load_interface_catalog(raw_catalog)
    except Exception as exc:
        raise StagingBundleError("interface catalog is invalid") from exc

    destination = output.expanduser().resolve()
    if destination.exists():
        raise StagingBundleError(
            f"refusing to overwrite existing bundle: {destination}"
        )

    with tempfile.TemporaryDirectory(prefix="nebula-toolbox-staging-") as temporary:
        root = Path(temporary) / staging_name
        shutil.copytree(source, root)
        manifest_path = root / "nebula-tool-pack.yaml"
        manifest = _source_manifest(manifest_path)

        metadata = manifest.get("metadata")
        images = manifest.get("images")
        if not isinstance(metadata, dict) or not isinstance(images, list):
            raise StagingBundleError("Toolbox source manifest has invalid metadata")
        metadata["name"] = staging_name
        metadata["version"] = version
        description = metadata.get("description")
        metadata["description"] = (
            f"Unsigned staging build from {source_revision}. "
            f"{description if isinstance(description, str) else ''}"
        ).strip()

        resolved_platforms: set[str] = set()
        image_locks: dict[str, str] = {}
        for item in images:
            if not isinstance(item, dict):
                raise StagingBundleError("Toolbox image entry must be an object")
            platform = item.get("platform")
            if not isinstance(platform, str) or platform not in digests:
                raise StagingBundleError(
                    f"unsupported Toolbox image platform: {platform}"
                )
            item["image"] = f"{registry}@{digests[platform]}"
            resolved_platforms.add(platform)
            image_locks[platform] = item["image"]
        if resolved_platforms != set(digests):
            raise StagingBundleError(
                "Toolbox manifest must declare amd64 and arm64 images"
            )

        manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
            newline="\n",
        )
        (root / "interface-catalog.json").write_bytes(raw_catalog)
        staging_metadata = {
            "interface_catalog_digest": catalog.digest,
            "interface_tool_count": len(catalog.tools),
            "source_revision": source_revision,
            "trust": "local_unsigned",
            "images": image_locks,
        }
        (root / "staging-metadata.json").write_text(
            json.dumps(staging_metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        pack_tool_pack(root, destination)

    archive = read_tool_pack(destination)
    if archive.manifest.metadata.name != staging_name:
        raise StagingBundleError("packed staging manifest has the wrong identity")
    if "source/interface-catalog.json" not in archive.files:
        raise StagingBundleError("packed staging bundle lost its interface catalog")
    return {
        "bundle": str(destination),
        "bundle_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "identity": archive.manifest.identity,
        "manifest_digest": manifest_digest(archive.manifest),
        **staging_metadata,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-registry", required=True)
    parser.add_argument("--amd64-digest", required=True)
    parser.add_argument("--arm64-digest", required=True)
    parser.add_argument("--interface-catalog", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--staging-name", default=DEFAULT_STAGING_NAME)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_staging_bundle(
            source_root=args.source_root,
            output=args.output,
            image_registry=args.image_registry,
            amd64_digest=args.amd64_digest,
            arm64_digest=args.arm64_digest,
            interface_catalog=args.interface_catalog,
            version=args.version,
            source_revision=args.source_revision,
            staging_name=args.staging_name,
        )
    except StagingBundleError as exc:
        _parser().error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
