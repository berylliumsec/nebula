"""Command receipts preview large output as a marked head and tail."""

from __future__ import annotations

import inspect
import json

from nebula.v3.artifacts import ArtifactStore
from nebula.v3.tool_failures import with_failed_result_receipt
from nebula.v3.tool_results import (
    DEFAULT_TOOL_OUTPUT_READ_LINES,
    MAX_EXCERPT_BYTES,
    MAX_TOOL_OUTPUT_READ_LINES,
    ToolOutputPreview,
    ToolOutputService,
    ToolResultReceipt,
    ToolResultStatus,
    artifact_ref,
    command_output_preview,
    model_result_bytes,
)


def _streams(tmp_path, **outputs: bytes):
    store = ArtifactStore(tmp_path / "artifacts")
    streams = []
    for kind, data in outputs.items():
        artifact = store.put_bytes(
            data, engagement_id="project", filename=kind, media_type="text/plain"
        )
        streams.append((artifact, artifact_ref(artifact, kind=kind)))
    return store, streams


def _receipt(preview: list[ToolOutputPreview]) -> ToolResultReceipt:
    return ToolResultReceipt(
        tool_call_id="call",
        tool_name="run_command",
        tool_version="1",
        status=ToolResultStatus.COMPLETED,
        exit_code=0,
        output_preview=preview,
    )


def _preview(tmp_path, **outputs: bytes) -> list[ToolOutputPreview]:
    store, streams = _streams(tmp_path, **outputs)
    return command_output_preview(
        store, streams, lambda candidate: _receipt(candidate).as_model_result()
    )


def _numbered(count: int, *, width: int = 60) -> bytes:
    return b"".join(
        f"line {number:05d} ".ljust(width, "x").encode() + b"\n"
        for number in range(1, count + 1)
    )


def test_small_output_stays_complete_with_the_old_receipt_shape(tmp_path):
    preview = _preview(tmp_path, stdout=b"ok: 3 items\n", stderr=b"warning\n")

    assert [item.text for item in preview] == ["ok: 3 items\n", "warning\n"]
    assert not any(item.partial for item in preview)
    dumped = _receipt(preview).as_model_result()["output_preview"]
    assert dumped == [
        {
            "artifact_id": preview[0].artifact_id,
            "kind": "stdout",
            "text": "ok: 3 items\n",
        },
        {"artifact_id": preview[1].artifact_id, "kind": "stderr", "text": "warning\n"},
    ]


def test_large_output_shows_marked_head_and_tail_within_the_bound(tmp_path):
    data = _numbered(2_000)
    (preview,) = _preview(tmp_path, stdout=data)

    assert preview.partial is True
    assert preview.total_lines == 2_000
    assert preview.total_bytes == len(data)
    assert preview.text.startswith("line 00001 ")
    assert preview.text.endswith("line 02000 " + "x" * 49 + "\n")
    omitted = preview.omitted
    assert omitted is not None
    head_lines = preview.text.split("[...")[0].count("\n")
    tail_lines = preview.text.split("...]\n")[1].count("\n")
    assert omitted.start_line == head_lines + 1
    assert omitted.end_line == 2_000 - tail_lines
    assert omitted.byte_count == (omitted.end_line - omitted.start_line + 1) * 61
    assert (
        f"lines {omitted.start_line}-{omitted.end_line} of 2000; read them with "
        f"tool_output.read starting_line={omitted.start_line}"
    ) in preview.text
    receipt = _receipt([preview]).as_model_result()
    assert receipt["output_preview"][0]["partial"] is True
    assert receipt["output_preview"][0]["omitted"]["start_line"] == omitted.start_line
    # The preview uses most of the model-delivery bound without exceeding it.
    assert 6 * 1024 < model_result_bytes(receipt) <= MAX_EXCERPT_BYTES


def test_partial_receipt_round_trips_and_keeps_read_tools(tmp_path):
    (preview,) = _preview(tmp_path, stdout=_numbered(500))
    receipt = _receipt([preview])
    again = ToolResultReceipt.model_validate(receipt.as_model_result())
    assert again.output_preview[0] == preview


def test_both_streams_share_the_bound_and_a_small_one_stays_whole(tmp_path):
    preview = _preview(tmp_path, stdout=_numbered(3_000), stderr=b"error: boom\n")

    assert [item.kind for item in preview] == ["stdout", "stderr"]
    assert preview[0].partial and not preview[1].partial
    assert preview[1].text == "error: boom\n"
    assert model_result_bytes(_receipt(preview).as_model_result()) <= MAX_EXCERPT_BYTES

    both = _preview(tmp_path, stdout=_numbered(3_000), stderr=_numbered(3_000))
    assert all(item.partial for item in both)
    assert model_result_bytes(_receipt(both).as_model_result()) <= MAX_EXCERPT_BYTES


