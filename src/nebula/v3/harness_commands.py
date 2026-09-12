"""Explicit operator commands, separate from model prompts and turn accounting."""

import re
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
