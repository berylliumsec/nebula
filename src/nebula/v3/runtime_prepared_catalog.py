"""Verified shared runtime metadata without constructing project controllers."""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from .domain import RunnerProfile as StoredRunnerProfile, utc_now
from .sandbox import PreparedContainerImage

KALI_RUNTIME_METADATA_SCHEMA = "nebula.kali-runtime/v1"


class RuntimeCatalogError(ValueError):
    pass


def _runner_profile_fingerprint(profile: StoredRunnerProfile) -> str:
    """Bind preparation to runner security configuration, not health telemetry."""

    payload = {
        "runtime": profile.runtime.value,
        "executable": profile.executable,
        "context": profile.context,
        "socket": profile.socket,
        "platform": profile.platform,
        "isolation": profile.isolation.value,
        "seccomp_profile": profile.seccomp_profile,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def persist_runtime_metadata(
    data_root: Path, profile: StoredRunnerProfile, image: PreparedContainerImage
) -> None:
    payload = {
        "schema": KALI_RUNTIME_METADATA_SCHEMA,
        "verified_at": utc_now().isoformat(),
        "runner_profile_id": profile.id,
        "runner_profile_revision": profile.revision,
        "runner_profile_fingerprint": _runner_profile_fingerprint(profile),
        "source_reference": image.source_reference,
        "source_is_digest_pinned": "@sha256:" in image.source_reference,
        "base_resolved_reference": image.base_resolved_reference,
        "base_digest": image.base_digest,
        "resolved_reference": image.resolved_reference,
        "image_digest": image.digest,
        "platform": image.platform,
        "installed_packages": list(image.installed_packages),
        "security_tools": list(image.security_tools),
        "security_tool_packages": list(image.security_tool_packages),
        "security_tool_provenance": {
            tool: list(packages) for tool, packages in image.security_tool_provenance
        },
        "security_tool_manifest_sha256": image.security_tool_manifest_sha256,
        "binary_inventory": [
            {"name": name, "path": path, "version": version}
            for name, path, version in image.binary_inventory
        ],
        "registry_refreshed": image.refreshed,
    }
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".kali-runtime-",
            suffix=".tmp",
            dir=data_root,
            delete=False,
        ) as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        temporary.replace((data_root / "kali-runtime.json"))
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise RuntimeCatalogError(
            "verified Kali runtime metadata could not be persisted"
        ) from exc


def read_runtime_metadata(data_root: Path) -> dict[str, Any] | None:
    try:
        with (data_root / "kali-runtime.json").open("rb") as stream:
            content = stream.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            return None
        payload = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError):
        # diagnostic-expected: absent/invalid cached metadata means no verified inventory.
        return None
    return payload if isinstance(payload, dict) else None


def automation_runtime_metadata(data_root: Path) -> dict[str, Any] | None:
    payload = read_runtime_metadata(data_root)
    if payload is None:
        return None
    image_digest = payload.get("image_digest")
    runner_id = payload.get("runner_profile_id")
    runner_revision = payload.get("runner_profile_revision")
    runner_fingerprint = payload.get("runner_profile_fingerprint")
    inventory = payload.get("binary_inventory")
    if (
        payload.get("schema") != KALI_RUNTIME_METADATA_SCHEMA
        or not isinstance(image_digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None
        or not isinstance(runner_id, str)
        or not isinstance(runner_revision, int)
        or not isinstance(inventory, list)
        or not inventory
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("version"), str)
            for item in inventory
        )
    ):
        return None
    return {
        "image": image_digest,
        "digest": image_digest,
        "runner_profile_id": runner_id,
        "runner_profile_revision": runner_revision,
        "runner_profile_fingerprint": (
            runner_fingerprint
            if isinstance(runner_fingerprint, str)
            and re.fullmatch(r"[0-9a-f]{64}", runner_fingerprint)
            else None
        ),
        "inventory": inventory,
        "detail": "using the verified prepared Kali headless image",
    }
