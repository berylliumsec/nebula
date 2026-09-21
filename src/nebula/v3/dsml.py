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

_FRAME = re.compile(rf"{_TAG}calls>(?P<body>.*?){_END}calls>", re.DOTALL)
_INVOKE = re.compile(
    rf'{_TAG}invoke\s+name="(?P<name>[^"]*)"\s*>(?P<body>.*?){_END}invoke>',
    re.DOTALL,
)
_PARAMETER = re.compile(
    rf'{_TAG}parameter\s+name="(?P<name>[^"]*)"\s*>(?P<value>.*?){_END}parameter>',
    re.DOTALL,
)

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


_FRAME_START = re.compile(rf"^{_TAG}calls>")
_FRAME_END = re.compile(rf"{_END}calls>$")


def is_frame(text: str) -> bool:
    """Whether ``text`` is a DSML frame and nothing else.

    This recognizes the outer frame only. It is what a caller asks after
    :func:`recover` has already taken every frame it could read, so a true
    answer means the frame could not be read and there is no answer in it.
    """

    stripped = text.strip()
    if "DSML" not in stripped:
        return False
    return bool(_FRAME_START.match(stripped)) and bool(_FRAME_END.search(stripped))


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


def _parse_invoke(name: str, body: str) -> DsmlCall | None:
    if not _TOOL_NAME.match(name):
        return None
    arguments: dict[str, Any] = {}
    for match in _PARAMETER.finditer(body):
        parameter = match.group("name")
        # A repeated parameter is ambiguous about which value wins, and an
        # unnamed one has no argument to fill, so neither is guessed at.
        if not parameter or parameter in arguments:
            return None
        arguments[parameter] = _decode_value(match.group("value"))
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
