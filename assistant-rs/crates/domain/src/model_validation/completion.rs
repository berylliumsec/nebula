//! The complete send-request boundary. Capability availability is checked by
//! preparation after validation; unused selections must never be silently lost.
use super::*;
use sha2::{Digest, Sha256};

/// Canonical request fields, including settings unavailable to a given runtime.
/// Only the transport's complete model validation admits this representation.
/// Prompts and selected context are deliberately excluded from Debug output.
#[derive(Clone, Serialize)]
#[serde(transparent)]
pub struct CompletionRequest {
    pub fields: Map<String, Value>,
}
impl CompletionRequest {
    pub fn parse(bytes: &[u8]) -> Result<Self> {
        let value = hydrate(
            Model::ChatCompletionRequest,
            InputOrigin::RetainedJson,
            bytes,
        )?;
        let Value::Object(fields) = value else {
            return Err(RecordError::Schema);
        };
        Ok(Self { fields })
    }
}
impl<'de> Deserialize<'de> for CompletionRequest {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        // Wire JSON uses Value's number-aware visitor. Already canonical
        // Values must be moved directly, as parse() and the HTTP boundary do:
        // serde_json::from_value can coerce a huge integer through a float.
        match Value::deserialize(deserializer)? {
            Value::Object(fields) => Ok(Self { fields }),
            _ => Err(serde::de::Error::custom(
                "completion request must be an object",
            )),
        }
    }
}

#[derive(Clone, Copy)]
pub(super) enum RequestField {
    Temperature,
    ExactText,
    SourceKind,
    Hash { fixed_length: bool },
    Model(Model),
    Messages,
    StringDictionary,
}
const fn field(name: &'static str, kind: FieldType, nullable: bool) -> Field {
    Field {
        name,
        kind,
        nullable,
    }
}
const fn request(name: &'static str, kind: RequestField, nullable: bool) -> Field {
    field(name, FieldType::Request(kind), nullable)
}
const fn int(name: &'static str, min: i64, max: Option<i64>) -> Field {
    field(name, FieldType::Integer { min, max }, true)
}
const fn flag(name: &'static str) -> Field {
    field(name, FieldType::Boolean, false)
}
pub(super) const MESSAGE_FIELDS: [Field; 3] = [
    field(
        "role",
        FieldType::Enum(&["system", "user", "assistant"]),
        false,
    ),
    text("content", 1, Some(100_000), false),
    field(
        "content_blocks",
        FieldType::Models {
            model: Model::ChatContentBlock,
            max: Some(64),
        },
        false,
    ),
];
pub(super) const ATTACHMENT_FIELDS: [Field; 6] = [
    request("source_kind", RequestField::SourceKind, false),
    text("source_id", 0, Some(200), true),
    text("source_label", 1, Some(500), false),
    request("text", RequestField::ExactText, false),
    request(
        "sha256",
        RequestField::Hash {
            fixed_length: false,
        },
        false,
    ),
    flag("truncated"),
];
pub(super) const PENDING_FIELDS: [Field; 3] = [
    text("provider_profile_id", 0, Some(200), false),
    text("model", 0, Some(500), false),
    int("max_active", 1, Some(100)),
];
pub(super) const FIELDS: [Field; 34] = [
    field("backend", FieldType::ChatBackend, false),
    text("provider_id", 1, Some(200), true),
    text("harness_profile_id", 1, Some(200), true),
    text("harness_session_id", 1, Some(200), true),
    field(
        "mcp_server_ids",
        FieldType::Strings { min: 0, max: 64 },
        false,
    ),
    field(
        "ssh_environment_ids",
        FieldType::Strings { min: 0, max: 64 },
        true,
    ),
    field("hook_ids", FieldType::Strings { min: 0, max: 32 }, false),
    text("model", 0, Some(500), true),
    text("engagement_id", 0, Some(200), true),
    text("session_id", 0, Some(200), true),
    text("goal_id", 0, Some(200), true),
    request("skill", RequestField::StringDictionary, true),
    request("messages", RequestField::Messages, false),
    field(
        "context_attachments",
        FieldType::Models {
            model: Model::ChatContextAttachment,
            max: Some(20),
        },
        false,
    ),
    int("max_output_tokens", 1, Some(1_000_000)),
    request("temperature", RequestField::Temperature, true),
    flag("include_knowledge"),
    flag("allow_cloud_knowledge"),
    flag("tools_enabled"),
    flag("allow_subagents"),
    flag("allow_agent_messaging"),
    int("max_active_subagents", 1, Some(100)),
    text("subagent_provider_id", 0, Some(200), true),
    text("subagent_model", 0, Some(500), true),
    request(
        "pending_provider_subagent",
        RequestField::Model(Model::PendingProviderSubagent),
        true,
    ),
    int("max_artifact_queries", 0, None),
    flag("allow_cloud_tool_results"),
    text("harness_mode", 1, Some(100), true),
    text("harness_reasoning_effort", 0, Some(100), true),
    field(
        "reasoning_effort",
        FieldType::Literal(&["none", "minimal", "low", "medium", "high", "xhigh"]),
        true,
    ),
    text("harness_service_tier", 0, Some(100), true),
    request("harness_skill", RequestField::StringDictionary, true),
    request(
        "runtime_switch_confirmation",
        RequestField::Hash { fixed_length: true },
        true,
    ),
    flag("stream"),
];

