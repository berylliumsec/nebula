import hashlib
import json

import pytest

from scripts.verify_stabilization_build import verify_candidate


@pytest.mark.parametrize(
    "defect", [None, "dirty", "mixed", "changed", "extra", "timestamp"]
)
def test_candidate_requires_exact_clean_artifacts(tmp_path, defect):
    web = tmp_path / "web"
    web.mkdir()
    for name in ("index.html", "sw.js"):
        (web / name).write_text(name)
    manifest = {
        "schema": "nebula.web-build/v1",
        "commit": "a" * 40,
        "dirty": defect == "dirty",
        "builtAt": "fixture-time",
        "assets": {
            name: hashlib.sha256(name.encode()).hexdigest()
            for name in ("index.html", "sw.js")
        },
    }
    (web / "web-build.json").write_text(json.dumps(manifest))
    identity = tmp_path / "core.json"
    identity.write_text(
        json.dumps(
            {
                "commit": ("b" if defect == "mixed" else "a") * 40,
                "build_timestamp": "other" if defect == "timestamp" else "fixture-time",
            }
        )
    )
    if defect == "changed":
        (web / "sw.js").write_text("stale worker")
    if defect == "extra":
        (web / "stale.js").write_text("old bundle")
    if defect:
        with pytest.raises(ValueError):
            verify_candidate(web, identity, "a" * 40)
    else:
        assert verify_candidate(web, identity, "a" * 40)["web_assets"] == 2