def test_partial_preview_redacts_secrets_at_head_and_tail(tmp_path):
    secret = "ghp_" + "A" * 36
    data = (
        f"Authorization: Bearer {'b' * 40}\ntoken={secret}\n".encode()
        + _numbered(2_000)
        + f"password=hunter2hunter2\nexport GH={secret}\n".encode()
    )
    (preview,) = _preview(tmp_path, stdout=data)

    assert preview.partial
    assert secret not in preview.text
    assert "b" * 40 not in preview.text
    assert "hunter2hunter2" not in preview.text
    assert "Bearer [REDACTED]" in preview.text
    assert preview.text.endswith("export GH=[REDACTED TOKEN]\n")


def test_partial_preview_never_cuts_into_a_private_key(tmp_path):
    key = (
        "-----BEGIN PRIVATE KEY-----\n"
        + "".join(f"MIIEv{n:03d}{'Q' * 56}\n" for n in range(40))
        + "-----END PRIVATE KEY-----\n"
    ).encode()
    # A key at the very start of a large stream, then one at its end.
    (preview,) = _preview(tmp_path, stdout=key + _numbered(2_000) + key)

    assert "MIIEv" not in preview.text
    assert "PRIVATE KEY-----" not in preview.text or "[REDACTED PRIVATE KEY]" in (
        preview.text
    )
    # A key whose body would straddle the head's cut is withheld, not cut.
    straddling = _numbered(40) + key * 3 + _numbered(2_000)
    store, streams = _streams(tmp_path / "straddle", stdout=straddling)
    (preview,) = command_output_preview(
        store, streams, lambda candidate: _receipt(candidate).as_model_result()
    )
    assert "MIIEv" not in preview.text


def test_one_huge_line_shows_its_redacted_ends(tmp_path):
    data = b"[" + b'"value",' * 40_000 + b'"token=' + b"s" * 30 + b'"]'
    (preview,) = _preview(tmp_path, stdout=data)

    assert preview.partial
    assert preview.text.startswith('["value",')
    assert preview.text.endswith('"token=[REDACTED]"]')
    assert preview.total_lines == 1
    assert preview.omitted is not None
    assert (preview.omitted.start_line, preview.omitted.end_line) == (1, 1)
    assert preview.omitted.byte_count == len(data)


def test_binary_or_truncated_output_stays_artifact_only(tmp_path):
    assert _preview(tmp_path, stdout=b"\x00" * 10_000) == []
    store, streams = _streams(tmp_path / "t", stdout=_numbered(2_000))
    artifact, ref = streams[0]
    truncated = [(artifact, ref.model_copy(update={"truncated": True}))]
    assert (
        command_output_preview(
            store, truncated, lambda c: _receipt(c).as_model_result()
        )
        == []
    )


def test_failure_context_keeps_the_tail_of_a_partial_preview(tmp_path):
    (preview,) = _preview(tmp_path, stdout=_numbered(2_000) + b"fatal: final error\n")
    failure = {"category": "execution_failed", "side_effects": "none"}
    result = with_failed_result_receipt(failure, _receipt([preview]).as_model_result())
    item = result["result_receipt"]["output_preview"][0]
    assert item["partial"] is True
    assert item["text"].endswith("fatal: final error\n")


def test_tool_output_read_defaults_to_a_larger_page(tmp_path):
    default = inspect.signature(ToolOutputService.read).parameters["line_count"]
    assert default.default == DEFAULT_TOOL_OUTPUT_READ_LINES == 400
    assert MAX_TOOL_OUTPUT_READ_LINES == 400
    store, streams = _streams(
        tmp_path, stdout=b"".join(b"%d\n" % n for n in range(1, 1_001))
    )
    service = ToolOutputService(store=None, artifact_store=store)  # type: ignore[arg-type]
    page = service._read_artifact(
        streams[0][0],
        starting_line=1,
        line_count=DEFAULT_TOOL_OUTPUT_READ_LINES,
        continuation_cursor=None,
    )
    # Short lines now page past the old 200-line maximum; the 8 KiB bound
    # still ends the page first.
    assert 200 < len(page["lines"]) < 400
    assert page["continuation"] == {"starting_line": len(page["lines"]) + 1}
    assert len(json.dumps(page).encode()) <= MAX_EXCERPT_BYTES
    # Long lines are still bounded by bytes, not lines.
    store, streams = _streams(tmp_path / "wide", stdout=_numbered(1_000, width=200))
    service = ToolOutputService(store=None, artifact_store=store)  # type: ignore[arg-type]
    page = service._read_artifact(
        streams[0][0],
        starting_line=1,
        line_count=DEFAULT_TOOL_OUTPUT_READ_LINES,
        continuation_cursor=None,
    )
    assert 30 < len(page["lines"]) < 400
    assert model_result_bytes(page) <= MAX_EXCERPT_BYTES
