import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from nebula.v3.toolpacks import (
    Ed25519Keyring,
    SignatureEnvelope,
    ToolCatalogV1,
    canonical_catalog_json,
)
from scripts.build_tool_pack_catalog import build_catalog
from nebula.v3.toolpack_sdk import validate_tool_pack_directory
from scripts.validate_tool_pack_release import main, validate_release_source


SAFE_FOUNDATION = (
    Path(__file__).parents[2] / "src/nebula/v3/tool_pack_assets/safe_foundation"
)


def test_safe_foundation_is_valid_source_but_not_a_release_candidate():
    report = validate_release_source(SAFE_FOUNDATION)

    assert report["status"] == "valid-source"
    assert report["candidate_ready_for_offline_signing"] is False
    assert report["publication_ready"] is False
    assert {pack["identity"] for pack in report["packs"]} == {
        "berylliumsec/safe-network@0.1.0",
        "berylliumsec/safe-web@0.1.0",
        "berylliumsec/safe-intelligence@0.1.0",
        "berylliumsec/safe-code@0.1.0",
    }
    assert all(pack["unresolved_digest_placeholders"] for pack in report["packs"])
    assert all(pack["missing_release_attachments"] for pack in report["packs"])


def test_candidate_gate_fails_without_inventing_release_material(capsys):
    assert main(["--root", str(SAFE_FOUNDATION)]) == 0
    assert main(["--root", str(SAFE_FOUNDATION), "--require-candidate-ready"]) == 1
    assert '"publication_ready": false' in capsys.readouterr().out


def test_resolved_temporary_fixture_can_reach_offline_signing_gate(tmp_path):
    candidate = tmp_path / "safe-foundation"
    shutil.copytree(SAFE_FOUNDATION, candidate)
    token = re.compile(rb"\{\{sha256:[a-z0-9._-]+\}\}")
    for path in candidate.rglob("*"):
        if path.is_file():
            path.write_bytes(token.sub(b"sha256:" + b"a" * 64, path.read_bytes()))
    for manifest_path in candidate.glob("*/nebula-tool-pack.yaml"):
        manifest = validate_tool_pack_directory(
            manifest_path.parent, allow_digest_placeholders=False
        )
        for image in manifest.images:
            sbom = manifest_path.parent / image.sbom
            sbom.parent.mkdir(parents=True, exist_ok=True)
            sbom.write_text(
                json.dumps(
                    {
                        "bomFormat": "CycloneDX",
                        "specVersion": "1.6",
                        "version": 1,
                    }
                ),
                encoding="utf-8",
            )
            provenance = manifest_path.parent / image.provenance
            provenance.parent.mkdir(parents=True, exist_ok=True)
            provenance.write_text(
                json.dumps(
                    {
                        "_type": "https://in-toto.io/Statement/v1",
                        "subject": [
                            {"name": image.image, "digest": {"sha256": "a" * 64}}
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

    report = validate_release_source(candidate)

    assert report["candidate_ready_for_offline_signing"] is True
    assert report["publication_ready"] is False
    assert report["blockers"] == []


def test_production_catalog_builder_signs_collection_and_bundles(tmp_path):
    candidate = tmp_path / "safe-foundation"
    shutil.copytree(SAFE_FOUNDATION, candidate)
    digests: dict[str, str] = {}
    for manifest_path in candidate.glob("*/nebula-tool-pack.yaml"):
        for placeholder in re.findall(
            r"\{\{sha256:([a-z0-9._-]+)\}\}",
            manifest_path.read_text(encoding="utf-8"),
        ):
            digests[placeholder] = "a" * 64
        source = re.sub(
            r"\{\{sha256:[a-z0-9._-]+\}\}",
            "sha256:" + "a" * 64,
            manifest_path.read_text(encoding="utf-8"),
        )
        resolved = candidate / "resolved.yaml"
        resolved.write_text(source, encoding="utf-8")
        original = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(source, encoding="utf-8")
        manifest = validate_tool_pack_directory(
            manifest_path.parent, allow_digest_placeholders=False
        )
        manifest_path.write_text(original, encoding="utf-8")
        for image in manifest.images:
            sbom = manifest_path.parent / image.sbom
            sbom.parent.mkdir(parents=True, exist_ok=True)
            sbom.write_text(
                json.dumps(
                    {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1}
                ),
                encoding="utf-8",
            )
            provenance = manifest_path.parent / image.provenance
            provenance.parent.mkdir(parents=True, exist_ok=True)
            provenance.write_text(
                json.dumps(
                    {
                        "_type": "https://in-toto.io/Statement/v1",
                        "subject": [
                            {"name": image.image, "digest": {"sha256": "a" * 64}}
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
    (candidate / "resolved.yaml").unlink()
    digest_file = tmp_path / "digests.json"
    digest_file.write_text(json.dumps(digests), encoding="utf-8")
    private = Ed25519PrivateKey.generate()
    key_file = tmp_path / "release.pem"
    key_file.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    site = build_catalog(
        source_root=candidate,
        output_root=tmp_path / "site",
        digest_file=digest_file,
        private_key_file=key_file,
        key_id="berylliumsec.test",
        generated_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        base_url="https://catalog.example/tool-packs/",
    )

    keys = json.loads((site / "berylliumsec-tool-pack-keys.json").read_text())["keys"]
    catalog = ToolCatalogV1.model_validate_json((site / "catalog-v1.json").read_bytes())
    signature = SignatureEnvelope.model_validate_json(
        (site / "catalog-v1.json.signature.json").read_bytes()
    )
    Ed25519Keyring(keys).verify_publisher(
        canonical_catalog_json(catalog), signature, "berylliumsec"
    )
    assert len(catalog.entries) == 4
    assert {entry.collection_id for entry in catalog.entries} == {"safe-foundation"}
    assert len(list((site / "bundles").glob("*.nebula-toolpack"))) == 4
