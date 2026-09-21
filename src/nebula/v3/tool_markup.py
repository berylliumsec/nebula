"""Tool calls a model wrote into its reply in its own native markup.

A serving runtime that does not run a model's tool parser leaves the call in
the Chat Completions ``content`` field. vLLM and SGLang skip the parser for
a request that declares no tools or sets ``tool_choice: "none"``, and some
OpenRouter upstreams never run it. Three grammars arrive this way:

- DeepSeek's DSML, read by :mod:`nebula.v3.dsml`;
- GLM 4.5 and later::

      <tool_call>name<arg_key>key</arg_key><arg_value>value</arg_value></tool_call>

  GLM-4.5's template puts a newline after the name and after each element,
  and the 4.7 and 5.x templates put none;
- DeepSeek V3 and R1 special tokens::

      <｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>name
      ```json
      {"key": "value"}
      ```<｜tool▁call▁end｜><｜tool▁calls▁end｜>

  and V3.1's ``<｜tool▁call▁begin｜>name<｜tool▁sep｜>{...}<｜tool▁call▁end｜>``.

Each is a control frame, never an answer. A frame Core can read completely
becomes an ordinary tool call, which goes to the same broker, policy and
approval path as any other: nothing here executes or allows anything.
Parsing is all-or-nothing per frame, and a frame Core cannot read stays in
the text for the caller's quarantine, which :func:`frame_start` finds
wherever it begins.

GLM's tag is plain enough for an answer to mention, so a GLM frame starts
only where the tag is followed by a tool name and then a line break, an
argument or the close, or where argument tags follow it before it closes.
DSML tags and DeepSeek's special tokens are never prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from . import dsml

Markup = Literal["dsml", "glm", "deepseek"]

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")

_GLM_OPEN = "<tool_call>"
_GLM_FRAME = re.compile(r"<tool_call>(?P<body>.*?)</tool_call>", re.DOTALL)
_GLM_CLOSE = "</tool_call>"
_GLM_ARGUMENT_TAGS = ("<arg_key>", "<arg_value>")
# Where a frame starts: the tag and a name, then the line break GLM-4.5
# writes after it, an argument or the close.
_GLM_START = re.compile(
    r"<tool_call>\s*[A-Za-z_][\w.-]*(?:[ \t]*\n|\s*(?:<arg_key>|</tool_call>))"
)
# A tail that is still on its way to such a start.
_GLM_PENDING = re.compile(r"<tool_call>\s*(?:[A-Za-z_][\w.-]*\s*)?(?P<rest><[^>]*)?")
_GLM_PENDING_ELEMENTS = ("<arg_key>", _GLM_CLOSE)
_GLM_NAME = re.compile(r"\s*(?P<name>[^\s<]+)\s*")
_GLM_ARGUMENT = re.compile(
    r"<arg_key>(?P<key>.*?)</arg_key>\s*<arg_value>(?P<value>.*?)</arg_value>\s*",
    re.DOTALL,
)
# A pending GLM start is only ever this long: a name is at most 128
# characters. A longer tail is released rather than held indefinitely.
_GLM_PENDING_WINDOW = 256

# DeepSeek writes U+FF5C pipes and U+2581 word separators; routes that
# normalize text turn the pipes into ASCII, which is read the same way.
_DS_PIPE = "[|\uff5c]"


def _deepseek_token(name: str) -> str:
    return rf"<{_DS_PIPE}tool\u2581{name}{_DS_PIPE}>"


_DS_FRAME = re.compile(
    _deepseek_token("calls\u2581begin")
    + r"(?P<body>.*?)"
    + _deepseek_token("calls\u2581end"),
    re.DOTALL,
)
_DS_CALL = re.compile(
    r"\s*"
    + _deepseek_token("call\u2581begin")
    + r"(?P<call>.*?)"
    + _deepseek_token("call\u2581end")
    + r"\s*",
    re.DOTALL,
)
_DS_SEPARATOR = re.compile(_deepseek_token("sep"))
# Any of the protocol's tool tokens, complete or not.
_DS_START = re.compile(rf"<{_DS_PIPE}tool\u2581")
_DS_OPENING = "<|tool\u2581"
_DS_FENCE = "```"


@dataclass(frozen=True)
class MarkupCall:
    """One recovered call and the markup it arrived in."""

    name: str
    arguments: dict[str, Any]
    markup: Markup


@dataclass(frozen=True)
class MarkupRecovery:
    """What a scan of one assistant message found.

    ``text`` is the message with every frame Core could read removed, and
    every frame it could not read still in place.
    """

    calls: list[MarkupCall] = field(default_factory=list)
    text: str = ""
    unparsed_frames: int = 0

    @property
    def recovered(self) -> bool:
        return bool(self.calls)


def _glm_start(text: str) -> int | None:
    if _GLM_OPEN not in text:
        return None
    match = _GLM_START.search(text)
    first = match.start() if match is not None else None
    # A tag whose arguments follow before it closes is a frame too, even
    # when what sits between them is not a name Core could read.
    start = text.find(_GLM_OPEN)
    while start >= 0 and (first is None or start < first):
        close = text.find(_GLM_CLOSE, start)
        frame = text[start : close if close >= 0 else len(text)]
        if any(tag in frame for tag in _GLM_ARGUMENT_TAGS):
            return start
        start = text.find(_GLM_OPEN, start + 1)
    return first


def _deepseek_start(text: str) -> int | None:
    if "tool\u2581" not in text:
        return None
    match = _DS_START.search(text)
    return match.start() if match is not None else None


def frame_start(text: str) -> int | None:
    """Where the first native tool frame in ``text`` begins, or ``None``.

    A caller asks after :func:`recover` has taken every frame it could read,
    so a frame still in the text is one Core could not read, and nothing
    from there on is an answer.
    """

    starts = [
        start
        for start in (dsml.frame_start(text), _glm_start(text), _deepseek_start(text))
        if start is not None
    ]
    return min(starts, default=None)


def is_frame(text: str) -> bool:
    """Whether ``text`` is a native tool frame with no answer before it."""

    return frame_start(text.strip()) == 0


def _glm_pending(tail: str) -> bool:
    if _GLM_OPEN.startswith(tail):
        return True
    match = _GLM_PENDING.fullmatch(tail)
    if match is None:
        return False
    rest = match.group("rest") or ""
    return not rest or any(
        element.startswith(rest) for element in _GLM_PENDING_ELEMENTS
    )


def _glm_partial_start(text: str) -> int:
    window = max(0, len(text) - _GLM_PENDING_WINDOW)
    start = text.find("<", window)
    while start >= 0:
        if _glm_pending(text[start:]):
            return start
        start = text.find("<", start + 1)
    return len(text)


def _deepseek_partial_start(text: str) -> int:
    for start in range(max(0, len(text) - len(_DS_OPENING) + 1), len(text)):
        if _DS_OPENING.startswith(text[start:].replace("\uff5c", "|")):
            return start
    return len(text)


def partial_tag_start(text: str) -> int:
    """Where a tail of ``text`` that could still start a frame begins.

    A streamed answer can show everything before this index and hold the rest
    until the next piece settles it. Without such a tail this is ``len(text)``.
    """

    return min(
        dsml.partial_tag_start(text),
        _glm_partial_start(text),
        _deepseek_partial_start(text),
    )


def _arguments_object(source: str) -> dict[str, Any] | None:
    try:
        value = json.loads(source)
    except ValueError:  # diagnostic-expected: frame left in the text for quarantine
        return None
    return value if isinstance(value, dict) else None


def _parse_glm(body: str) -> list[MarkupCall] | None:
    head = _GLM_NAME.match(body)
    if head is None or not _TOOL_NAME.match(head.group("name")):
        return None
    arguments: dict[str, Any] = {}
    cursor = head.end()
    while cursor < len(body):
        argument = _GLM_ARGUMENT.match(body, cursor)
        # Anything but arguments after the name is an instruction Core does
        # not understand, so the call is read whole or not at all.
        if argument is None:
            return None
        key = argument.group("key").strip()
        # A repeated key is ambiguous about which value wins, and an empty
        # one has no argument to fill.
        if not key or key in arguments:
            return None
        arguments[key] = dsml.decode_value(argument.group("value"))
        cursor = argument.end()
    return [MarkupCall(name=head.group("name"), arguments=arguments, markup="glm")]


def _fenced_call(rest: str) -> tuple[str, str] | None:
    """V3 and R1: the name on the separator's line, then a fenced JSON body."""

    name, line_break, block = rest.lstrip().partition("\n")
    block = block.strip()
    if not line_break or len(block) < 2 * len(_DS_FENCE):
        return None
    if not (block.startswith(_DS_FENCE) and block.endswith(_DS_FENCE)):
        return None
    source = block[len(_DS_FENCE) : -len(_DS_FENCE)]
    return name.strip(), source.removeprefix("json").strip()


