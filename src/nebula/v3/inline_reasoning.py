"""Reasoning a model wrote into its reply as ``<think>`` markup.

A runtime without a reasoning parser (Ollama, LM Studio, llama.cpp, vLLM
without ``--reasoning-parser``, some OpenRouter upstreams) leaves the model's
thinking in the reply text instead of a reasoning field. Two shapes arrive:

- The reply opens with ``<think>…</think>`` or ``<thinking>…</thinking>``.
  Nothing else opens a reply that way, so it is read for any model.
- The chat template already wrote the opening ``<think>`` into the prompt,
  as DeepSeek R1 and V3.x in thinking mode, GLM and Qwen's thinking models
  do, so the reply carries only the closing ``</think>`` and everything
  before it is the thought. That is read only for those model families, and
  only when the route sent no reasoning of its own: a route that parsed the
  thought leaves no template close behind, and any other ``</think>`` is the
  model talking about the tag.

A tag anywhere else in a reply is the model mentioning it, and stays. This is
Vercel's ``extractReasoningMiddleware`` (``startWithReasoning`` for the
second shape) and LiteLLM's ``_parse_content_for_reasoning``.
"""

from __future__ import annotations

import re
from typing import Literal

Channel = Literal["reasoning", "text"]
Piece = tuple[Channel, str]

_OPENINGS = ("<think>", "<thinking>")
_ANY_OPENING = re.compile(r"<think(?:ing)?>")
_TEMPLATE_CLOSE = "</think>"
# How far back a tag can begin that a later piece completes.
_TAG_OVERLAP = max(len(tag) for tag in (*_OPENINGS, _TEMPLATE_CLOSE)) - 1
# Families whose chat templates end the prompt with ``<think>``: DeepSeek R1
# and V3.x, GLM 4.5 and later, and Qwen's thinking releases (QwQ, Qwen3
# ``-thinking``). Matched anywhere in the id, so vendor and route prefixes
# (``deepseek/``, ``z-ai/``, ``zai-org/``) and suffixes do not matter.
_TEMPLATE_THINKING_MODEL = re.compile(r"deepseek|glm|qwq|qwen.*think", re.IGNORECASE)


def template_opens_thinking(model: str) -> bool:
    """Whether ``model``'s chat template writes the opening ``<think>``."""

    return _TEMPLATE_THINKING_MODEL.search(model) is not None


def _leading_opening(text: str) -> str | None:
    return next((tag for tag in _OPENINGS if text.startswith(tag)), None)


def _closing(opening: str) -> str:
    return f"</{opening[1:]}"


def split_reply(text: str, *, template_opened: bool) -> tuple[str, str]:
    """A whole reply as ``(reasoning, answer)``.

    ``template_opened`` says the prompt already opened a thought, so a
    ``</think>`` with no ``<think>`` before it closes that thought. A thought
    that never closes is all reasoning: the reply stopped before an answer.
    """

    body = text.lstrip()
    opening = _leading_opening(body)
    if opening is not None:
        thought = body[len(opening) :]
        end = thought.find(_closing(opening))
        if end < 0:
            return thought, ""
        return thought[:end], thought[end + len(_closing(opening)) :]
    if template_opened:
        end = text.find(_TEMPLATE_CLOSE)
        if end >= 0 and _ANY_OPENING.search(text, 0, end) is None:
            return text[:end], text[end + len(_TEMPLATE_CLOSE) :]
    return "", text


class ReplySplitter:
    """Splits a streamed reply the way :func:`split_reply` splits a whole one.

    A piece is held only while it is undecided: while the reply's opening
    could still be a think tag, while a closing tag may be arriving, or, for
    a template-opened thought, until its close arrives or the route shows it
    parses reasoning itself. Everything else passes through at once.
    """

    def __init__(self, *, template_opened: bool) -> None:
        self._template_opened = template_opened
        self._state: Literal[
            "opening", "thinking", "template_thought", "answer_start", "answer"
        ] = "opening"
        self._held = ""
        self._close = ""
        self._scanned = 0

    def route_reasoning(self) -> list[Piece]:
        """The route sent reasoning of its own, so it parses thoughts itself.

        A ``</think>`` in the reply is then not a template's, and text held
        waiting for one is answer.
        """

        self._template_opened = False
        if self._state != "template_thought":
            return []
        self._state = "answer"
        return self._drain()

    def push(self, delta: str) -> list[Piece]:
        self._held += delta
        return self._drain()

    def finish(self) -> list[Piece]:
        """Whatever is still held once the reply has ended."""

        held, self._held = self._held, ""
        if self._state == "thinking":
            return [("reasoning", held)] if held else []
        if self._state == "answer_start":
            held = held.lstrip()
        return [("text", held)] if held else []

    def _drain(self) -> list[Piece]:
        pieces: list[Piece] = []
        while True:
            if self._state == "opening":
                body = self._held.lstrip()
                opening = _leading_opening(body)
                if opening is not None:
                    self._state = "thinking"
                    self._close = _closing(opening)
                    self._held = body[len(opening) :]
                elif not body or any(tag.startswith(body) for tag in _OPENINGS):
                    return pieces
                elif self._template_opened:
                    self._state = "template_thought"
                else:
                    self._state = "answer"
            elif self._state == "thinking":
                end = self._held.find(self._close)
                if end < 0:
                    keep = _partial_suffix_start(self._held, self._close)
                    if keep:
                        pieces.append(("reasoning", self._held[:keep]))
                    self._held = self._held[keep:]
                    return pieces
                if end:
                    pieces.append(("reasoning", self._held[:end]))
                self._held = self._held[end + len(self._close) :]
                self._state = "answer_start"
            elif self._state == "template_thought":
                start = max(0, self._scanned - _TAG_OVERLAP)
                end = self._held.find(_TEMPLATE_CLOSE, start)
                limit = end if end >= 0 else len(self._held)
                if _ANY_OPENING.search(self._held, start, limit) is not None:
                    # A tag opened in the reply before any close: the reply
                    # is talking about tags, not closing the prompt's thought.
                    self._state = "answer"
                elif end < 0:
                    self._scanned = len(self._held)
                    return pieces
                else:
                    if end:
                        pieces.append(("reasoning", self._held[:end]))
                    self._held = self._held[end + len(_TEMPLATE_CLOSE) :]
                    self._state = "answer_start"
            elif self._state == "answer_start":
                # The blank lines between a thought and its answer are neither.
                body = self._held.lstrip()
                self._held = body
                if not body:
                    return pieces
                self._state = "answer"
            else:
                if self._held:
                    pieces.append(("text", self._held))
                    self._held = ""
                return pieces


def _partial_suffix_start(text: str, tag: str) -> int:
    """Where a tail of ``text`` that could still grow into ``tag`` begins."""

    for size in range(min(len(tag) - 1, len(text)), 0, -1):
        if text.endswith(tag[:size]):
            return len(text) - size
    return len(text)
