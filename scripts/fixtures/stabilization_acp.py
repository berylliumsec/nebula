#!/usr/bin/env python3
"""Inert ACP peer for packaged-Core acceptance, never an execution runtime.

Only fixed protocol messages are emitted. No commands, network, credentials or
operator paths are read. The runner copies this file into its disposable profile;
the adjacent receipt records whether the real adapter delivered a decision once.
"""

import json
from pathlib import Path
import sys


def main():
    if "models" in sys.argv:
        print("* stabilization-fixture")
        return
    if "--version" in sys.argv:
        print("stabilization-acp-fixture-v1")
        return
    pending = None
    sequence = 0
    receipt = Path(__file__).with_suffix(".receipts.jsonl")

    def emit(message):
        print(json.dumps({"jsonrpc": "2.0", **message}), flush=True)

    def respond(identifier, result):
        emit({"id": identifier, "result": result})

    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        identifier = message.get("id")
        if method == "initialize":
            respond(
                identifier,
                {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "Inert acceptance fixture", "version": "1"},
                    "agentCapabilities": {"loadSession": True},
                },
            )
        elif method in {"authenticate", "session/load", "session/set_mode"}:
            respond(identifier, {})
        elif method == "session/new":
            respond(identifier, {"sessionId": "inert-native-session"})
        elif method == "session/prompt":
            sequence += 1
            permission_id = f"inert-permission-{sequence}"
            pending = (identifier, permission_id)
            emit(
                {
                    "id": permission_id,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "inert-native-session",
                        "toolCall": {
                            "toolCallId": permission_id,
                            "title": "Inert fixture acknowledgement; no tool executes",
                        },
                        "options": [
                            {
                                "optionId": "allow",
                                "kind": "allow_once",
                                "name": "Allow once",
                            },
                            {
                                "optionId": "deny",
                                "kind": "reject_once",
                                "name": "Reject",
                            },
                        ],
                    },
                }
            )
        elif pending and method == "session/cancel":
            respond(pending[0], {"stopReason": "cancelled"})
            pending = None
        elif pending and identifier == pending[1] and "result" in message:
            allowed = message["result"].get("outcome", {}).get("optionId") == "allow"
            with receipt.open("a") as output:
                output.write(
                    json.dumps({"request_id": identifier, "allowed": allowed}) + "\n"
                )
            answer = (
                "NATIVE_APPROVAL_ACCEPTED_ONCE"
                if allowed
                else "NATIVE_APPROVAL_DECLINED"
            )
            if allowed:
                answer += "\n\n" + "\n\n".join(
                    f"Local fixture paragraph {index}. No command or network action occurred."
                    for index in range(1, 61)
                )
            emit(
                {
                    "method": "session/update",
                    "params": {
                        "sessionId": "inert-native-session",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": answer},
                        },
                    },
                }
            )
            respond(pending[0], {"stopReason": "end_turn"})
            pending = None
        elif identifier is not None:
            emit(
                {
                    "id": identifier,
                    "error": {"code": -32601, "message": "Unsupported fixture method"},
                }
            )


if __name__ == "__main__":
    main()
