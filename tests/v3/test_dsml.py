"""Reading DSML tool calls that arrived as assistant content."""

from nebula.v3.dsml import MAX_FRAME_CHARACTERS, recover
from nebula.v3.providers import ModelResponse, ToolCall


def _frame(body: str) -> str:
    return f"<｜DSML｜ calls>{body}</｜DSML｜ calls>"


def _invoke(name: str, body: str = "") -> str:
    return f'<｜DSML｜ invoke name="{name}">{body}</｜DSML｜ invoke>'


def _parameter(name: str, value: str) -> str:
    return f'<｜DSML｜ parameter name="{name}">{value}</｜DSML｜ parameter>'


def test_a_frame_becomes_the_call_it_describes():
    found = recover(_frame(_invoke("read_file", _parameter("path", "notes.md"))))

    assert [(call.name, call.arguments) for call in found.calls] == [
        ("read_file", {"path": "notes.md"})
    ]
    assert found.text == ""
    assert found.unparsed_frames == 0


def test_the_protocol_is_read_with_either_pipe_and_through_json_escaping():
    """Routes transcode U+FF5C to ASCII and escape closing tags; both parse."""

    # JSON escapes the slash; some routes escape the whole closing tag.
    slash_escaped = (
        '<|DSML| calls><|DSML| invoke name="read_file">'
        '<|DSML| parameter name="path">notes.md<\\/|DSML| parameter>'
        "<\\/|DSML| invoke><\\/|DSML| calls>"
    )
    tag_escaped = slash_escaped.replace("<\\/", "\\</")

    for frame in (slash_escaped, tag_escaped):
        found = recover(frame)
        assert [call.arguments for call in found.calls] == [{"path": "notes.md"}], frame


def test_prose_around_a_frame_survives_the_call_it_carried():
    text = f"Reading the notes first.\n\n{_frame(_invoke('read_file'))}\n\nThen I will summarise."

    found = recover(text)

    assert [call.name for call in found.calls] == ["read_file"]
    assert found.text == "Reading the notes first.\n\n\n\nThen I will summarise."


def test_every_invoke_in_every_frame_is_recovered_in_order():
    text = _frame(
        _invoke("read_file", _parameter("path", "a.md"))
        + _invoke("read_file", _parameter("path", "b.md"))
    ) + _frame(_invoke("search_files", _parameter("query", "auth")))

    found = recover(text)

    assert [call.name for call in found.calls] == [
        "read_file",
        "read_file",
        "search_files",
    ]
    assert [call.arguments for call in found.calls] == [
        {"path": "a.md"},
        {"path": "b.md"},
        {"query": "auth"},
    ]


def test_a_structured_argument_arrives_as_the_value_it_stands_for():
    """DSML carries every value as text; JSON source becomes the value."""

    found = recover(
        _frame(
            _invoke(
                "write_config",
                _parameter("options", '{"retries": 2, "hosts": ["a", "b"]}')
                + _parameter("count", "3")
                + _parameter("ratio", "-1.5e3")
                + _parameter("enabled", "true")
                + _parameter("previous", "null"),
            )
        )
    )

    assert found.calls[0].arguments == {
        "options": {"retries": 2, "hosts": ["a", "b"]},
        "count": 3,
        "ratio": -1500.0,
        "enabled": True,
        "previous": None,
    }


def test_text_that_only_resembles_json_stays_exactly_what_the_model_wrote():
    found = recover(
        _frame(
            _invoke(
                "run_query",
                _parameter("id", "artifact-a")
                + _parameter("zip", "007")
                + _parameter("expression", "2 + 2")
                + _parameter("broken", "{not json")
                + _parameter("empty", ""),
            )
        )
    )

    assert found.calls[0].arguments == {
        "id": "artifact-a",
        "zip": "007",
        "expression": "2 + 2",
        "broken": "{not json",
        "empty": "",
    }


def test_a_value_keeps_its_own_shape_when_the_frame_was_pretty_printed():
    found = recover(
        "<｜DSML｜ calls>\n"
        '  <｜DSML｜ invoke name="write_file">\n'
        '    <｜DSML｜ parameter name="body">line one\nline two</｜DSML｜ parameter>\n'
        "  </｜DSML｜ invoke>\n"
        "</｜DSML｜ calls>"
    )

    assert found.calls[0].arguments == {"body": "line one\nline two"}


def test_a_frame_core_cannot_read_completely_is_left_where_it_was():
    """Half a call is not a call, so the caller's quarantine still sees it."""

    unreadable = [
        # Nothing to run.
        _frame(""),
        # Prose the model wrote inside the call it was making.
        _frame(_invoke("read_file", "the file I mentioned")),
        # Prose between two calls.
        _frame(_invoke("read_file") + "and then" + _invoke("read_file")),
        # Ambiguous: two values for one argument.
        _frame(_invoke("read_file", _parameter("path", "a") + _parameter("path", "b"))),
        # An identity no tool can have.
        _frame(_invoke("Read_File", _parameter("path", "a.md"))),
        _frame(_invoke("", _parameter("path", "a.md"))),
        # An argument with no name to fill.
        _frame(_invoke("read_file", _parameter("", "a.md"))),
        # A frame that never closed.
        '<｜DSML｜ calls><｜DSML｜ invoke name="read_file">',
    ]

    for text in unreadable:
        found = recover(text)
        assert found.calls == [], text
        assert found.text == text, text


def test_one_unreadable_frame_does_not_cost_a_readable_one():
    text = _frame("") + _frame(_invoke("read_file", _parameter("path", "a.md")))

    found = recover(text)

    assert [call.name for call in found.calls] == ["read_file"]
    assert found.unparsed_frames == 1
    assert found.text == _frame("")


def test_an_oversized_frame_is_not_parsed():
    found = recover(
        _frame(
            _invoke("read_file", _parameter("path", "a" * (MAX_FRAME_CHARACTERS + 1)))
        )
    )

    assert found.calls == []
    assert found.unparsed_frames == 1


def test_content_without_a_frame_is_returned_untouched():
    for text in ["", "A plain answer.", "I considered DSML and decided against it."]:
        found = recover(text)
        assert found.calls == []
        assert found.text == text
        assert found.unparsed_frames == 0


def test_a_response_carrying_a_frame_reports_ordinary_tool_calls():
    response = ModelResponse(
        provider_id="provider",
        model="model-a",
        text=f"Checking.\n{_frame(_invoke('read_file', _parameter('path', 'a.md')))}",
    )

    assert [(call.name, call.arguments) for call in response.tool_calls] == [
        ("read_file", {"path": "a.md"})
    ]
    assert response.text == "Checking."
    # Reading the response back does not recover the same calls twice.
    assert len(ModelResponse.model_validate(response.model_dump()).tool_calls) == 1


def test_recovered_calls_join_the_ones_the_route_reported():
    response = ModelResponse(
        provider_id="provider",
        model="model-a",
        text=_frame(_invoke("read_file", _parameter("path", "a.md"))),
        tool_calls=[ToolCall(id="call-1", name="search_files", arguments={})],
    )

    assert [call.name for call in response.tool_calls] == ["search_files", "read_file"]
    assert response.tool_calls[1].id.startswith("dsml-")
    assert response.tool_calls[1].id != response.tool_calls[0].id


def test_a_response_whose_frame_is_unreadable_keeps_its_text_for_quarantine():
    frame = _frame(_invoke("read_file", "the file I mentioned"))
    response = ModelResponse(provider_id="provider", model="model-a", text=frame)

    assert response.tool_calls == []
    assert response.text == frame