pub(super) fn default(model: Model, field: Field) -> Option<Value> {
    match (model, field.name) {
        (Model::ChatCompletionRequest, "backend") => Some(json!("provider")),
        (Model::ChatCompletionRequest, "include_knowledge") => Some(json!(true)),
        (Model::ChatCompletionRequest, "mcp_server_ids" | "hook_ids" | "context_attachments")
        | (Model::ChatRequestMessage, "content_blocks") => Some(json!([])),
        (Model::PendingProviderSubagent, "provider_profile_id" | "model") => Some(json!("")),
        (Model::ChatCompletionRequest | Model::ChatContextAttachment, _)
            if matches!(field.kind, FieldType::Boolean) =>
        {
            Some(json!(false))
        }
        _ => None,
    }
}

pub(super) fn coherence(model: Model, value: &Map<String, Value>) -> Option<&'static str> {
    if model == Model::ChatContextAttachment {
        let digest = format!("{:x}", Sha256::digest(value["text"].as_str()?.as_bytes()));
        return (value["sha256"] != digest)
            .then_some("context attachment sha256 does not match its text");
    }
    if model != Model::ChatCompletionRequest {
        return None;
    }
    let messages = value["messages"].as_array()?;
    if messages
        .iter()
        .map(|m| m["content"].as_str().unwrap().chars().count())
        .sum::<usize>()
        > 250_000
    {
        return Some("chat history exceeds the 250000 character limit");
    }
    if messages.iter().any(|m| m["role"] == "system") {
        return Some("client-supplied system messages are not allowed");
    }
    if messages.last()?["role"] != "user" {
        return Some("the final chat message must have role=user");
    }
    if value["context_attachments"]
        .as_array()?
        .iter()
        .map(|v| v["text"].as_str().unwrap().chars().count())
        .sum::<usize>()
        > 20_000
    {
        return Some("selected context exceeds the 20000 character limit");
    }
    if value["backend"] == "provider" {
        if value["provider_id"].is_null() {
            return Some("provider chat requires provider_id");
        }
        if !value["harness_profile_id"].is_null() || !value["harness_session_id"].is_null() {
            return Some("provider chat cannot include harness runtime fields");
        }
    } else if value["harness_profile_id"].is_null() || !value["provider_id"].is_null() {
        return Some("harness chat requires harness_profile_id and no provider_id");
    }
    None
}

