import json
from pathlib import Path

import pytest

from nebula.v3.toolpack_sdk import read_tool_pack
from scripts.build_toolbox_staging_bundle import (
    StagingBundleError,
    build_staging_bundle,
)
from scripts.compare_toolbox_interface_catalogs import (
    InterfaceCatalogMismatch,
    compare_interface_catalogs,
)


TOOLBOX = (
    Path(__file__).parents[2] / "src/nebula/v3/tool_pack_assets/toolbox/environment"
)


def _write_interface_catalog(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "protocol": "nebula.toolbox.catalog/v2",
                "interface_protocol": "nebula.toolbox.interface/v2",
                "toolbox_version": "0.1.0",
                "tools": [
                    {
                        "protocol": "nebula.toolbox.interface/v2",
                        "name": "fixture",
                        "version": "1.0.0",
                        "executable": "/usr/bin/fixture",
                        "aliases": [],
                        "category": "local",
                        "risk_class": "local_read",
                        "description": "Staging bundle fixture.",
                        "homepage": "https://example.com/fixture",
                        "synopsis": "fixture [options]",
                        "examples": [{"purpose": "Show help", "arguments": ["--help"]}],
                        "notes": ["Test-only exact interface."],
                        "commands": [
                            {
                                "path": [],
                                "synopsis": "fixture [options]",
                                "options": [],
                                "positionals": [],
                                "help_documents": [
                                    {
                                        "command_path": [],
                                        "argv": ["/usr/bin/fixture", "--help"],
                                        "exit_code": 0,
                                        "sha256": "c" * 64,
                                        "text": "fixture [options]",
                                    }
                                ],
                            }
                        ],
                        "coverage": {
                            "complete": True,
                            "help_documents": 1,
                            "documented_options": 0,
                            "structured_options": 0,
                            "unmapped_options": [],
                        },
                    }
                ],
                "inventory": [],
            }
        ),
        encoding="utf-8",
    )


def test_staging_bundle_resolves_both_images_and_embeds_catalog(tmp_path):
    interface_catalog = tmp_path / "interface-catalog.json"
    _write_interface_catalog(interface_catalog)
    output = tmp_path / "nebula-toolbox-staging.nebula-toolpack"

    result = build_staging_bundle(
        source_root=TOOLBOX,
        output=output,
        image_registry="ghcr.io/berylliumsec/nebula-toolbox-staging",
        amd64_digest="sha256:" + "a" * 64,
        arm64_digest="sha256:" + "b" * 64,
        interface_catalog=interface_catalog,
        version="0.1.0.dev7",
        source_revision="c" * 40,
    )

    archive = read_tool_pack(output)
    assert result["identity"] == "berylliumsec/nebula-toolbox-staging@0.1.0.dev7"
    assert archive.manifest.metadata.name == "nebula-toolbox-staging"
    assert archive.manifest.metadata.version == "0.1.0.dev7"
    assert {runtime.language for runtime in archive.manifest.operator_runtimes} == {
        "bash",
        "sh",
        "python",
    }
    bash = next(
        runtime
        for runtime in archive.manifest.operator_runtimes
        if runtime.language == "bash"
    )
    assert bash.aliases == ["bash", "shell"]
    assert bash.interpreter == "/bin/bash"
    assert {image.platform: image.image for image in archive.manifest.images} == {
        "linux/amd64": (
            "ghcr.io/berylliumsec/nebula-toolbox-staging@sha256:" + "a" * 64
        ),
        "linux/arm64": (
            "ghcr.io/berylliumsec/nebula-toolbox-staging@sha256:" + "b" * 64
        ),
    }
    assert archive.files["source/interface-catalog.json"] == (
        interface_catalog.read_bytes()
    )
    assert result["interface_tool_count"] == 1
    assert len(result["bundle_sha256"]) == 64


def test_staging_bundle_rejects_a_non_digest_image_lock(tmp_path):
    interface_catalog = tmp_path / "interface-catalog.json"
    _write_interface_catalog(interface_catalog)

    with pytest.raises(StagingBundleError, match="linux/amd64 digest"):
        build_staging_bundle(
            source_root=TOOLBOX,
            output=tmp_path / "staging.nebula-toolpack",
            image_registry="ghcr.io/berylliumsec/nebula-toolbox-staging",
            amd64_digest="latest",
            arm64_digest="sha256:" + "b" * 64,
            interface_catalog=interface_catalog,
            version="0.1.0.dev7",
            source_revision="c" * 40,
        )


def test_catalog_comparison_ignores_architecture_evidence_and_unordered_help_lists(
    tmp_path,
):
    amd64 = tmp_path / "amd64.json"
    arm64 = tmp_path / "arm64.json"
    _write_interface_catalog(amd64)
    amd64_payload = json.loads(amd64.read_text(encoding="utf-8"))
    option = {
        "id": "formats",
        "flags": ["--formats"],
        "usage": "--formats VALUE",
        "description": "select formats (url, ipv4, mail)",
        "section": "output",
        "value": {
            "name": "VALUE",
            "type": "string",
            "required": True,
            "style": "separate",
        },
        "repeatable": False,
        "conflicts_with": [],
        "requires": [],
        "implies": [],
    }
    amd64_payload["tools"][0]["commands"][0]["options"].append(option)
    amd64_payload["tools"][0]["coverage"].update(
        {"documented_options": 1, "structured_options": 1}
    )
    amd64_payload["inventory"].append(
        {
            "name": "perl5.36-x86_64-linux-gnu",
            "path": "/usr/bin/perl5.36-x86_64-linux-gnu",
            "catalogued": False,
            "interface": None,
            "aliases": [],
        }
    )
    amd64.write_text(json.dumps(amd64_payload), encoding="utf-8")

    arm64_payload = json.loads(json.dumps(amd64_payload))
    arm64_option = arm64_payload["tools"][0]["commands"][0]["options"][0]
    arm64_option["description"] = "select formats (mail, url, ipv4)"
    document = arm64_payload["tools"][0]["commands"][0]["help_documents"][0]
    document.update({"sha256": "d" * 64, "text": "fixture arm64 help"})
    arm64_payload["inventory"][-1].update(
        {
            "name": "perl5.36-aarch64-linux-gnu",
            "path": "/usr/bin/perl5.36-aarch64-linux-gnu",
        }
    )
    arm64.write_text(json.dumps(arm64_payload), encoding="utf-8")

    result = compare_interface_catalogs(amd64, arm64)

    assert result["tool_count"] == 1
    assert len(result["contract_sha256"]) == 64
    assert result["amd64_catalog_sha256"] != result["arm64_catalog_sha256"]


def test_catalog_comparison_rejects_a_model_facing_difference(tmp_path):
    amd64 = tmp_path / "amd64.json"
    arm64 = tmp_path / "arm64.json"
    _write_interface_catalog(amd64)
    arm64_payload = json.loads(amd64.read_text(encoding="utf-8"))
    arm64_payload["tools"][0]["synopsis"] = "fixture TARGET"
    arm64.write_text(json.dumps(arm64_payload), encoding="utf-8")

    with pytest.raises(InterfaceCatalogMismatch, match="synopsis"):
        compare_interface_catalogs(amd64, arm64)
