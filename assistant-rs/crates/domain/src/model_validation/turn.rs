//! Complete provider/harness Turn fields; no ownership or lifecycle mutation.
use super::*;

const fn field(name: &'static str, kind: FieldType, nullable: bool) -> Field {
    Field {
        name,
        kind,
        nullable,
    }
}
const fn int(name: &'static str, min: i64, nullable: bool) -> Field {
    field(name, FieldType::Integer { min, max: None }, nullable)
}

pub(super) const FIELDS: [Field; 32] = [
    super::FIELDS[0],
    super::FIELDS[1],
    super::FIELDS[2],
    super::FIELDS[3],
    text("engagement_id", 0, None, false),
    text("session_id", 0, None, false),
    text("goal_id", 0, None, true),
    field("backend", FieldType::ChatBackend, false),
    text("provider_profile_id", 0, None, true),
    text("harness_turn_id", 0, None, true),
    text("model", 0, None, false),
    field(
        "status",
        FieldType::Enum(&[
            "routing",
            "waiting_approval",
            "waiting_callback",
            "finalizing",
            "complete",
            "failed",
            "cancelled",
            "interrupted",
        ]),
        false,
    ),
    field("tools_enabled", FieldType::Boolean, false),
    int("max_tool_calls", 0, true),
    int("max_artifact_queries", 0, true),
    int("next_step", 0, false),
    int("execution_tool_calls", 0, false),
    int("artifact_queries", 0, false),
    field(
        "tool_call_ids",
        FieldType::Strings {
            min: 0,
            max: usize::MAX,
        },
        false,
    ),
    field(
        "tool_history",
        FieldType::Dictionaries { max: usize::MAX },
        false,
    ),
    text("approval_id", 0, None, true),
    text("scope_policy_id", 0, None, true),
    int("scope_revision", 1, true),
    field("request_snapshot", FieldType::Dictionary, false),
    field("usage", FieldType::Usage, false),
    text("reasoning", 0, Some(200_000), false),
    text("content", 0, Some(200_000), false),
    text("final_message_id", 0, None, true),
    text("error", 0, Some(1_000), true),
    text("execution_owner_id", 0, Some(200), true),
    text("execution_claim_id", 0, Some(200), true),
    field("execution_claimed_at", FieldType::OptionalTime, true),
];

pub(super) fn default(model: Model, field: Field) -> Option<Value> {
    if model != Model::ChatTurn {
        return None;
    }
    match field.name {
        "backend" => Some("provider".into()),
        "status" => Some("routing".into()),
        "tools_enabled" => Some(false.into()),
        "next_step" | "execution_tool_calls" | "artifact_queries" => Some(0.into()),
        "tool_call_ids" | "tool_history" => Some(json!([])),
        "request_snapshot" => Some(json!({})),
        "usage" => Some(json!({"input_tokens":0,"output_tokens":0,"total_tokens":0})),
        "reasoning" | "content" => Some("".into()),
        _ => None,
    }
}

pub(super) fn coherence(p: &Map<String, Value>) -> Option<&'static str> {
    if p["backend"] == "provider" && p["provider_profile_id"].as_str().is_none_or(str::is_empty) {
        return Some("provider chat turns require provider_profile_id");
    }
    if p["backend"] == "harness" && !p["provider_profile_id"].is_null() {
        return Some("harness chat turns cannot reference a provider");
    }
    let present = [
        "execution_owner_id",
        "execution_claim_id",
        "execution_claimed_at",
    ]
    .iter()
    .filter(|name| !p[**name].is_null())
    .count();
    if present != 0 && present != 3 {
        return Some("turn execution ownership must be recorded atomically");
    }
    None
}
