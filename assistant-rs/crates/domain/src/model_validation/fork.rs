//! Complete retained fork inputs and clone constructors; no dispatch capability.
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
const fn dictionary(name: &'static str) -> Field {
    field(name, FieldType::Dictionary, false)
}
const ROLE: &[&str] = &["system", "user", "assistant"];
const BLOCK: &[&str] = &["text", "code", "image", "artifact", "citation", "activity"];
const HARNESS_STATUS: &[&str] = &[
    "starting",
    "idle",
    "running",
    "waiting_approval",
    "closed",
    "failed",
    "interrupted",
];
pub(super) const MESSAGE_FIELDS: [Field; 21] = [
    FIELDS[0],
    FIELDS[1],
    FIELDS[2],
    FIELDS[3],
    text("engagement_id", 0, None, false),
    text("session_id", 0, None, false),
    int("sequence", 1, false),
    field("role", FieldType::Enum(ROLE), false),
    text("content", 0, Some(200_000), false),
    text("reasoning", 0, Some(200_000), false),
    field(
        "content_blocks",
        FieldType::Models {
            model: Model::ChatContentBlock,
            max: Some(64),
        },
        false,
    ),
    text("source_message_id", 0, Some(200), true),
    text("provider_profile_id", 0, None, true),
    text("model", 0, None, true),
    field("usage", FieldType::Usage, true),
    int("elapsed_ms", 0, true),
    int("approval_wait_ms", 0, true),
    text("finish_reason", 0, None, true),
    text("provider_request_id", 0, None, true),
    field(
        "citations",
        FieldType::Models {
            model: Model::ChatCitation,
            max: None,
        },
        false,
    ),
    dictionary("metadata"),
];
pub(super) const BLOCK_FIELDS: [Field; 8] = [
    field("type", FieldType::Literal(BLOCK), false),
    text("text", 0, Some(200_000), true),
    text("language", 0, Some(100), true),
    text("artifact_id", 0, Some(200), true),
    text("media_type", 0, Some(200), true),
    text("alt", 0, Some(1000), true),
    text("activity_id", 0, Some(200), true),
    dictionary("metadata"),
];
pub(super) const CITATION_FIELDS: [Field; 7] = [
    text("source_id", 0, None, false),
    text("name", 0, None, false),
    text("citation", 0, None, true),
    text("artifact_id", 0, None, true),
    text("chunk_id", 0, None, false),
    int("page", 1, true),
    text("excerpt", 0, Some(320), false),
];
pub(super) const DECISION_FIELDS: [Field; 17] = [
    FIELDS[0],
    FIELDS[1],
    FIELDS[2],
    FIELDS[3],
    text("engagement_id", 0, None, false),
    text("session_id", 0, None, true),
    field(
        "scope",
        FieldType::Pattern {
            values: &["conversation", "project"],
            pattern: "^(conversation|project)$",
        },
        false,
    ),
    field(
        "kind",
        FieldType::Pattern {
            values: &["decision", "constraint", "assumption", "question"],
            pattern: "^(decision|constraint|assumption|question)$",
        },
        false,
    ),
    text("text", 1, Some(4000), false),
    field(
        "status",
        FieldType::Pattern {
            values: &["active", "superseded", "removed"],
            pattern: "^(active|superseded|removed)$",
        },
        false,
    ),
    text("source_message_id", 0, None, true),
    text("source_session_id", 0, None, true),
    text("source_selection", 0, None, true),
    int("effective_sequence", 0, false),
    text("copied_from_id", 0, None, true),
    field("copied_from_revision", FieldType::AnyInteger, true),
    field(
        "history",
        FieldType::Dictionaries { max: usize::MAX },
        false,
    ),
];
pub(super) const HARNESS_FIELDS: [Field; 16] = [
    FIELDS[0],
    FIELDS[1],
    FIELDS[2],
    FIELDS[3],
    text("engagement_id", 0, None, false),
    text("harness_profile_id", 0, None, false),
    text("display_name", 0, Some(160), true),
    text("external_session_id", 0, Some(500), true),
    text("model", 1, Some(500), false),
    field("status", FieldType::Enum(HARNESS_STATUS), false),
    field(
        "mcp_server_ids",
        FieldType::Strings { min: 0, max: 64 },
        false,
    ),
    field("mcp_snapshot", FieldType::Dictionaries { max: 64 }, false),
    text("adapter_version", 0, Some(200), true),
    text("last_turn_id", 0, Some(200), true),
    field("last_activity_at", FieldType::OptionalTime, false),
    dictionary("metadata"),
];

pub(super) fn default(model: Model, field: Field) -> Option<Value> {
    match (model, field.name) {
        (Model::ChatMessage, "content" | "reasoning") => Some("".into()),
        (Model::ChatMessage, "content_blocks" | "citations")
        | (Model::ChatDecision, "history")
        | (Model::HarnessSession, "mcp_server_ids" | "mcp_snapshot") => Some(json!([])),
        (Model::ChatMessage | Model::ChatContentBlock | Model::HarnessSession, "metadata") => {
            Some(json!({}))
        }
        (Model::ChatDecision, "scope") => Some("conversation".into()),
        (Model::ChatDecision, "kind") => Some("decision".into()),
        (Model::ChatDecision, "status") => Some("active".into()),
        (Model::ChatDecision, "effective_sequence") => Some(json!(0)),
        (Model::HarnessSession, "status") => Some("starting".into()),
        _ => None,
    }
}

