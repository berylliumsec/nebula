import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest
import tomli

from scripts.nebula3_version import (
    VersionSyncError,
    check_versions,
    read_versions,
    set_version,
)
from scripts.build_nebula_core import macos_codesign_arguments
from scripts.build_nebula_core import stage_runtime_payload
from scripts.package_audit import (
    ArtifactAuditError,
    FORBIDDEN_MODULES,
    inspect_installer_tree,
    validate_members,
)


ROOT = Path(__file__).resolve().parents[2]
CURRENT_VERSION = (ROOT / "NEBULA3_VERSION").read_text(encoding="utf-8").strip()


def test_nebula3_version_is_synchronized_across_every_package():
    assert check_versions(ROOT, expected=CURRENT_VERSION) == CURRENT_VERSION


def test_native_launcher_and_admin_command_contract():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        scripts = tomli.load(stream)["tool"]["poetry"]["scripts"]
    assert scripts == {"nebula-core": "nebula.v3.cli:main"}

    linux_launcher = (ROOT / "packaging/linux/nebula").read_text(encoding="utf-8")
    assert 'exec /usr/bin/nebula-ui "$@"' in linux_launcher
    assert "exec /usr/bin/nebula-core" not in linux_launcher

    assert not (ROOT / "packaging/homebrew/nebula.rb.in").exists()


def test_protected_release_distribution_is_linux_x86_64_only():
    paths = [
        ROOT / ".github/workflows/nebula3-release.yml",
        ROOT / ".github/workflows/nebula3-release-finalize.yml",
        ROOT / ".github/workflows/publish-updater-manifest.yml",
    ]
    workflows = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "Nebula-$VERSION-linux-x86_64.AppImage" in workflows
    assert "Nebula-$VERSION-linux-x86_64.deb" in workflows
    for unsupported in (
        "APPLE_",
        "darwin-aarch64",
        "macOS-arm64",
        "macos-15",
        "notarytool",
    ):
        assert unsupported not in workflows


def test_python_source_tree_contains_only_core_namespace():
    package_root = ROOT / "src/nebula"
    unexpected = sorted(
        path.name
        for path in package_root.iterdir()
        if path.name not in {"__init__.py", "__pycache__", "v3"}
    )
    assert unexpected == []


def test_nebula2_publish_workflow_is_removed():
    assert not (ROOT / ".github/workflows/publish.yml").exists()


def test_version_cli_supports_release_check_contract():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/nebula3_version.py",
            "check",
            "--expected",
            CURRENT_VERSION,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == CURRENT_VERSION


def test_core_build_script_supports_direct_path_imports(tmp_path):
    script = ROOT / "scripts" / "build_nebula_core.py"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import runpy; "
                f"runpy.run_path({str(script)!r}, run_name='build_import_check')"
            ),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _version_fixture(root: Path) -> None:
    (root / "src/nebula/v3").mkdir(parents=True)
    (root / "ui/src-tauri").mkdir(parents=True)
    (root / "NEBULA3_VERSION").write_text("3.0.0-alpha.1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "nebula-ai"\nversion = "3.0.0-alpha.1"\n',
        encoding="utf-8",
    )
    (root / "src/nebula/v3/version.py").write_text(
        '__version__ = "3.0.0-alpha.1"\n', encoding="utf-8"
    )
    (root / "ui/src-tauri/tauri.conf.json").write_text(
        json.dumps({"version": "3.0.0-alpha.1"}), encoding="utf-8"
    )
    (root / "ui/src-tauri/Cargo.toml").write_text(
        '[package]\nname = "nebula-ui"\nversion = "3.0.0-alpha.1"\n',
        encoding="utf-8",
    )
    (root / "ui/src-tauri/Cargo.lock").write_text(
        'version = 4\n\n[[package]]\nname = "a-dependency"\nversion = "1.0.0"\n\n'
        '[[package]]\nname = "nebula-ui"\nversion = "3.0.0-alpha.1"\n',
        encoding="utf-8",
    )
    (root / "ui/package.json").write_text(
        json.dumps({"name": "nebula-ui", "version": "3.0.0-alpha.1"}),
        encoding="utf-8",
    )
    (root / "ui/package-lock.json").write_text(
        json.dumps(
            {
                "name": "nebula-ui",
                "version": "3.0.0-alpha.1",
                "packages": {"": {"name": "nebula-ui", "version": "3.0.0-alpha.1"}},
            }
        ),
        encoding="utf-8",
    )