fn fail(
    report: &mut ValidationReport,
    path: Vec<Location>,
    error: Failure,
) -> Result<Option<Value>> {
    report.add_at(error.kind, path.clone(), path, error.msg, error.ctx)?;
    Ok(None)
}
fn exact_text(value: &Value, min: usize, max: usize) -> FieldResult {
    let Some(text) = value.as_str() else {
        return Err(Failure::simple(
            "string_type",
            "Input should be a valid string",
        ));
    };
    let length = text.chars().count();
    if length < min {
        return Err(Failure::context(
            "string_too_short",
            format!(
                "String should have at least {min} character{}",
                if min == 1 { "" } else { "s" }
            ),
            json!({"min_length":min}),
        ));
    }
    if length > max {
        return Err(Failure::context(
            "string_too_long",
            format!("String should have at most {max} characters"),
            json!({"max_length":max}),
        ));
    }
    Ok(value.clone())
}
fn scalar(kind: RequestField, value: &Value) -> FieldResult {
    match kind {
        RequestField::ExactText => exact_text(value, 1, 20_000),
        RequestField::SourceKind | RequestField::Hash { .. } => {
            let (min, max, pattern) = match kind {
                RequestField::SourceKind => (1, Some(100), "^[a-z0-9._-]+$"),
                RequestField::Hash { fixed_length: true } => (64, Some(64), "^[0-9a-f]{64}$"),
                _ => (0, None, "^[0-9a-f]{64}$"),
            };
            let output = validate_field(text("", min, max, false), value)?;
            let spelling = output.as_str().unwrap();
            let valid = match kind {
                RequestField::SourceKind => spelling
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b)),
                _ => {
                    spelling.len() == 64
                        && spelling
                            .bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                }
            };
            if !valid {
                return Err(Failure::context(
                    "string_pattern_mismatch",
                    format!("String should match pattern '{pattern}'"),
                    json!({"pattern":pattern}),
                ));
            }
            Ok(output)
        }
        RequestField::Temperature => {
            let number = goal::float_number(value)?;
            if number.is_nan() || number < 0.0 {
                return Err(Failure::context(
                    "greater_than_equal",
                    "Input should be greater than or equal to 0",
                    json!({"ge":0.0}),
                ));
            }
            if number > 2.0 {
                return Err(Failure::context(
                    "less_than_equal",
                    "Input should be less than or equal to 2",
                    json!({"le":2.0}),
                ));
            }
            Ok(json!(number))
        }
        _ => unreachable!("compound request field"),
    }
}

pub(super) fn validate(
    kind: RequestField,
    value: &Value,
    path: Vec<Location>,
    report: &mut ValidationReport,
) -> Result<Option<Value>> {
    match kind {
        RequestField::Model(model) => fork::nested(model, value, path, report),
        RequestField::Messages => {
            if value.as_array().is_some_and(Vec::is_empty) {
                return fail(
                    report,
                    path,
                    Failure::context(
                        "too_short",
                        "List should have at least 1 item after validation, not 0",
                        json!({"field_type":"List","min_length":1,"actual_length":0}),
                    ),
                );
            }
            fork::models(Model::ChatRequestMessage, Some(200), value, path, report)
        }
        RequestField::StringDictionary => {
            let Some(object) = value.as_object() else {
                return fail(
                    report,
                    path,
                    Failure::simple("dict_type", "Input should be a valid dictionary"),
                );
            };
            let keys: Vec<_> = report
                .input_order_at(&path)
                .map_or_else(|| object.keys().cloned().collect(), |keys| keys.to_vec());
            let before = report.len();
            let mut output = Map::new();
            for key in keys {
                let mut child = path.clone();
                child.push(Location::Field(key.clone()));
                match validate_field(text("", 0, None, false), &object[&key]) {
                    Ok(value) => {
                        output.insert(key.trim().into(), value);
                    }
                    Err(error) => {
                        fail(report, child, error)?;
                    }
                }
            }
            Ok((before == report.len()).then_some(output.into()))
        }
        _ => match scalar(kind, value) {
            Ok(value) => Ok(Some(value)),
            Err(error) => fail(report, path, error),
        },
    }
}
