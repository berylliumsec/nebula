import re
import zipfile
from pathlib import Path

import pytest

from nebula.v3.toolpack_sdk import (
    ToolPackSDKError,
    init_tool_pack,
    pack_tool_pack,
    read_tool_pack,
    validate_tool_pack_directory,
)


def resolve_template_digests(path):
    source = path.read_text(encoding="utf-8")
    source = re.sub(r"\{\{sha256:[a-z0-9._-]+\}\}", "sha256:" + "a" * 64, source)
    path.write_text(source, encoding="utf-8")


def test_sdk_initializes_valid_source_but_refuses_unresolved_release_pack(tmp_path):
    root = init_tool_pack(tmp_path / "sample", name="sample", publisher="acme")
    manifest = validate_tool_pack_directory(root)

    assert manifest.identity == "acme/sample@0.1.0"
    assert (root / "Containerfile").is_file()
    assert "ENTRYPOINT" not in (root / "Containerfile").read_text(encoding="utf-8")
    assert (root / "tests/parser-fixtures/output.json").is_file()
    with pytest.raises(ToolPackSDKError, match="unresolved"):
        pack_tool_pack(root, tmp_path / "sample.ntp")
    with pytest.raises(ToolPackSDKError, match="empty"):
        init_tool_pack(root, name="again", publisher="acme")
    with pytest.raises(ToolPackSDKError, match="canonical"):
        init_tool_pack(tmp_path / "invalid", name="bad\nname", publisher="acme")


def test_sdk_creates_deterministic_safe_archive_and_reads_it(tmp_path):
    first = init_tool_pack(tmp_path / "one", name="sample", publisher="acme")
    second = init_tool_pack(tmp_path / "two", name="sample", publisher="acme")
    for root in (first, second):
        resolve_template_digests(root / "nebula-tool-pack.yaml")

    archive_one = pack_tool_pack(first, tmp_path / "one.ntp")
    archive_two = pack_tool_pack(second, tmp_path / "two.ntp")
    assert archive_one.read_bytes() == archive_two.read_bytes()
    loaded = read_tool_pack(archive_one)
    assert loaded.manifest.identity == "acme/sample@0.1.0"
    assert "source/Containerfile" in loaded.files
    with pytest.raises(ToolPackSDKError, match="overwrite"):
        pack_tool_pack(first, archive_one)


def test_sdk_rejects_symlinks_and_archive_path_traversal(tmp_path):
    root = init_tool_pack(tmp_path / "sample", name="sample", publisher="acme")
    (root / "escape").symlink_to(tmp_path / "outside")
    with pytest.raises(ToolPackSDKError, match="symlinks"):
        validate_tool_pack_directory(root)

    bad = tmp_path / "bad.ntp"
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../manifest.json", "{}")
    with pytest.raises(ToolPackSDKError, match="unsafe"):
        read_tool_pack(bad)


def test_sdk_rejects_mismatched_source_manifest_and_archive_limits(
    tmp_path, monkeypatch
):
    root = init_tool_pack(tmp_path / "source", name="sample", publisher="acme")
    resolve_template_digests(root / "nebula-tool-pack.yaml")
    original = pack_tool_pack(root, tmp_path / "original.ntp")
    mismatched = tmp_path / "mismatched.ntp"
    with (
        zipfile.ZipFile(original) as source,
        zipfile.ZipFile(mismatched, "w") as destination,
    ):
        for member in source.infolist():
            payload = source.read(member)
            if member.filename == "source/nebula-tool-pack.yaml":
                payload = payload.replace(b"version: 0.1.0", b"version: 0.2.0")
            destination.writestr(member.filename, payload)
    with pytest.raises(ToolPackSDKError, match="do not match"):
        read_tool_pack(mismatched)

    monkeypatch.setattr("nebula.v3.toolpack_sdk.MAX_PACK_ARCHIVE_MEMBERS", 1)
    with pytest.raises(ToolPackSDKError, match="too many members"):
        read_tool_pack(original)


def test_toolbox_source_is_structurally_valid_but_unresolved():
    assets = Path(__file__).parents[2] / "src/nebula/v3/tool_pack_assets/toolbox"
    identities = {
        validate_tool_pack_directory(path.parent).identity
        for path in assets.glob("*/nebula-tool-pack.yaml")
    }
    assert identities == {
        "berylliumsec/nebula-toolbox@0.1.2",
    }
    for path in assets.glob("*/nebula-tool-pack.yaml"):
        with pytest.raises(ToolPackSDKError, match="unresolved"):
            validate_tool_pack_directory(path.parent, allow_digest_placeholders=False)