def _parse_deepseek_call(call: str) -> MarkupCall | None:
    parts = _DS_SEPARATOR.split(call)
    if len(parts) != 2:
        return None
    kind, rest = parts[0].strip(), parts[1]
    fenced = _fenced_call(rest) if kind == "function" else None
    if fenced is not None:
        name, source = fenced
    else:
        # V3.1 names the function before the separator. A V3 call whose
        # type is not ``function`` has no name there and fails the check.
        name, source = kind, rest.strip()
    if not _TOOL_NAME.match(name):
        return None
    arguments = _arguments_object(source)
    if arguments is None:
        return None
    return MarkupCall(name=name, arguments=arguments, markup="deepseek")


def _parse_deepseek(body: str) -> list[MarkupCall] | None:
    calls: list[MarkupCall] = []
    cursor = 0
    while cursor < len(body):
        match = _DS_CALL.match(body, cursor)
        if match is None or len(calls) >= dsml.MAX_CALLS_PER_FRAME:
            return None
        call = _parse_deepseek_call(match.group("call"))
        if call is None:
            return None
        calls.append(call)
        cursor = match.end()
    return calls or None


def _recover_frames(
    text: str,
    frames: re.Pattern[str],
    parse: Callable[[str], list[MarkupCall] | None],
    budget: int,
) -> tuple[list[MarkupCall], str, int]:
    calls: list[MarkupCall] = []
    pieces: list[str] = []
    unparsed = 0
    cursor = 0
    for frame in frames.finditer(text):
        parsed = (
            parse(frame.group("body"))
            if len(frame.group(0)) <= dsml.MAX_FRAME_CHARACTERS
            else None
        )
        if parsed is None or len(calls) + len(parsed) > budget:
            unparsed += 1
            continue
        calls.extend(parsed)
        pieces.append(text[cursor : frame.start()])
        cursor = frame.end()
    pieces.append(text[cursor:])
    return calls, "".join(pieces), unparsed


def recover(text: str) -> MarkupRecovery:
    """Read every complete native tool frame in ``text`` back into tool calls."""

    found = dsml.recover(text)
    calls = [
        MarkupCall(name=call.name, arguments=call.arguments, markup="dsml")
        for call in found.calls
    ]
    remaining = found.text
    unparsed = found.unparsed_frames
    grammars: list[
        tuple[str, re.Pattern[str], Callable[[str], list[MarkupCall] | None]]
    ] = [
        (_GLM_OPEN, _GLM_FRAME, _parse_glm),
        ("tool\u2581calls\u2581begin", _DS_FRAME, _parse_deepseek),
    ]
    for marker, frames, parse in grammars:
        if marker not in remaining:
            continue
        recovered, rest, missed = _recover_frames(
            remaining, frames, parse, dsml.MAX_CALLS_PER_MESSAGE - len(calls)
        )
        calls.extend(recovered)
        unparsed += missed
        if recovered:
            remaining = rest
    return MarkupRecovery(
        calls=calls,
        # Removing a frame can leave the blank lines that surrounded it.
        text=remaining.strip() if calls else text,
        unparsed_frames=unparsed,
    )
