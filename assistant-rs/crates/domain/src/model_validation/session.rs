//! Session construction only; no changes to retained Session read semantics.
use super::*;

pub(super) const FIELDS: [Field; 14] = [
    super::FIELDS[0],
    super::FIELDS[1],
    super::FIELDS[2],
    super::FIELDS[3],
    text("engagement_id", 0, None, false),
    text("title", 1, Some(300), false),
    Field {
        name: "backend",
        kind: FieldType::ChatBackend,
        nullable: false,
    },
    text("provider_profile_id", 0, None, true),
    text("harness_profile_id", 0, None, true),
    text("harness_session_id", 0, None, true),
    text("parent_session_id", 0, Some(200), true),
    text("forked_from_message_id", 0, Some(200), true),
    text("model", 0, None, false),
    Field {
        name: "metadata",
        kind: FieldType::Dictionary,
        nullable: false,
    },
];
pub(super) fn default(model: Model, field: Field) -> Option<Value> {
    if model != Model::ChatSession {
        return None;
    }
    match field.name {
        "backend" => Some("provider".into()),
        "metadata" => Some(json!({})),
        _ => None,
    }
}
pub(super) fn backend(value: &Value) -> FieldResult {
    if matches!(value.as_str(), Some("provider" | "harness")) {
        Ok(value.clone())
    } else {
        Err(Failure::context(
            "enum",
            "Input should be 'provider' or 'harness'",
            json!({"expected":"'provider' or 'harness'"}),
        ))
    }
}
pub(super) fn coherence(p: &Map<String, Value>) -> Option<&'static str> {
    let truthy = |name: &str| p[name].as_str().is_some_and(|v| !v.is_empty());
    if p["backend"] == "provider" {
        if !truthy("provider_profile_id")
            || !p["harness_profile_id"].is_null()
            || !p["harness_session_id"].is_null()
        {
            return Some("provider chat sessions require only provider_profile_id");
        }
    } else if !truthy("harness_profile_id")
        || !truthy("harness_session_id")
        || !p["provider_profile_id"].is_null()
    {
        return Some("harness chat sessions require harness profile and session");
    }
    None
}