def test_set_version_updates_every_nebula3_source(tmp_path):
    _version_fixture(tmp_path)
    set_version(tmp_path, "3.1.0-rc.2")
    assert set(read_versions(tmp_path).values()) == {"3.1.0-rc.2"}


def test_version_check_reports_all_mismatched_sources(tmp_path):
    _version_fixture(tmp_path)
    package = tmp_path / "ui/package.json"
    package.write_text(
        json.dumps({"name": "nebula-ui", "version": "3.0.0-alpha.2"}),
        encoding="utf-8",
    )
    with pytest.raises(VersionSyncError, match="package.json='3.0.0-alpha.2'"):
        check_versions(tmp_path)


def test_runtime_dependency_boundary_rejects_removed_stacks():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomli.load(stream)["tool"]["poetry"]
    runtime = {name.lower() for name in project["dependencies"]}
    assert {
        "fastapi",
        "sqlalchemy",
        "langgraph",
        "regex",
        "boto3",
        "pypdf",
        "reportlab",
        "pillow",
    } <= runtime
    assert not ({"pyqt6", "torch", "transformers", "chromadb"} & runtime)
    assert "legacy" not in project.get("group", {})
    assert "legacy-dev" not in project.get("group", {})
    assert "pytest-qt" not in {
        name.lower() for name in project["group"]["dev"]["dependencies"]
    }
    assert "regex" not in {name.lower() for name in FORBIDDEN_MODULES}


def test_core_archive_rejects_nltk_and_its_pyinstaller_runtime_hook():
    for member in ("nltk", "nltk/tokenize/casual.py", "pyi_rth_nltk.py"):
        with pytest.raises(ArtifactAuditError, match="forbidden members"):
            validate_members([member])


def test_core_archive_requires_timeout_capable_regex_runtime():
    with pytest.raises(ArtifactAuditError, match="required members absent: regex"):
        validate_members(["nebula.v3.cli", "nebula.v3.mcp_gateway"])


