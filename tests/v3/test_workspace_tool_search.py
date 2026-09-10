import os
from pathlib import Path

import pytest

from nebula.v3.tool_results import WorkspaceOutputService, ToolOutputAccessError


def test_literal_regex_file_directory_and_sparse_large_workspace(tmp_path):
    for i in range(1200):
        (tmp_path / f"{i:04}.txt").write_text("ordinary line\n" * 8)
    (tmp_path / "1199.txt").write_text("needle value\n")
    service = WorkspaceOutputService(tmp_path, regex_seconds=0)
    result = service.search(query="needle")
    assert result["status"] == "complete"
    assert result["matches"][0]["path"] == "1199.txt"
    assert service.search(query="absent")["matches"] == []
    assert service.search(query="needle", path="1199.txt")["status"] == "complete"
    regex = WorkspaceOutputService(tmp_path).search(query=r"needle \w+", mode="regex")
    assert regex["matches"] == result["matches"]


def test_budget_exhaustion_preserves_results_and_excludes_generated_content(tmp_path):
    (tmp_path / "a.txt").write_text("needle\n")
    (tmp_path / "b.txt").write_text("x" * 10000)
    generated = tmp_path / "node_modules"
    generated.mkdir()
    (generated / "large.txt").write_text("needle\n" * 10000)
    partial = WorkspaceOutputService(tmp_path, max_bytes=64).search(query="needle")
    assert partial["incomplete"] and partial["matches"]
    assert "byte budget" in partial["reason"]
    assert "narrow" in partial["guidance"]
    complete = WorkspaceOutputService(tmp_path).search(query="needle")
    assert complete["status"] == "complete"
    assert len(complete["matches"]) == 1
    assert complete["skipped_directories"] == 1
    assert WorkspaceOutputService(tmp_path).search(query="needle", path="node_modules")[
        "matches"
    ]
    for options, reason in [
        ({"search_seconds": 0}, "time budget"),
        ({"max_files": 1}, "file budget"),
    ]:
        result = WorkspaceOutputService(tmp_path, **options).search(query="absent")
        assert result["status"] == "incomplete"
        assert reason in result["reason"]
    timeout = WorkspaceOutputService(tmp_path, regex_seconds=0).search(
        query="needle", mode="regex"
    )
    assert timeout["incomplete"] and "regular-expression" in timeout["reason"]


def test_search_pagination_missing_permissions_and_path_boundaries(
    tmp_path, monkeypatch
):
    (tmp_path / "a.txt").write_text("needle\n" * 3)
    service = WorkspaceOutputService(tmp_path)
    first = service.search(query="needle", match_limit=1)
    second = service.search(
        query="needle", match_limit=1, cursor=first["continuation_cursor"]
    )
    assert first["matches"][0]["line"] == 1
    assert second["matches"][0]["line"] == 2
    for path, message in [
        ("missing", "does not exist"),
        ("../outside", "rejected"),
        (str(tmp_path), "relative"),
    ]:
        with pytest.raises(ToolOutputAccessError, match=message):
            service.search(query="x", path=path)
    (tmp_path / "link").symlink_to(tmp_path / "a.txt")
    with pytest.raises(ToolOutputAccessError, match="symbolic"):
        service.search(query="needle", path="link")
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(ToolOutputAccessError, match="unsupported"):
        service.search(query="needle", path="fifo")
    original = Path.open

    def denied(path, *args, **kwargs):
        if path.name == "private.txt":
            raise PermissionError(13, "Permission denied", str(path))
        return original(path, *args, **kwargs)

    (tmp_path / "private.txt").write_text("secret")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(ToolOutputAccessError, match="inaccessible"):
        service.search(query="needle", path="private.txt")
    partial = service.search(query="needle")
    assert partial["unreadable_count"] == 1
    assert partial["incomplete"] and len(partial["matches"]) == 3
