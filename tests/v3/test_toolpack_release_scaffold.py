import json
import re
import shutil
from pathlib import Path

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
