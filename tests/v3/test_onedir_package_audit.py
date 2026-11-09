"""The runtime audit combines embedded modules and external pinned data."""

import pytest

from scripts import package_audit


def test_onedir_audit_requires_data_outside_the_executable(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "nebula-runtime").write_bytes(b"inert executable")
    embedded = set()
    for alternatives in package_audit.REQUIRED_MEMBERS:
        name = alternatives[0]
        if "/" in name:
            path = runtime / "_internal" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"pinned data")
        else:
            embedded.add(name)
    monkeypatch.setattr(
        package_audit, "_pyinstaller_members", lambda _path: embedded.copy()
    )
    result = package_audit.inspect_pyinstaller_directory(runtime, "nebula-runtime")
    assert result["status"] == "ok"
    assert result["format"] == "pyinstaller-onedir"
    (runtime / "_internal/nebula/v3/runtime-compatibility.json").unlink()
    with pytest.raises(package_audit.ArtifactAuditError, match="runtime-compatibility"):
        package_audit.inspect_pyinstaller_directory(runtime, "nebula-runtime")
