"""DeepSeek's DSML tool protocol, as it arrives inside assistant content.

Some OpenRouter routes serialize a model's DSML tool calls into the Chat
Completions ``content`` field instead of ``tool_calls``. The calls are real
work the model asked for, so Core reads them back into ordinary tool calls
rather than discarding the turn. A frame looks like this, with either an
ASCII ``|`` or the full-width ``｜`` the protocol normally uses::

    <｜DSML｜ calls>
      <｜DSML｜ invoke name="tool_name">
        <｜DSML｜ parameter name="parameter_name">value</｜DSML｜ parameter>
      </｜DSML｜ invoke>
    </｜DSML｜ calls>

The outer tag has other spellings too: DeepSeek V3.2's published encoding
writes ``function_calls`` and marks each parameter ``string="true"`` or
``string="false"``, and Cline has seen ``tool_calls`` frames whose invoke body
is one JSON object. All of them are read the same way.

Nothing here executes anything, and nothing here decides a call is allowed:
recovered calls go to the same broker, policy and approval path every other
tool call takes. Parsing is all-or-nothing per frame. A frame Core cannot
read completely is left in the text exactly as it arrived, so the caller's
existing quarantine still sees it instead of a half-understood call.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Both pipes appear in the wild: the protocol uses U+FF5C, and some routes
# transcode it to ASCII. A backslash before a closing tag is an escape a few
# routes add when they embed the frame in JSON.
_PIPE = "[|\uff5c]"
_TAG = rf"<{_PIPE}DSML{_PIPE}\s*"
# A route that embeds the frame in JSON may escape the closing tag's slash,
# and some escape the whole tag; both spellings mean the same close.
_END = rf"\\?<\\?/{_PIPE}DSML{_PIPE}\s*"

# A frame closes with the spelling it opened with.
_FRAME = re.compile(
    rf"{_TAG}(?P<kind>(?:tool_|function_)?calls)>(?P<body>.*?){_END}(?P=kind)>",
    re.DOTALL,
)
_INVOKE = re.compile(
    rf'{_TAG}invoke\s+name="(?P<name>[^"]*)"\s*>(?P<body>.*?){_END}invoke>',
    re.DOTALL,
)
# Attributes after the name are tolerated; only ``string`` means anything.
_PARAMETER = re.compile(
    rf'{_TAG}parameter\s+name="(?P<name>[^"]*)"(?P<attributes>[^>]*)>'
    rf"(?P<value>.*?){_END}parameter>",
    re.DOTALL,
)
_STRING_ATTRIBUTE = re.compile(r'\bstring\s*=\s*"(?P<declared>true|false)"')
# Any tag of the protocol, opening or closing, in any spelling. It is how a
# frame Core cannot read is still found, wherever in the text it starts.
_ANY_TAG = re.compile(rf"\\?<\\?/?{_PIPE}DSML{_PIPE}")
# How a tag can begin, pipes read as ASCII. A streamed answer holds back a
# tail that could still grow into one of these.
_TAG_OPENINGS = ("<|DSML|", "</|DSML|", "<\\/|DSML|", "\\<|DSML|", "\\</|DSML|")
_LONGEST_OPENING = max(len(opening) for opening in _TAG_OPENINGS)
# A value no parameter can have, for a declared JSON value that is not JSON.
_UNREADABLE = object()

# The identity a recovered call must have to be a tool call at all. It is the
# same shape providers require, checked here so a frame that cannot produce a
# valid call is left quarantined rather than rejected later as a provider bug.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_JSON_NUMBER = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?$")
_JSON_LITERALS = {"true", "false", "null"}

#: A frame larger than this is not parsed. It is far past any real call and
#: bounds the backtracking the patterns above can be asked to do.
MAX_FRAME_CHARACTERS = 200_000
#: Calls per frame, past which the frame is treated as malformed.
MAX_CALLS_PER_FRAME = 64
#: Calls recovered from one message, past which later frames are left alone.
MAX_CALLS_PER_MESSAGE = 64


def frame_start(text: str) -> int | None:
    """Where the first DSML tag in ``text`` begins, or ``None`` without one.

    Any tag counts, in any spelling and whether or not it closes. It is what a
    caller asks after :func:`recover` has already taken every frame it could
    read, so a tag still in the text starts a frame Core could not read, and
    nothing from there on is an answer.
    """

    if "DSML" not in text:
        return None
    match = _ANY_TAG.search(text)
    return match.start() if match is not None else None


def is_frame(text: str) -> bool:
    """Whether ``text`` is a DSML frame with no answer before it."""

    return frame_start(text.strip()) == 0


def partial_tag_start(text: str) -> int:
    """Where a tail of ``text`` that could still become a DSML tag begins.

    A streamed answer can show everything before this index and hold the rest
    until the next piece settles whether a frame is starting. Without such a
    tail this is ``len(text)``.
    """

    for start in range(max(0, len(text) - _LONGEST_OPENING + 1), len(text)):
        tail = text[start:].replace("\uff5c", "|")
        if any(opening.startswith(tail) for opening in _TAG_OPENINGS):
            return start
    return len(text)


@dataclass(frozen=True)
class DsmlCall:
    """One recovered call: a tool name and the arguments it was given."""

    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class DsmlRecovery:
    """What a scan of one assistant message found.

    ``text`` is the message with every frame Core could read removed, and
    every frame it could not read still in place.
    """

    calls: list[DsmlCall] = field(default_factory=list)
    text: str = ""
    unparsed_frames: int = 0

    @property
    def recovered(self) -> bool:
        return bool(self.calls)


def _decode_value(raw: str) -> Any:
    """A parameter body as the value it stands for.

    DSML carries every value as text, so a structured argument arrives as its
    JSON source. Only text that is unambiguously a JSON container or literal
    is decoded; anything else stays the string it was, which keeps a value
    like ``artifact-a`` or ``2 + 2`` exactly as the model wrote it.
    """

    value = raw.strip()
    if not value:
        return ""
    if value[0] in "[{" or value in _JSON_LITERALS or _JSON_NUMBER.match(value):
        try:
            return json.loads(value)
        except ValueError:  # diagnostic-expected: not the JSON it resembled; the literal text is the value
            return value
    return value


def _parameter_value(raw: str, attributes: str) -> Any:
    """A parameter's value, as its ``string`` attribute declares it if it does.

    ``string="true"`` keeps the text exactly as written, even text that looks
    like a number; ``string="false"`` declares JSON, and text that is not JSON
    is unreadable rather than guessed at.
    """

    declared = _STRING_ATTRIBUTE.search(attributes)
    if declared is None:
        return _decode_value(raw)
    if declared.group("declared") == "true":
        return raw
    try:
        return json.loads(raw)
    except ValueError:  # diagnostic-expected: frame left in the text for quarantine
        return _UNREADABLE


def _json_body(body: str) -> dict[str, Any] | None:
    """An invoke whose whole body is one JSON object carries its arguments so."""

    try:
        value = json.loads(body)
    except ValueError:  # diagnostic-expected: frame left in the text for quarantine
        return None
    return value if isinstance(value, dict) else None


def _parse_invoke(name: str, body: str) -> DsmlCall | None:
    if not _TOOL_NAME.match(name):
        return None
    if body.strip().startswith("{") and not _PARAMETER.search(body):
        whole = _json_body(body.strip())
        return None if whole is None else DsmlCall(name=name, arguments=whole)
    arguments: dict[str, Any] = {}
    for match in _PARAMETER.finditer(body):
        parameter = match.group("name")
        # A repeated parameter is ambiguous about which value wins, and an
        # unnamed one has no argument to fill, so neither is guessed at.
        if not parameter or parameter in arguments:
            return None
        value = _parameter_value(match.group("value"), match.group("attributes"))
        if value is _UNREADABLE:
            return None
        arguments[parameter] = value
    # Anything outside the parameters is an instruction Core does not
    # understand, so a call is recovered only when the whole invoke is read.
    if _PARAMETER.sub("", body).strip():
        return None
    return DsmlCall(name=name, arguments=arguments)


def _parse_frame(body: str) -> list[DsmlCall] | None:
    calls: list[DsmlCall] = []
    for match in _INVOKE.finditer(body):
        call = _parse_invoke(match.group("name"), match.group("body"))
        if call is None or len(calls) >= MAX_CALLS_PER_FRAME:
            return None
        calls.append(call)
    if not calls:
        return None
    # Prose between invokes would be an answer Core is about to throw away.
    if _INVOKE.sub("", body).strip():
        return None
    return calls


def recover(text: str) -> DsmlRecovery:
    """Read every complete DSML frame in ``text`` back into tool calls."""

    if not text or "DSML" not in text:
        return DsmlRecovery(text=text)

    calls: list[DsmlCall] = []
    pieces: list[str] = []
    unparsed = 0
    cursor = 0
    for frame in _FRAME.finditer(text):
        body = frame.group("body")
        parsed = (
            _parse_frame(body) if len(frame.group(0)) <= MAX_FRAME_CHARACTERS else None
        )
        if parsed is None:
            unparsed += 1
            continue
        if len(calls) + len(parsed) > MAX_CALLS_PER_MESSAGE:
            unparsed += 1
            continue
        calls.extend(parsed)
        pieces.append(text[cursor : frame.start()])
        cursor = frame.end()
    pieces.append(text[cursor:])
    remaining = "".join(pieces)
    # Removing a frame can leave the blank lines that surrounded it.
    return DsmlRecovery(
        calls=calls,
        text=remaining.strip() if calls else text,
        unparsed_frames=unparsed,
    )
