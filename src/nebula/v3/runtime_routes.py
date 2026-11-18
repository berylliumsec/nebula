"""Control classification shared by every forwarding and command-admission hop."""

from __future__ import annotations

import json
import re
from typing import Literal

_CONTROL = re.compile(
    r"^/api/v1/(?:approvals/[^/]+/decision|chat/turns/[^/]+/cancel|"
    r"auth/devices/[^/]+|harness-interactions/[^/]+/decision|"
    r"harness-turns/[^/]+/steer|runs/[^/]+/steer|"
    r"memory-budgets/jobs/[^/]+/apply|"
    r"browser-companion/[^/]+/(?:control|actions/[^/]+)|"
    r"browser-actions/[^/]+/decision|browser-intercepts/[^/]+/decision|"
    r"chat/sessions/[^/]+/subagents(?:/[^/]+)?/stop|"
    r"harness-turns/[^/]+(?:/tasks/[^/]+)?/stop|"
    r"runs/[^/]+(?:/browser-automation)?/stop|executions/[^/]+/cancel)$"
)
# One route carries every goal action; only its body says which. Pause is a
# control, while start and resume begin work and stay behind ordinary admission.
_GOAL_ACTIONS = re.compile(r"^/api/v1/chat/sessions/[^/]+/goal/actions$")
_TURN_CANCEL = re.compile(r"^/api/v1/chat/turns/[^/]+/cancel$")
_CONTROL_GOAL_ACTIONS = frozenset({"pause"})


def goal_action(body: bytes | None) -> str | None:
    """The requested goal action, or None when the body does not name one."""
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        # diagnostic-expected: request validation answers a malformed body; it is ordinary work here.
        return None
    action = parsed.get("action") if isinstance(parsed, dict) else None
    return action if isinstance(action, str) else None


def command_lane(
    path: str, *, method: str = "POST", body: bytes | None = None
) -> Literal["work", "control"]:
    if _CONTROL.fullmatch(path):
        return "control"
    if (
        method == "POST"
        and _GOAL_ACTIONS.fullmatch(path)
        and goal_action(body) in _CONTROL_GOAL_ACTIONS
    ):
        return "control"
    return "work"


def recovery_control(method: str, path: str, body: bytes | None) -> bool:
    """Whether a control may run while project restoration or activation is held.

    These record an operator's pause or stop without starting work, replaying
    an interrupted effect or clearing an execution claim. Every other control
    waits for the project to finish recovery.
    """
    if method != "POST":
        return False
    if _TURN_CANCEL.fullmatch(path):
        return True
    return bool(
        _GOAL_ACTIONS.fullmatch(path) and goal_action(body) in _CONTROL_GOAL_ACTIONS
    )


def forwarding_lane(scope: dict) -> Literal["read", "work", "control"]:
    """Forwarding capacity for one request; it authorizes nothing.

    A forwarder sees no body, so a goal action travels on control capacity:
    every request admission may classify as control is forwarded as control,
    and admission (which reads the body) still holds start and resume as work.
    """
    method = scope.get("method", "GET")
    if method in {"GET", "HEAD", "OPTIONS"}:
        return "read"
    path = scope.get("path", "")
    if method == "POST" and _GOAL_ACTIONS.fullmatch(path):
        return "control"
    return command_lane(path, method=method)


_CHAT_SESSION = re.compile(r"^/api/v1/chat-sessions/[^/]+$")
_GOAL = re.compile(r"^/api/v1/chat/sessions/[^/]+/goal$")


def rerunnable_request(method: str, path: str, body: bytes | None) -> bool:
    """Whether a request may run again after its worker died having saved nothing.

    Every effect of these routes is a project SQL write committed through the
    execution scope, or follows one, so a run that committed no write applied
    nothing. A provider call before it has no durable effect.

    A harness chat qualifies too: ``prepare_chat`` commits the chat turn, its
    queued harness turn and the operator's message in one transaction, and
    only ``start_chat_turn`` on that saved, queued turn sends anything to the
    vendor. A run that committed nothing never reached the vendor. Before
    that write it may only read (the skill catalog, tool components).

    Routes stay out when an effect can precede their first Core write, or
    lives outside Core SQL: deleting a chat (closes its harness session
    first), files, the vault, Butler memory, processes, browsers, network
    deliveries and harness steering.
    """
    try:
        parsed = json.loads(body) if body else {}
    except (ValueError, UnicodeError, RecursionError):
        # diagnostic-expected: request validation answers a malformed body; it never runs again.
        return False
    if not isinstance(parsed, dict):
        return False
    if method == "POST" and path == "/api/v1/chat/completions":
        if parsed.get("backend", "provider") == "harness":
            return True
        # A streamed provider chat starts as a typed chat acceptance, which
        # recovery runs again from its own binding.
        return (
            parsed.get("stream") is not True
            and parsed.get("backend", "provider") == "provider"
        )
    if method == "POST" and _GOAL_ACTIONS.fullmatch(path):
        # Every goal action is one guarded goal update (with its children).
        return isinstance(parsed.get("action"), str)
    if method in {"POST", "PATCH"} and _GOAL.fullmatch(path):
        # Creating or editing a goal saves the goal row alone.
        return True
    if method == "PATCH" and _CHAT_SESSION.fullmatch(path):
        # Rename, archive, pin, flag and settings: one conversation update,
        # then its schedule's pause or resume, all in Core SQL.
        return True
    if method == "POST" and path == "/api/v1/chat/control-assistant/session":
        # Reserving Butler's conversation saves it (and, for a harness, its
        # session record) without contacting the vendor.
        return parsed.get("backend") in {"provider", "harness"}
    return False
