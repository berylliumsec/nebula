import hashlib
import json

from scripts import generate_release_sbom


def test_release_sboms_include_components_and_artifact_hash(tmp_path, monkeypatch):
    artifact = tmp_path / "Nebula.AppImage"
    artifact.write_bytes(b"packaged-nebula")
    (tmp_path / "NEBULA3_VERSION").write_text("3.1.0\n", encoding="utf-8")
    inventory = [
        {
            "ecosystem": "pypi",
            "name": f"dependency-{index}",
            "version": "1.0.0",
            "license": "MIT",
        }
        for index in range(12)
    ]
    monkeypatch.setattr(
        generate_release_sbom,
        "component_inventory",
        lambda _root, _target, *, direct: inventory,
    )

    cyclonedx_path, spdx_path, count = generate_release_sbom.write_sboms(
        artifact=artifact,
        root=tmp_path,
        target="x86_64-unknown-linux-gnu",
        direct=True,
    )

    cyclonedx = json.loads(cyclonedx_path.read_text(encoding="utf-8"))
    spdx = json.loads(spdx_path.read_text(encoding="utf-8"))
    assert count == 12
    assert len(cyclonedx["components"]) == 12
    assert len(spdx["packages"]) == 12
    assert cyclonedx["metadata"]["component"]["hashes"] == [
        {
            "alg": "SHA-256",
            "content": hashlib.sha256(b"packaged-nebula").hexdigest(),
        }
    ]
