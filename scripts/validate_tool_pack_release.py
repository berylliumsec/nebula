"""Validate Safe Foundation source or a resolved offline-signing candidate."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml
from yaml.tokens import AliasToken

from nebula.v3.toolpack_sdk import ToolPackSDKError, validate_tool_pack_directory


DEFAULT_ROOT = Path("src/nebula/v3/tool_pack_assets/safe_foundation")
PLACEHOLDER = re.compile(rb"\{\{sha256:[a-z0-9._-]+\}\}")
REGISTRY = re.compile(
    r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$"
)
CONTAINER_FROM = re.compile(rb"(?im)^FROM\s+(?:--platform=[^\s]+\s+)?([^\s]+)")


class ReleaseSourceError(RuntimeError):
    """The release-source layout is ambiguous or structurally unsafe."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ReleaseSourceError(f"duplicate release-config key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _load_config(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
        if any(isinstance(token, AliasToken) for token in yaml.scan(source)):
            raise ReleaseSourceError("release config cannot contain YAML aliases")
        payload = yaml.load(source, Loader=_UniqueLoader)
    except ReleaseSourceError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ReleaseSourceError("cannot read catalog-build.yaml safely") from exc
    if not isinstance(payload, dict):
        raise ReleaseSourceError("catalog-build.yaml must contain one object")
    expected = {
        "catalog_url",
        "catalog_signature_url",
        "image_registry",
        "packs",
    }
    if set(payload) != expected:
        raise ReleaseSourceError(
            "catalog-build.yaml must contain exactly: " + ", ".join(sorted(expected))
        )
    return payload


def _https_url(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ReleaseSourceError(f"{field} must be an HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseSourceError(
            f"{field} must be a credential-free HTTPS URL without query or fragment"
        )
    return value


def _relative_path(root: Path, value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ReleaseSourceError(f"{field} must be a portable relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise ReleaseSourceError(f"{field} escapes the release-source directory")
    candidate = (root / Path(*pure.parts)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ReleaseSourceError(f"{field} escapes the release-source directory")
    return candidate


def _release_attachment(pack_root: Path, value: str, *, field: str) -> Path:
    candidate = _relative_path(pack_root, value, field=field)
    if candidate.is_symlink():
        raise ReleaseSourceError(f"{field} cannot be a symlink")
    return candidate


def _attachment_error(path: Path, *, kind: str) -> str | None:
    try:
        if kind == "SBOM":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return "SBOM must contain one JSON object"
            cyclone_dx = payload.get("bomFormat") == "CycloneDX" and isinstance(
                payload.get("specVersion"), str
            )
            spdx = isinstance(payload.get("spdxVersion"), str) and payload[
                "spdxVersion"
            ].startswith("SPDX-")
            if not (cyclone_dx or spdx):
                return "SBOM is neither CycloneDX nor SPDX JSON"
            return None
        statements = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not statements:
            return "provenance JSONL has no statements"
        if any(
            not isinstance(statement, dict)
            or "in-toto.io/Statement/" not in str(statement.get("_type", ""))
            or not isinstance(statement.get("subject"), list)
            or not statement["subject"]
            for statement in statements
        ):
            return "provenance JSONL has an invalid in-toto statement"
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return f"{kind} is not valid UTF-8 JSON: {exc.__class__.__name__}"


def validate_release_source(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Return a report; candidate readiness never implies signature readiness."""

    release_root = root.expanduser().resolve()
    if not release_root.is_dir():
        raise ReleaseSourceError(
            f"tool-pack release root does not exist: {release_root}"
        )
    config_path = release_root / "catalog-build.yaml"
    if config_path.is_symlink():
        raise ReleaseSourceError("catalog-build.yaml cannot be a symlink")
    config = _load_config(config_path)
    catalog_url = _https_url(config["catalog_url"], field="catalog_url")
    signature_url = _https_url(
        config["catalog_signature_url"], field="catalog_signature_url"
    )
    registry = config["image_registry"]
    if not isinstance(registry, str) or not REGISTRY.fullmatch(registry):
        raise ReleaseSourceError(
            "image_registry must be a lowercase registry/repository without tag or digest"
        )
    configured_packs = config["packs"]
    if (
        not isinstance(configured_packs, list)
        or not configured_packs
        or any(not isinstance(value, str) for value in configured_packs)
    ):
        raise ReleaseSourceError("packs must be a non-empty list of manifest paths")
    if len(configured_packs) != len(set(configured_packs)):
        raise ReleaseSourceError("packs cannot contain duplicate manifest paths")
    discovered_packs = sorted(
        path.relative_to(release_root).as_posix()
        for path in release_root.glob("*/nebula-tool-pack.yaml")
    )
    if sorted(configured_packs) != discovered_packs:
        raise ReleaseSourceError(
            "catalog-build.yaml must list every Safe Foundation manifest exactly once"
        )

    reports: list[dict[str, Any]] = []
    blockers: list[str] = []
    identities: set[str] = set()
    for relative in configured_packs:
        manifest_path = _relative_path(release_root, relative, field="pack path")
        if manifest_path.name != "nebula-tool-pack.yaml" or not manifest_path.is_file():
            raise ReleaseSourceError(f"configured pack manifest is missing: {relative}")
        pack_root = manifest_path.parent
        try:
            manifest = validate_tool_pack_directory(
                pack_root, allow_digest_placeholders=True
            )
            manifest.ensure_curated_multiarch()
        except ToolPackSDKError as exc:
            raise ReleaseSourceError(f"invalid pack source {relative}: {exc}") from exc
        if manifest.metadata.publisher != "berylliumsec":
            raise ReleaseSourceError(
                f"curated pack has unexpected publisher: {manifest.identity}"
            )
        if manifest.identity in identities:
            raise ReleaseSourceError(f"duplicate pack identity: {manifest.identity}")
        identities.add(manifest.identity)
        for image in manifest.images:
            if not image.image.startswith(f"{registry}/"):
                raise ReleaseSourceError(
                    f"{manifest.identity} image is outside image_registry: {image.image}"
                )

        placeholder_names: set[str] = set()
        unpinned_base_images: set[str] = set()
        for path in sorted(pack_root.rglob("*")):
            if path.is_symlink():
                raise ReleaseSourceError(f"pack source cannot contain symlinks: {path}")
            if path.is_file():
                content = path.read_bytes()
                placeholder_names.update(
                    match.decode("ascii") for match in PLACEHOLDER.findall(content)
                )
                if path.name.startswith("Containerfile"):
                    for reference in CONTAINER_FROM.findall(content):
                        if PLACEHOLDER.search(reference):
                            continue
                        decoded = reference.decode("utf-8", errors="replace")
                        if decoded != "scratch" and not re.fullmatch(
                            r"[^\s@]+@sha256:[0-9a-f]{64}", decoded
                        ):
                            unpinned_base_images.add(decoded)
        missing_attachments: list[str] = []
        invalid_attachments: list[str] = []
        for image in manifest.images:
            for label, relative_attachment in (
                ("SBOM", image.sbom),
                ("provenance", image.provenance),
            ):
                attachment = _release_attachment(
                    pack_root,
                    relative_attachment,
                    field=f"{manifest.identity} {label}",
                )
                if not attachment.is_file():
                    missing_attachments.append(relative_attachment)
                else:
                    error = _attachment_error(attachment, kind=label)
                    if error is not None:
                        invalid_attachments.append(f"{relative_attachment}: {error}")

        pack_blockers: list[str] = []
        if placeholder_names:
            pack_blockers.append(
                f"{len(placeholder_names)} unresolved digest placeholder(s)"
            )
        if missing_attachments:
            pack_blockers.append(
                f"{len(set(missing_attachments))} missing SBOM/provenance file(s)"
            )
        if unpinned_base_images:
            pack_blockers.append(
                f"{len(unpinned_base_images)} mutable Containerfile base image(s)"
            )
        if invalid_attachments:
            pack_blockers.append(
                f"{len(invalid_attachments)} invalid SBOM/provenance file(s)"
            )
        blockers.extend(f"{manifest.identity}: {item}" for item in pack_blockers)
        reports.append(
            {
                "identity": manifest.identity,
                "manifest": relative,
                "tools": sorted(tool.name for tool in manifest.tools),
                "platforms": sorted({image.platform for image in manifest.images}),
                "unresolved_digest_placeholders": sorted(placeholder_names),
                "missing_release_attachments": sorted(set(missing_attachments)),
                "invalid_release_attachments": sorted(set(invalid_attachments)),
                "mutable_containerfile_base_images": sorted(unpinned_base_images),
                "candidate_ready": not pack_blockers,
            }
        )

    return {
        "status": "valid-source",
        "candidate_ready_for_offline_signing": not blockers,
        "publication_ready": False,
        "publication_note": (
            "This validator does not create or verify release signatures and never publishes."
        ),
        "catalog_url": catalog_url,
        "catalog_signature_url": signature_url,
        "image_registry": registry,
        "packs": reports,
        "blockers": blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--require-candidate-ready",
        action="store_true",
        help="Fail unless all digests and SBOM/provenance paths are resolved.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Retained for an explicit stable CLI contract.",
    )
    args = parser.parse_args(argv)
    try:
        report = validate_release_source(args.root)
    except ReleaseSourceError as exc:
        print(json.dumps({"status": "invalid", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    if (
        args.require_candidate_ready
        and not report["candidate_ready_for_offline_signing"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
