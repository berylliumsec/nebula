"""Control classification shared by every forwarding and command-admission hop."""

from __future__ import annotations

import re
from typing import Literal

_CONTROL = re.compile(
    r"^/api/v1/(?:approvals/[^/]+/decision|chat/turns/[^/]+/cancel|"
    r"auth/devices/[^/]+|harness-interactions/[^/]+/decision|"
    r"harness-turns/[^/]+/steer|runs/[^/]+/steer|"
    r"memory-budgets/jobs/[^/]+/apply|"
    r"browser-companion/[^/]+/actions/[^/]+|"
    r"browser-actions/[^/]+/decision|browser-intercepts/[^/]+/decision|"
    r"chat/sessions/[^/]+/subagents(?:/[^/]+)?/stop|"
    r"harness-turns/[^/]+(?:/tasks/[^/]+)?/stop|"
    r"runs/[^/]+(?:/browser-automation)?/stop|executions/[^/]+/cancel)$"
)


def command_lane(path: str) -> Literal["work", "control"]:
    return "control" if _CONTROL.fullmatch(path) else "work"


def forwarding_lane(scope: dict) -> Literal["read", "work", "control"]:
    return (
        "read"
        if scope.get("method", "GET") in {"GET", "HEAD", "OPTIONS"}
        else command_lane(scope.get("path", ""))
    )
