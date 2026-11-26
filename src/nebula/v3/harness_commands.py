"""Explicit operator commands, separate from model prompts and turn accounting."""

import re
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any


COMMAND_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def compatibility_commands() -> list[dict[str, str]]:
    return [
        {
            "name": "goal",
            "description": "Set or inspect a goal",
            "hint": "<objective> | status | pause | resume | clear",
            "source": "nebula",
        },
        {
            "name": "goals",
            "description": "Alias for /goal",
            "hint": "<objective> | status | pause | resume | clear",
            "source": "nebula",
        },
        {
            "name": "usage",
            "description": "Session usage",
            "hint": "",
            "source": "nebula",
        },
        {"name": "help", "description": "Command help", "hint": "", "source": "nebula"},
    ]


def normalize_commands(value: Any) -> list[dict[str, str]]:
    """Bound untrusted display metadata; an advertisement replaces the catalog."""
    commands: dict[str, dict[str, str]] = {}
    if not isinstance(value, list):
        return []
    for item in value[:256]:
        if not isinstance(item, dict):
            continue
        name, description = item.get("name"), item.get("description")
        if (
            not isinstance(name, str)
            or not COMMAND_NAME.fullmatch(name)
            or not isinstance(description, str)
        ):
            continue
        argument = item.get("input")
        hint = argument.get("hint") if isinstance(argument, dict) else ""
        commands[name] = {
            "name": name,
            "description": description[:1000],
            "hint": hint[:500] if isinstance(hint, str) else "",
            "source": "native",
        }
    return list(commands.values())


def parse_harness_command(text: str) -> tuple[str, str] | None:
    parts = text.strip().split(maxsplit=1)
    if (
        not parts
        or not parts[0].startswith("/")
        or not COMMAND_NAME.fullmatch(parts[0][1:])
    ):
        return None
    return parts[0], parts[1] if len(parts) > 1 else ""


def usage_reply(value: Any, *, grok: bool) -> str:
    if not isinstance(value, dict):
        raise ValueError(
            "The harness returned an invalid usage response. Retry /usage."
        )
    if grok:
        usage = value.get("usage")
        if not isinstance(usage, dict):
            raise ValueError("Grok did not return session usage. Retry /usage.")
        fields = [
            ("Input tokens", usage.get("inputTokens")),
            ("Output tokens", usage.get("outputTokens")),
        ]
        scope = "Grok session usage (since this harness process opened the session)"
    else:
        usage = value.get("threadUsage")
        if not isinstance(usage, dict):
            return "Codex has no usage estimate for this session yet. Retry /usage after a turn completes."
        groups = usage.get("groups")
        if not isinstance(groups, list):
            raise ValueError("Codex returned invalid session usage. Retry /usage.")
        fields = []
        for label, key in [
            ("Input tokens", "inputTokens"),
            ("Output tokens", "outputTokens"),
            ("Total tokens", "totalTokens"),
        ]:
            counts = [group.get(key) for group in groups if isinstance(group, dict)]
            if counts and all(
                isinstance(count, int) and not isinstance(count, bool) and count >= 0
                for count in counts
            ):
                fields.append(
                    (label, sum(count for count in counts if isinstance(count, int)))
                )
        scope = "Codex session usage (estimated)"
    lines = [scope]
    for label, count in fields:
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            lines.append(f"{label}: {count:,}")
    if len(lines) == 1:
        lines.append("Token counts are not available from the harness yet.")
    return "\n\n".join(lines)


def claude_commands() -> list[dict[str, str]]:
    """Nebula answers these itself; other slash text still reaches Claude."""

    return [
        {
            "name": "usage",
            "description": "Conversation tokens and Claude plan limits",
            "hint": "",
            "source": "nebula",
        }
    ]


def _utc_label(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def claude_usage_reply(
    *,
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    turns: int,
    windows: Sequence[Any],
    reset_windows: int,
) -> str:
    """Nebula's own totals plus the plan windows Claude last reported here.

    ``windows`` are HarnessQuotaSnapshot values, newest first. Claude has no
    on-demand usage read, so every figure states where and when it came from.
    """

    lines = ["Claude usage for this conversation (counted by Nebula)"]
    if turns:
        lines.append(
            "\n".join(
                [
                    f"Input tokens: {input_tokens:,}",
                    f"Output tokens: {output_tokens:,}",
                    f"Total tokens: {total_tokens:,}",
                    f"Turns: {turns:,}",
                ]
            )
        )
    else:
        lines.append("No Claude turns have reported token usage here yet.")
    if windows:
        observed = _utc_label(windows[0].observed_at)
        rows = []
        for window in windows:
            row = (
                f"{window.window_label}: {round(window.used_percent)}% used, "
                f"{round(window.remaining_percent)}% left"
            )
            if window.resets_at is not None:
                row += f" · resets {_utc_label(window.resets_at)}"
            rows.append(row)
        lines.append(
            f"Claude plan limits (last reported by Claude in this project, {observed})"
        )
        lines.append("\n".join(rows))
    else:
        lines.append(
            "Claude has not reported plan limits in this project yet. It reports them "
            "during a turn when a limit's status changes, such as when you near a limit."
        )
    if reset_windows:
        lines.append(
            f"{reset_windows} plan window{'s have' if reset_windows != 1 else ' has'} "
            "reset since Claude last reported it."
        )
    lines.append(
        "Settings › Usage and credits shows the latest Claude report from any project."
    )
    return "\n\n".join(lines)
