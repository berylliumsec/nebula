#!/usr/bin/env python3
"""Emit deterministic stored-record compatibility cases using the Python oracle.

Only constructs Pydantic values. No application, database, provider, or tool starts.
These are canonical persisted records, not API coercion/defaulting conformance.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from pydantic import ValidationError
from nebula.v3.domain import ENTITY_MODEL_BY_KIND

ROOT = Path(__file__).resolve().parents[1]
AT = "2026-09-23T12:00:00Z"
LATER = "2026-09-23T12:01:00Z"


def collect_records():
    models = {k: v for k, v in ENTITY_MODEL_BY_KIND.items() if k.startswith("chat_")}
    inventory = json.loads(
        (ROOT / "assistant-rs/compatibility/python-assistant.json").read_text()
    )
    schemas = {k: v.model_json_schema() for k, v in sorted(models.items())}
    if schemas != inventory["entities"]:
        raise ValueError(
            "Python assistant schemas changed; review and recapture the baseline first"
        )
    required = {
        "chat_sessions": {
            "title": "Documentation",
            "model": "fixture",
            "provider_profile_id": "provider",
        },
        "chat_messages": {
            "session_id": "session",
            "sequence": 1,
            "role": "user",
            "content": "Review the documentation",
        },
        "chat_read_cursors": {
            "session_id": "session",
            "device_id": "device",
            "through_at": AT,
        },
        "chat_bookmarks": {"session_id": "session", "message_id": "message"},
        "chat_decisions": {"session_id": "session", "text": "Keep existing examples"},
        "chat_queues": {"session_id": "session"},
        "chat_goals": {
            "session_id": "session",
            "objective": "Review docs",
            "completion_criteria": ["Examples are consistent"],
        },
        "chat_goal_usage_charges": {
            "goal_id": "goal",
            "subagent_id": "child",
            "child_turn_id": "child-turn",
            "usage": {},
        },
        "chat_turns": {
            "session_id": "session",
            "model": "fixture",
            "provider_profile_id": "provider",
        },
        "chat_subagents": {
            "parent_session_id": "session",
            "parent_turn_id": "turn",
            "child_session_id": "child",
            "name": "Reviewer",
            "task": "Review examples",
            "started_at": AT,
        },
        "chat_subagent_messages": {
            "subagent_id": "child",
            "parent_session_id": "session",
            "direction": "to_child",
            "content": "Review examples",
        },
        "chat_agent_messages": {
            "sender_session_id": "sender",
            "recipient_session_id": "recipient",
            "transcript_message_id": "transcript",
            "content": "The examples match",
        },
        "chat_schedules": {
            "session_id": "session",
            "provider_profile_id": "provider",
            "model": "fixture",
            "interval_seconds": 3600,
            "next_run_at": LATER,
        },
    }
    bases = {}
    cases = []

    def canonical(kind, values):
        return models[kind].model_validate(values).model_dump(mode="json")

    def valid(name, kind, changes):
        payload = canonical(kind, {**bases[kind], **changes})
        cases.append({"name": name, "kind": kind, "valid": True, "payload": payload})
        return payload

    def invalid(name, kind, changes):
        payload = {**deepcopy(bases[kind]), **changes}
        try:
            canonical(kind, payload)
        except ValidationError:
            cases.append(
                {"name": name, "kind": kind, "valid": False, "payload": payload}
            )
        else:
            raise AssertionError(f"invalid oracle case unexpectedly accepted: {name}")

    for kind, fields in sorted(required.items()):
        bases[kind] = canonical(
            kind,
            {
                "id": f"fixture-{kind}",
                "created_at": AT,
                "updated_at": LATER,
                "revision": 2,
                "engagement_id": "project",
                **fields,
            },
        )
        valid(f"{kind}:canonical", kind, {})
        invalid(f"{kind}:revision", kind, {"revision": 0})
        invalid(f"{kind}:time-order", kind, {"created_at": LATER, "updated_at": AT})
        invalid(f"{kind}:naive-created", kind, {"created_at": "2026-09-23T12:00:00"})
        invalid(f"{kind}:unknown-field", kind, {"unexpected": "must not be discarded"})

    s = "chat_sessions"
    valid(
        "session:harness",
        s,
        {
            "backend": "harness",
            "provider_profile_id": None,
            "harness_profile_id": "harness",
            "harness_session_id": "vendor-session",
        },
    )
    valid(
        "session:archive-branch-metadata",
        s,
        {
            "parent_session_id": "parent",
            "forked_from_message_id": "original",
            "metadata": {
                "archived": True,
                "archived_at": AT,
                "allow_agent_messaging": True,
                "max_parallel_subagents": None,
                "opaque": {"future": [1, None, " 東京 "]},
            },
        },
    )
    valid("session:unicode-limit", s, {"title": "🦀" * 300})
    valid(
        "session:large-metadata-integers",
        s,
        {"metadata": {"positive": 2**100, "negative": -(2**100)}},
    )
    invalid("session:missing-provider", s, {"provider_profile_id": None})
    invalid("session:mixed-backend", s, {"harness_profile_id": "harness"})
    invalid(
        "session:harness-missing-session",
        s,
        {
            "backend": "harness",
            "provider_profile_id": None,
            "harness_profile_id": "harness",
        },
    )
    invalid("session:unicode-over-limit", s, {"title": "🦀" * 301})

    m = "chat_messages"
    block_message = valid(
        "message:multimodal",
        m,
        {
            "role": "assistant",
            "content": "Summary",
            "content_blocks": [
                {"type": "text", "text": "Summary"},
                {"type": "code", "text": "print('fixture')", "language": "python"},
                {"type": "image", "artifact_id": "image", "alt": "Diagram"},
                {"type": "artifact", "artifact_id": "attachment"},
                {"type": "activity", "activity_id": "activity"},
                {"type": "citation"},
            ],
            "citations": [
                {
                    "source_id": "source",
                    "name": "Readme",
                    "chunk_id": "chunk",
                    "excerpt": "Example",
                    "page": 1,
                }
            ],
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            "elapsed_ms": 1000,
            "approval_wait_ms": 200,
        },
    )
    valid(
        "message:replaced",
        m,
        {"metadata": {"retracted_at": AT, "replaced_by_message_id": "replacement"}},
    )
    for value in [None, False, 0, "", [], {}]:
        valid(
            f"message:retraction-false:{json.dumps(value)}",
            m,
            {"metadata": {"retracted_at": value}},
        )
    valid("message:empty-assistant", m, {"role": "assistant", "content": ""})
    valid("message:unicode-content", m, {"content": "café 日本語 🦀\n" * 1000})
    invalid("message:blank-user", m, {"content": "   "})
    invalid("message:sequence", m, {"sequence": 0})
    invalid("message:negative-usage", m, {"usage": {"input_tokens": -1}})
    invalid("message:negative-elapsed", m, {"elapsed_ms": -1})
    invalid("message:unknown-role", m, {"role": "tool"})
    for index, field in [
        (0, "text"),
        (1, "text"),
        (2, "artifact_id"),
        (3, "artifact_id"),
        (4, "activity_id"),
    ]:
        blocks = deepcopy(block_message["content_blocks"])
        blocks[index][field] = None
        invalid(f"message:block-reference:{index}", m, {"content_blocks": blocks})
    blocks = deepcopy(block_message["content_blocks"])
    blocks[0]["extra"] = "unknown nested field"
    invalid("message:block-extra", m, {"content_blocks": blocks})

    for kind in ["chat_goals", "chat_turns"]:
        valid(
            f"{kind}:owned",
            kind,
            {
                "execution_owner_id": "owner",
                "execution_claim_id": "claim",
                "execution_claimed_at": AT,
            },
        )
        invalid(f"{kind}:partial-claim", kind, {"execution_owner_id": "owner"})
        invalid(
            f"{kind}:bad-claimed-time",
            kind,
            {
                "execution_owner_id": "owner",
                "execution_claim_id": "claim",
                "execution_claimed_at": "not-a-date",
            },
        )
    g = "chat_goals"
    valid("goal:blocked", g, {"status": "blocked", "blocked_reason": "Input needed"})
    valid(
        "goal:completed",
        g,
        {
            "status": "completed",
            "completion_summary": "Reviewed",
            "completion_evidence": [{"message_id": "answer"}],
        },
    )
    invalid("goal:blocked-without-reason", g, {"status": "blocked"})
    invalid(
        "goal:completed-without-evidence",
        g,
        {"status": "completed", "completion_summary": "Reviewed"},
    )
    invalid("goal:empty-criteria", g, {"completion_criteria": []})
    invalid(
        "goal:child-list-limit", g, {"child_session_ids": [str(i) for i in range(33)]}
    )
    t = "chat_turns"
    for status in [
        "routing",
        "waiting_approval",
        "waiting_callback",
        "finalizing",
        "complete",
        "failed",
        "cancelled",
        "interrupted",
    ]:
        valid(f"turn:status:{status}", t, {"status": status})
    valid(
        "turn:recovery-evidence",
        t,
        {
            "status": "interrupted",
            "request_snapshot": {
                "recovery": {
                    "automatic_retry_pending": True,
                    "unknown_tool_call_ids": ["unknown"],
                }
            },
            "tool_history": [
                {"tool_call_id": "unknown", "status": "failed", "provider_result": None}
            ],
        },
    )
    invalid("turn:queued-not-a-legacy-status", t, {"status": "queued"})
    invalid("turn:missing-provider", t, {"provider_profile_id": None})
    invalid("turn:harness-references-provider", t, {"backend": "harness"})
    valid(
        "turn:harness",
        t,
        {
            "backend": "harness",
            "provider_profile_id": None,
            "harness_turn_id": "harness-turn",
        },
    )
    child = "chat_subagents"
    for status in ["completed", "failed", "stopped", "interrupted"]:
        valid(
            f"child:terminal:{status}", child, {"status": status, "finished_at": LATER}
        )
        invalid(f"child:missing-finish:{status}", child, {"status": status})
    invalid("child:running-with-finish", child, {"finished_at": LATER})
    valid(
        "child:late-report",
        child,
        {
            "status": "completed",
            "finished_at": LATER,
            "reported_at": LATER,
            "pending_goal_charge_turn_id": "child-turn",
            "rounds": 2,
        },
    )
    cm = "chat_subagent_messages"
    valid(
        "child-message:question",
        cm,
        {"direction": "to_parent", "expects_reply": True, "awaiting_reply": True},
    )
    invalid("child-message:parent-question", cm, {"expects_reply": True})
    invalid("child-message:waiting-without-question", cm, {"awaiting_reply": True})
    peer = "chat_agent_messages"
    for status in ["delivered", "undelivered"]:
        valid(f"peer:{status}", peer, {"status": status, "delivered_at": LATER})
        invalid(f"peer:missing-delivery:{status}", peer, {"status": status})
    invalid("peer:self", peer, {"recipient_session_id": "sender"})
    invalid("peer:pending-with-delivery", peer, {"delivered_at": LATER})
    schedule = "chat_schedules"
    valid("schedule:archived", schedule, {"enabled": False, "paused_by": "archive"})
    invalid("schedule:short-interval", schedule, {"interval_seconds": 3599})
    invalid("schedule:long-interval", schedule, {"interval_seconds": 2592001})
    invalid("schedule:naive-next", schedule, {"next_run_at": "2026-09-23T12:00:00"})
    invalid("schedule:naive-last", schedule, {"last_run_at": "2026-09-23T12:00:00"})
    valid(
        "cursor:naive-baseline-permitted",
        "chat_read_cursors",
        {"through_at": "2026-09-23T12:00:00"},
    )
    valid(
        "decision:history",
        "chat_decisions",
        {
            "history": [{"text": "Old", "revision": 1}],
            "copied_from_id": "original",
            "copied_from_revision": 1,
        },
    )
    invalid("decision:unknown-scope", "chat_decisions", {"scope": "global"})
    invalid("queue:too-many-items", "chat_queues", {"items": [{}] * 1001})
    return {
        "format": "nebula.assistant-record-oracle/v1",
        "scope": "canonical model_dump JSON; excludes request coercion and historical defaults",
        "baseline_commit": inventory["baseline_commit"],
        "source_sha256": sha256(
            (ROOT / "src/nebula/v3/domain.py").read_bytes()
        ).hexdigest(),
        "schema_sha256": sha256(
            json.dumps(schemas, sort_keys=True).encode()
        ).hexdigest(),
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect_records()
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "cases": len(result["cases"]),
                "valid": sum(c["valid"] for c in result["cases"]),
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