pub(super) fn coherence(model: Model, value: &Map<String, Value>) -> Option<&'static str> {
    match model {
        Model::ChatMessage
            if value["role"] == "user"
                && value["content"].as_str().is_some_and(|text| {
                    text.trim_matches(|c: char| {
                        c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
                    })
                    .is_empty()
                }) =>
        {
            Some("user messages require content")
        }
        Model::ChatContentBlock => match value["type"].as_str() {
            Some("text") if value["text"].is_null() => Some("text blocks require text"),
            Some("code") if value["text"].is_null() => Some("code blocks require text"),
            Some("image") if value["artifact_id"].as_str().is_none_or(str::is_empty) => {
                Some("image blocks require artifact_id")
            }
            Some("artifact") if value["artifact_id"].as_str().is_none_or(str::is_empty) => {
                Some("artifact blocks require artifact_id")
            }
            Some("activity") if value["activity_id"].as_str().is_none_or(str::is_empty) => {
                Some("activity blocks require activity_id")
            }
            _ => None,
        },
        _ => None,
    }
}

fn fail(
    report: &mut ValidationReport,
    path: Vec<Location>,
    error: Failure,
) -> Result<Option<Value>> {
    report.add_at(error.kind, path.clone(), path, error.msg, error.ctx)?;
    Ok(None)
}
pub(super) fn nested(
    model: Model,
    value: &Value,
    path: Vec<Location>,
    report: &mut ValidationReport,
) -> Result<Option<Value>> {
    let Some(object) = value.as_object() else {
        return fail(
            report,
            path,
            Failure::context(
                "model_type",
                format!(
                    "Input should be a valid dictionary or instance of {}",
                    model.name()
                ),
                json!({"class_name":model.name()}),
            ),
        );
    };
    if report.model_input_at(&path) == Some(model) {
        // Fork sources are already validated model instances. Python reruns
        // model-after hooks, but does not coerce/revalidate their field values.
        if let Some(message) = coherence(model, object) {
            return fail(report, path, Failure::value(message));
        }
        return Ok(Some(value.clone()));
    }
    let before = report.len();
    let mut output = Map::new();
    for field in model.fields() {
        let mut child = path.clone();
        child.push(Location::Field(field.name.into()));
        if let Some(value) = object.get(field.name) {
            if let Some(value) = goal::validate(*field, value, child, report)? {
                output.insert(field.name.into(), value);
            }
        } else if let Some(value) = default(model, *field).or_else(|| goal::default(model, *field))
        {
            output.insert(field.name.into(), value);
        } else if field.nullable {
            output.insert(field.name.into(), Value::Null);
        } else {
            report.add_at(
                "missing",
                child,
                path.clone(),
                "Field required".into(),
                None,
            )?;
        }
    }
    let keys: Vec<_> = report
        .input_order_at(&path)
        .map_or_else(|| object.keys().cloned().collect(), |keys| keys.to_vec());
    for key in keys {
        if !model.fields().iter().any(|field| field.name == key) {
            let mut child = path.clone();
            child.push(Location::Field(key));
            fail(
                report,
                child,
                Failure::simple("extra_forbidden", "Extra inputs are not permitted"),
            )?;
        }
    }
    if report.len() != before {
        return Ok(None);
    }
    if let Some(message) = coherence(model, &output) {
        return fail(report, path, Failure::value(message));
    }
    Ok(Some(output.into()))
}
pub(super) fn models(
    model: Model,
    max: Option<usize>,
    value: &Value,
    path: Vec<Location>,
    report: &mut ValidationReport,
) -> Result<Option<Value>> {
    let Some(items) = value.as_array() else {
        return fail(
            report,
            path,
            Failure::simple("list_type", "Input should be a valid list"),
        );
    };
    if let Some(max) = max.filter(|max| items.len() > *max) {
        return fail(
            report,
            path,
            Failure::context(
                "too_long",
                format!(
                    "List should have at most {max} items after validation, not {}",
                    items.len()
                ),
                json!({"field_type":"List","max_length":max,"actual_length":items.len()}),
            ),
        );
    }
    if items.len() > MAX_ISSUES {
        return Err(RecordError::TooLarge);
    }
    let before = report.len();
    let mut output = Vec::with_capacity(items.len());
    for (index, item) in items.iter().enumerate() {
        let mut child = path.clone();
        child.push(Location::Index(index));
        if let Some(value) = nested(model, item, child, report)? {
            output.push(value);
        }
    }
    Ok((report.len() == before).then_some(output.into()))
}

pub(super) fn scalar(kind: FieldType, value: &Value) -> FieldResult {
    if let FieldType::AnyInteger = kind {
        return integer(value);
    }
    let (values, error) = match kind {
        FieldType::Enum(values) => (values, "enum"),
        FieldType::Literal(values) => (values, "literal_error"),
        FieldType::Pattern { values, pattern } => {
            let value = validate_field(text("pattern", 0, None, false), value)?;
            if value.as_str().is_some_and(|value| values.contains(&value)) {
                return Ok(value);
            }
            return Err(Failure::context(
                "string_pattern_mismatch",
                format!("String should match pattern '{pattern}'"),
                json!({"pattern":pattern}),
            ));
        }
        _ => unreachable!("fork scalar"),
    };
    if value.as_str().is_some_and(|value| values.contains(&value)) {
        return Ok(value.clone());
    }
    let mut expected = String::new();
    for (index, value) in values.iter().enumerate() {
        if index > 0 {
            expected.push_str(if index + 1 == values.len() {
                " or "
            } else {
                ", "
            });
        }
        expected.push('\'');
        expected.push_str(value);
        expected.push('\'');
    }
    Err(Failure::context(
        error,
        format!("Input should be {expected}"),
        json!({"expected":expected}),
    ))
}
