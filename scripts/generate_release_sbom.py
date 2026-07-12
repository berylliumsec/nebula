#!/usr/bin/env python3
"""Generate dependency-aware CycloneDX and SPDX SBOMs for a release artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from urllib.parse import quote

from packaging.utils import canonicalize_name

from scripts.generate_third_party_notices import (
    cargo_components,
    locked_main_packages,
    npm_components,
)


def python_components(lockfile: Path) -> list[dict[str, str]]:
    required = locked_main_packages(lockfile)
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name")
    }
    components = []
    for name in sorted(required & installed.keys()):
        distribution = installed[name]
        license_name = (
            distribution.metadata.get("License-Expression")
            or distribution.metadata.get("License")
            or "Not declared"
        ).strip()
        components.append(
            {
                "ecosystem": "pypi",
                "name": distribution.metadata.get("Name", name),
                "version": distribution.version,
                "license": license_name,
            }
        )
    if not components:
        raise RuntimeError("no installed locked Python components were found")
    return components


def component_inventory(
    root: Path, target: str, *, direct: bool
) -> list[dict[str, str]]:
    components = python_components(root / "poetry.lock")
    components.extend(npm_components(root))
    components.extend(cargo_components(root, target, all_features=direct))
    return sorted(
        components,
        key=lambda item: (item["ecosystem"], item["name"].lower(), item["version"]),
    )


def _purl(component: dict[str, str]) -> str:
    ecosystem = component["ecosystem"]
    name = quote(component["name"], safe="/" if ecosystem == "npm" else "")
    return f"pkg:{ecosystem}/{name}@{quote(component['version'], safe='.+-')}"


def write_sboms(
    *, artifact: Path, root: Path, target: str, direct: bool
) -> tuple[Path, Path, int]:
    artifact = artifact.resolve()
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    components = component_inventory(root.resolve(), target, direct=direct)
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    serial = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{artifact.name}:{digest}')}"
    cyclonedx = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial,
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "component": {
                "type": "application",
                "name": artifact.name,
                "version": root.joinpath("NEBULA3_VERSION").read_text().strip(),
                "hashes": [{"alg": "SHA-256", "content": digest}],
            },
        },
        "components": [
            {
                "type": "library",
                "group": component["ecosystem"],
                "name": component["name"],
                "version": component["version"],
                "purl": _purl(component),
                "licenses": [{"license": {"name": component["license"]}}],
            }
            for component in components
        ],
    }
    packages = []
    for index, component in enumerate(components, start=1):
        packages.append(
            {
                "name": component["name"],
                "SPDXID": f"SPDXRef-Package-{index}",
                "versionInfo": component["version"],
                "downloadLocation": "NOASSERTION",
                "filesAnalyzed": False,
                "licenseConcluded": "NOASSERTION",
                "licenseDeclared": "NOASSERTION",
                "copyrightText": "NOASSERTION",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": _purl(component),
                    }
                ],
            }
        )
    spdx = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{artifact.name}-SBOM",
        "documentNamespace": serial.replace(
            "urn:uuid:", "https://nebula.invalid/sbom/"
        ),
        "creationInfo": {"created": timestamp, "creators": ["Tool: Nebula-SBOM-1"]},
        "packages": packages,
        "annotations": [
            {
                "annotationDate": timestamp,
                "annotationType": "OTHER",
                "annotator": "Tool: Nebula-SBOM-1",
                "comment": f"Artifact SHA-256: {digest}",
            }
        ],
    }
    cyclonedx_path = Path(f"{artifact}.cyclonedx.json")
    spdx_path = Path(f"{artifact}.spdx.json")
    cyclonedx_path.write_text(json.dumps(cyclonedx, indent=2) + "\n", encoding="utf-8")
    spdx_path.write_text(json.dumps(spdx, indent=2) + "\n", encoding="utf-8")
    return cyclonedx_path, spdx_path, len(components)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--target", required=True)
    parser.add_argument("--direct", action="store_true")
    arguments = parser.parse_args()
    _, _, count = write_sboms(
        artifact=arguments.artifact,
        root=arguments.root,
        target=arguments.target,
        direct=arguments.direct,
    )
    if count < 10:
        raise RuntimeError(f"release SBOM contained only {count} components")
    print(count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
