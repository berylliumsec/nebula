"""Inert protocol peer; Core and the production harness adapters remain real."""

import asyncio
import json

from nebula.v3.harnesses import HarnessTransportError, _AcpRpc


class CommandPeer(_AcpRpc):
    connection_state = "connected"

    def __init__(self, root, session_id):
        self.events = asyncio.Queue()
        self.command_catalogs = {}
        self._capture_notification(
            {
                "method": "session/update",
                "params": {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [
                            {
                                "name": "vendor-check",
                                "description": "Check project",
                                "input": {"hint": "target"},
                            }
                        ],
                    },
                },
            }
        )
        self.root, self.session_id = root, session_id
        self.state = root / f"command-goal-{session_id}.json"

    async def request(self, method, params):
        with (self.root / "command-requests.jsonl").open("a") as file:
            file.write(json.dumps({"method": method, "params": params}) + "\n")
        goal = json.loads(self.state.read_text()) if self.state.exists() else None
        if method == "account/usage/read":
            return {
                "threadUsage": {
                    "groups": [
                        {"inputTokens": 12, "outputTokens": 3, "totalTokens": 15}
                    ]
                }
            }
        if method == "_x.ai/session/usage":
            return {"usage": {"inputTokens": 12, "outputTokens": 3}}
        if method == "thread/goal/get":
            return {"goal": goal}
        if method == "thread/goal/set":
            goal = {
                "objective": params.get(
                    "objective", (goal or {}).get("objective", "Clock")
                ),
                "status": params.get("status", "active"),
            }
            self.state.write_text(json.dumps(goal))
            return {"goal": goal}
        if method == "thread/goal/clear":
            self.state.unlink(missing_ok=True)
            return {}
        if method == "session/prompt":
            first = params["prompt"][0]["text"]
            if first.startswith("/vendor-check"):
                answer = "Vendor command received: " + first
                self._capture_notification(
                    {
                        "method": "session/update",
                        "params": {
                            "sessionId": self.session_id,
                            "update": {
                                "sessionUpdate": "available_commands_update",
                                "availableCommands": [],
                            },
                        },
                    }
                )
            elif first == "/goal status":
                answer = "Goal: Clock" if goal else "No goal is set."
            else:
                if first.startswith("/goal "):
                    self.state.write_text(
                        json.dumps({"objective": first[6:], "status": "active"})
                    )
                answer = "Goal work completed."
                await self.events.put(
                    {
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_thought_chunk",
                                "content": {
                                    "type": "text",
                                    "text": "Retained thinking from the native peer.",
                                },
                            }
                        },
                    }
                )
            await self.events.put(
                {
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": answer},
                        }
                    },
                }
            )
            return {"stopReason": "end_turn"}
        if method == "turn/start":
            turn = "native-command-turn"
            updates = [
                (
                    "item/reasoning/summaryTextDelta",
                    {
                        "itemId": "thought",
                        "summaryIndex": 0,
                        "delta": "Retained thinking from the native peer.",
                    },
                ),
                (
                    "item/completed",
                    {
                        "item": {
                            "id": "thought",
                            "type": "reasoning",
                            "summary": [
                                {
                                    "type": "summary_text",
                                    "text": "Retained thinking from the native peer.",
                                }
                            ],
                        }
                    },
                ),
                (
                    "item/agentMessage/delta",
                    {"itemId": "answer", "delta": "Goal work completed."},
                ),
                ("turn/completed", {"turn": {"id": turn, "status": "completed"}}),
            ]
            for event, payload in updates:
                await self.events.put(
                    {
                        "method": event,
                        "params": {
                            "threadId": self.session_id,
                            "turnId": turn,
                            **payload,
                        },
                    }
                )
            return {"turn": {"id": turn}}
        raise HarnessTransportError(f"Unsupported inert peer method: {method}")

    async def notify(self, method, params=None):
        pass

    async def close(self):
        self.connection_state = "disconnected"
