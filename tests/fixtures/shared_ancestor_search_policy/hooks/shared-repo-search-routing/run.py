#!/usr/bin/env python3
import json
import os
import sys


def deny(kind, reason):
    print(f"repo_search_guard ({kind}): {reason}", file=sys.stderr)
    raise SystemExit(2)


def broad_search(command, workspace):
    projects_root = os.path.dirname(workspace)
    return (
        ("rg " in command or "grep -R" in command)
        and projects_root in command
    )


event = json.load(sys.stdin)
if event.get("schema") != "nebula.native-hook-event/v1":
    deny("tool.before", "invalid native-hook envelope")
if event.get("event") != "tool.before":
    raise SystemExit(0)
payload = event.get("payload")
if not isinstance(payload, dict):
    deny("tool.before", "tool payload is unavailable")
workspace = payload.get("workspace_path")
if not isinstance(workspace, str) or not os.path.isabs(workspace):
    deny("tool.before", "absolute workspace_path is required")

arguments = payload.get("arguments")
if not isinstance(arguments, dict):
    deny("tool.before", "tool arguments are unavailable")
tool_name = payload.get("tool_name")
if tool_name == "run_command":
    command = arguments.get("command")
    if not isinstance(command, str):
        deny("run_command", "command must be text")
    if broad_search(command, workspace):
        deny("run_command", "use capability_search for shared-repository discovery")
elif tool_name == "process_io" and arguments.get("action") == "write":
    context = payload.get("process_context")
    if not isinstance(context, dict) or not all(
        isinstance(context.get(field), str) and context[field]
        for field in ("command", "cwd", "status")
    ):
        deny("process_io shell write", "parent process context is unavailable")
    command = arguments.get("input")
    if not isinstance(command, str):
        deny("process_io shell write", "write input must be text")
    if broad_search(command, workspace):
        deny(
            "process_io shell write",
            "use capability_search for shared-repository discovery",
        )