def test_nebula3_runtime_has_no_conditional_import_fallbacks():
    conditional_imports = []
    for path in sorted((ROOT / "src/nebula/v3").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for statement in node.body:
                if any(
                    isinstance(candidate, (ast.Import, ast.ImportFrom))
                    for candidate in ast.walk(statement)
                ):
                    conditional_imports.append(path.name)
                    break
    assert conditional_imports == []


def test_macos_release_core_requires_embedded_binary_signing():
    with pytest.raises(RuntimeError, match="APPLE_SIGNING_IDENTITY"):
        macos_codesign_arguments(
            platform="darwin", distribution="direct", identity=None
        )
    assert macos_codesign_arguments(
        platform="darwin",
        distribution="managed",
        identity="Developer ID Application: Nebula",
    ) == ["--codesign-identity", "Developer ID Application: Nebula"]
    assert (
        macos_codesign_arguments(
            platform="linux", distribution="managed", identity=None
        )
        == []
    )


def test_core_payload_staging_excludes_caches_and_source_maps(tmp_path):
    migrations = tmp_path / "src/nebula/v3/migrations"
    frontend = tmp_path / "ui/dist/assets"
    migrations.mkdir(parents=True)
    frontend.mkdir(parents=True)
    (migrations / "env.py").write_text("revision = 1\n", encoding="utf-8")
    cache = migrations / "__pycache__"
    cache.mkdir()
    (cache / "env.cpython-312.pyc").write_bytes(b"cache")
    (tmp_path / "ui/dist/index.html").write_text("<main></main>", encoding="utf-8")
    (frontend / "app.js").write_text("export {};", encoding="utf-8")
    (frontend / "app.js.map").write_text("{}", encoding="utf-8")

    staged_migrations, staged_frontend = stage_runtime_payload(tmp_path)

    assert (staged_migrations / "env.py").is_file()
    assert not (staged_migrations / "__pycache__").exists()
    assert (staged_frontend / "assets/app.js").is_file()
    assert not (staged_frontend / "assets/app.js.map").exists()


def test_tauri_dev_hook_builds_core_and_required_resources_before_vite():
    tauri = json.loads(
        (ROOT / "ui/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )
    package = json.loads((ROOT / "ui/package.json").read_text(encoding="utf-8"))

    assert tauri["build"]["beforeDevCommand"] == "npm run dev:desktop"
    assert package["scripts"]["dev:desktop"] == (
        "npm run build && npm run build:core && npm run dev"
    )
    assert package["scripts"]["build:core"] == (
        "poetry -C .. run python -m scripts.build_nebula_core"
    )
    assert tauri["bundle"]["externalBin"] == ["binaries/nebula-core"]
    assert (
        "../../build/nebula-core-metadata/THIRD_PARTY_NOTICES.txt"
        in tauri["bundle"]["resources"]
    )


def test_artifact_member_audit_accepts_only_complete_v3_payload():
    result = validate_members(
        {
            "nebula.v3.cli",
            "nebula.v3.mcp_gateway",
            "regex",
            "reportlab",
            "PIL",
            "nebula/v3/BUILD_INFO.json",
            "nebula/v3/migrations/script.py.mako",
            "nebula/v3/kali_tool_inventory.py",
            "nebula/v3/operator_help.md",
            "nebula/v3/diagnostic_guidance.json",
            "nebula/v3/report_assets/fonts/NotoSans-Regular.ttf",
            "nebula/v3/report_assets/fonts/NotoSans-Bold.ttf",
            "nebula/v3/report_assets/fonts/NotoSansMono-Regular.ttf",
            "nebula/v3/report_assets/fonts/NotoSansMono-Bold.ttf",
            "nebula/v3/report_assets/fonts/OFL.txt",
            "ui/dist/index.html",
            "licenses/LICENSE.md",
            "licenses/THIRD_PARTY_NOTICES.txt",
        }
    )
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    "forbidden", ["PyQt6.QtCore", "PySide6.QtCore", "torch", "_pytest.outcomes"]
)
def test_artifact_member_audit_rejects_gui_or_heavy_payload(forbidden):
    with pytest.raises(ArtifactAuditError, match="forbidden members"):
        validate_members(
            {
                "nebula.v3.cli",
                "nebula/v3/BUILD_INFO.json",
                "nebula/v3/migrations/script.py.mako",
                "ui/dist/index.html",
                "licenses/LICENSE.md",
                "licenses/THIRD_PARTY_NOTICES.txt",
                forbidden,
            }
        )


def test_artifact_member_audit_fails_when_legal_payload_is_absent():
    with pytest.raises(ArtifactAuditError, match="THIRD_PARTY_NOTICES"):
        validate_members(
            {
                "nebula.v3.cli",
                "nebula/v3/BUILD_INFO.json",
                "nebula/v3/migrations/script.py.mako",
                "ui/dist/index.html",
                "licenses/LICENSE.md",
            }
        )


def test_artifact_member_audit_rejects_build_residue():
    with pytest.raises(ArtifactAuditError, match="build residue"):
        validate_members(
            {
                "nebula.v3.cli",
                "nebula/v3/BUILD_INFO.json",
                "nebula/v3/migrations/script.py.mako",
                "nebula/v3/migrations/__pycache__/env.cpython-312.pyc",
                "ui/dist/index.html",
                "licenses/LICENSE.md",
                "licenses/THIRD_PARTY_NOTICES.txt",
            }
        )


def test_installer_tree_gate_requires_legal_files_and_rejects_toolchains(tmp_path):
    binary = tmp_path / "usr/bin"
    legal = tmp_path / "usr/share/doc/nebula"
    binary.mkdir(parents=True)
    legal.mkdir(parents=True)
    for name in ("nebula-ui", "nebula-core"):
        (binary / name).write_bytes(b"binary")
    (legal / "LICENSE").write_text("BSD\n", encoding="utf-8")
    (legal / "THIRD_PARTY_NOTICES.txt").write_text("Notices\n", encoding="utf-8")
    assert inspect_installer_tree(tmp_path)["status"] == "ok"

    (binary / "python3").write_bytes(b"toolchain")
    with pytest.raises(ArtifactAuditError, match="forbidden installer content"):
        inspect_installer_tree(tmp_path)
