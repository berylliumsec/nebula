//! Read-only contracts for canonical Python-persisted assistant records.
//!
//! This is deliberately separate from request validation: records have already
//! received Pydantic defaults/coercion. The persisted decoder additionally fills
//! deterministic legacy defaults, without creating identities or timestamps.
//! Unknown fields and incompatible records are reported, never silently dropped.

use std::{collections::HashMap, sync::LazyLock};

use chrono::{DateTime, FixedOffset, NaiveDateTime, SecondsFormat, Utc};
use jsonschema::Validator;
use serde_json::Value;

pub const MAX_RECORD_BYTES: usize = 16 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum AssistantKind {
    Session,
    Message,
    ReadCursor,
    Bookmark,
    Decision,
    Queue,
    Goal,
    GoalUsageCharge,
    Turn,
    Subagent,
    SubagentMessage,
    AgentMessage,
    Schedule,
}

impl AssistantKind {
    pub const ALL: [Self; 13] = [
        Self::Session,
        Self::Message,
        Self::ReadCursor,
        Self::Bookmark,
        Self::Decision,
        Self::Queue,
        Self::Goal,
        Self::GoalUsageCharge,
        Self::Turn,
        Self::Subagent,
        Self::SubagentMessage,
        Self::AgentMessage,
        Self::Schedule,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Session => "chat_sessions",
            Self::Message => "chat_messages",
            Self::ReadCursor => "chat_read_cursors",
            Self::Bookmark => "chat_bookmarks",
            Self::Decision => "chat_decisions",
            Self::Queue => "chat_queues",
            Self::Goal => "chat_goals",
            Self::GoalUsageCharge => "chat_goal_usage_charges",
            Self::Turn => "chat_turns",
            Self::Subagent => "chat_subagents",
            Self::SubagentMessage => "chat_subagent_messages",
            Self::AgentMessage => "chat_agent_messages",
            Self::Schedule => "chat_schedules",
        }
    }
}

impl TryFrom<&str> for AssistantKind {
    type Error = RecordError;

    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Self::ALL
            .into_iter()
            .find(|kind| kind.as_str() == value)
            .ok_or(RecordError::UnknownKind)
    }
}

#[derive(Debug, thiserror::Error, PartialEq)]
pub enum RecordError {
    #[error("unsupported assistant record kind")]
    UnknownKind,
    #[error(
        "assistant record exceeds the 16 MiB read limit; retain the original record for migration review"
    )]
    TooLarge,
    #[error("assistant record contains invalid JSON")]
    Json,
    #[error("embedded assistant schema could not be compiled")]
    Schema,
    // Never interpolate the payload or JSON-schema error: metadata can contain
    // sensitive transcript or credential material.
    #[error("assistant record does not match the canonical {0} storage schema")]
    Shape(&'static str),
    #[error("assistant record violates a persisted invariant: {0}")]
    Invariant(&'static str),
    #[error("{0}")]
    ModelValidation(crate::model_validation::ValidationReport),
}

/// A validated, immutable record. Opaque metadata is retained as JSON values;
/// these payloads must never be treated as authorization or execution commands.
#[derive(Clone, Debug, PartialEq)]
pub struct StoredAssistantRecord {
    kind: AssistantKind,
    payload: Value,
}

impl StoredAssistantRecord {
    /// Complete Turn model at the execution boundary. Other services retain
    /// their existing error/decoding surfaces until their own parity review.
    pub fn decode_execution_turn_direct(
        bytes: &[u8],
        origin: crate::model_validation::InputOrigin,
    ) -> Result<Self, RecordError> {
        let payload = crate::model_validation::hydrate(
            crate::model_validation::Model::ChatTurn,
            origin,
            bytes,
        )?;
        Self::decode(
            AssistantKind::Turn,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }

    pub fn decode_execution_turn_created(
        bytes: &[u8],
        defaults: &crate::model_validation::CreatedEntityDefaults,
        typed_paths: &[crate::model_validation::TypedModelPath],
    ) -> Result<Self, RecordError> {
        let payload = crate::model_validation::hydrate_created_turn(bytes, defaults, typed_paths)?;
        Self::decode(
            AssistantKind::Turn,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }

    /// Direct model boundary used only by transcript forks. Other retained
    /// readers preserve their existing strict decoding and error surfaces.
    pub fn decode_fork_persisted_direct(
        kind: AssistantKind,
        bytes: &[u8],
    ) -> Result<Self, RecordError> {
        use crate::model_validation::{InputOrigin, Model, hydrate};
        let model = match kind {
            AssistantKind::Session => Model::ChatSession,
            AssistantKind::Message => Model::ChatMessage,
            AssistantKind::Decision => Model::ChatDecision,
            AssistantKind::Goal => Model::ChatGoal,
            _ => return Err(RecordError::UnknownKind),
        };
        let payload = hydrate(model, InputOrigin::RetainedJson, bytes)?;
        Self::decode(
            kind,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }

    pub fn decode_fork_created(
        kind: AssistantKind,
        bytes: &[u8],
        defaults: &crate::model_validation::CreatedEntityDefaults,
        typed_paths: &[crate::model_validation::TypedModelPath],
    ) -> Result<Self, RecordError> {
        use crate::model_validation::{Model, hydrate_fork_created};
        let model = match kind {
            AssistantKind::Session => Model::ChatSession,
            AssistantKind::Message => Model::ChatMessage,
            AssistantKind::Decision => Model::ChatDecision,
            AssistantKind::Goal => Model::ChatGoal,
            _ => return Err(RecordError::UnknownKind),
        };
        let payload = hydrate_fork_created(model, bytes, defaults, typed_paths)?;
        Self::decode(
            kind,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }

    /// Match a direct Python model hydration where model validation errors are
    /// observable. Wrapped storage reads must continue using decode_persisted.
    /// Complete Goal/Schedule contracts are covered here; other kinds
    /// retain their existing decoder and error semantics.
    pub fn decode_persisted_direct(kind: AssistantKind, bytes: &[u8]) -> Result<Self, RecordError> {
        Self::decode_direct(
            kind,
            bytes,
            crate::model_validation::InputOrigin::RetainedJson,
        )
    }

    /// Validate an already merged writer model. Its timestamp inputs represent
    /// Python datetime values from model_dump(mode="python"), not an HTTP body.
    pub fn decode_updated_direct(kind: AssistantKind, bytes: &[u8]) -> Result<Self, RecordError> {
        Self::decode_direct(
            kind,
            bytes,
            crate::model_validation::InputOrigin::WriterModelDump,
        )
    }

    /// Construct a Goal or Session with already sampled trusted Entity clock factories,
    /// retaining the original constructor arguments in detailed errors.
    pub fn decode_created_direct(
        kind: AssistantKind,
        bytes: &[u8],
        created_at: DateTime<Utc>,
        updated_at: DateTime<Utc>,
    ) -> Result<Self, RecordError> {
        let payload = match kind {
            AssistantKind::Goal => {
                crate::model_validation::hydrate_created_goal(bytes, created_at, updated_at)?
            }
            AssistantKind::Session => {
                crate::model_validation::hydrate_created_session(bytes, created_at, updated_at)?
            }
            _ => return Err(RecordError::UnknownKind),
        };
        Self::decode(
            kind,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }

    fn decode_direct(
        kind: AssistantKind,
        bytes: &[u8],
        origin: crate::model_validation::InputOrigin,
    ) -> Result<Self, RecordError> {
        let model = match kind {
            AssistantKind::Schedule => crate::model_validation::Model::ChatSchedule,
            AssistantKind::Goal => crate::model_validation::Model::ChatGoal,
            _ => return Self::decode_persisted(kind, bytes),
        };
        let payload = crate::model_validation::hydrate(model, origin, bytes)?;
        Self::decode(
            kind,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }

    /// Read an older persisted payload using deterministic model defaults only.
    /// Identity, revision and timestamps must already exist. Opaque values retain
    /// their model semantics; no clock, UUID factory or external helper runs.
    pub fn decode_persisted(kind: AssistantKind, bytes: &[u8]) -> Result<Self, RecordError> {
        if kind == AssistantKind::Goal {
            return Self::decode_direct(
                kind,
                bytes,
                crate::model_validation::InputOrigin::RetainedJson,
            )
            .map_err(|error| match error {
                // Wrapped Store.get/list_entities expose corruption rather
                // than detailed Pydantic errors. Drop the input owner here.
                RecordError::ModelValidation(_) => RecordError::Shape("chat_goals"),
                error => error,
            });
        }
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        let mut payload: Value = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
        for field in ["id", "revision", "created_at", "updated_at"] {
            if payload.get(field).is_none() {
                return Err(RecordError::Shape(kind.as_str()));
            }
        }
        let schemas = SCHEMAS.as_ref().map_err(|_| RecordError::Schema)?;
        let schema = &schemas["entities"][kind.as_str()];
        fill_defaults(schema, schema, &mut payload);
        // Entity and Schedule have UTC field validators. Other timestamps
        // (notably goals and device read cursors) retain their recorded offset.
        // Keep decode() itself lossless for canonical-contract round trips.
        let timestamp_fields: &[&str] = if kind == AssistantKind::Schedule {
            &["created_at", "updated_at", "next_run_at", "last_run_at"]
        } else {
            &["created_at", "updated_at"]
        };
        for &field in timestamp_fields {
            if field == "last_run_at" && payload[field].is_null() {
                continue;
            }
            let time = aware(&payload[field])?;
            payload[field] = time
                .with_timezone(&Utc)
                .to_rfc3339_opts(
                    if time.timestamp_subsec_micros() == 0 {
                        SecondsFormat::Secs
                    } else {
                        SecondsFormat::Micros
                    },
                    true,
                )
                .into();
        }
        Self::decode(
            kind,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }

    pub fn decode(kind: AssistantKind, bytes: &[u8]) -> Result<Self, RecordError> {
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        let payload: Value = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
        let validators = VALIDATORS.as_ref().map_err(|_| RecordError::Schema)?;
        if !validators
            .get(&kind)
            .ok_or(RecordError::Schema)?
            .is_valid(&payload)
        {
            return Err(RecordError::Shape(kind.as_str()));
        }
        validate_invariants(kind, &payload)?;
        Ok(Self { kind, payload })
    }

    pub fn kind(&self) -> AssistantKind {
        self.kind
    }
    pub fn payload(&self) -> &Value {
        &self.payload
    }
    pub fn into_payload(self) -> Value {
        self.payload
    }

    /// Match `message_is_replaced`: retracted messages remain stored and can be
    /// inspected, but must be excluded from the current transcript/context.
    pub fn is_replaced_message(&self) -> bool {
        self.kind == AssistantKind::Message && truthy(&self.payload["metadata"]["retracted_at"])
    }
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|value| value != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn recorded_datetime(value: &str) -> bool {
    // Most optional model timestamps permit naive datetimes in the Python
    // baseline. Entity and schedule timestamps require awareness separately.
    DateTime::parse_from_rfc3339(value).is_ok()
        || NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f").is_ok()
}

pub(crate) fn require_canonical_fields(schema: &mut Value) {
    // A model_dump(mode="json") record includes every field, including nulls
    // and factory defaults. Do not invent UUIDs/times during a migration read.
    if schema.get("additionalProperties") == Some(&Value::Bool(false))
        && let Some(properties) = schema.get("properties").and_then(Value::as_object)
    {
        schema["required"] = Value::Array(properties.keys().cloned().map(Value::String).collect());
    }
    match schema {
        Value::Object(properties) => {
            for value in properties.values_mut() {
                require_canonical_fields(value);
            }
        }
        Value::Array(items) => {
            for value in items {
                require_canonical_fields(value);
            }
        }
        _ => {}
    }
}

static SCHEMAS: LazyLock<Result<Value, RecordError>> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../../../compatibility/python-assistant.json"))
        .map_err(|_| RecordError::Schema)
});

fn resolved<'a>(schema: &'a Value, root: &'a Value) -> &'a Value {
    if let Some(name) = schema
        .get("$ref")
        .and_then(Value::as_str)
        .and_then(|reference| reference.strip_prefix("#/$defs/"))
    {
        &root["$defs"][name]
    } else {
        schema
    }
}

pub(crate) fn fill_defaults(schema: &Value, root: &Value, payload: &mut Value) {
    let schema = resolved(schema, root);
    // JSON schema may express a Python float default as an integer (0). Match
    // model_dump's float representation without touching opaque metadata.
    if schema.get("type").and_then(Value::as_str) == Some("number")
        && let Some(value) = payload.as_f64()
        && let Some(number) = serde_json::Number::from_f64(value)
    {
        *payload = Value::Number(number);
    }
    if schema.get("type").and_then(Value::as_str) == Some("string")
        && schema.get("enum").is_none()
        && schema.get("const").is_none()
        && schema.get("format").is_none()
        && let Some(value) = payload.as_str()
    {
        *payload = Value::String(value.trim().to_owned());
    }
    if let Some(branches) = schema.get("anyOf").and_then(Value::as_array) {
        for branch in branches {
            if !payload.is_null() && branch.get("type").and_then(Value::as_str) != Some("null") {
                fill_defaults(branch, root, payload);
            }
        }
    }
    if let (Some(properties), Some(object)) = (
        schema.get("properties").and_then(Value::as_object),
        payload.as_object_mut(),
    ) {
        for (name, property) in properties {
            if !object.contains_key(name)
                && !schema["required"]
                    .as_array()
                    .is_some_and(|fields| fields.iter().any(|field| field == name))
            {
                let default = property.get("default").cloned().or_else(|| {
                    match resolved(property, root).get("type").and_then(Value::as_str) {
                        Some("array") => Some(Value::Array(Vec::new())),
                        Some("object") => Some(Value::Object(Default::default())),
                        _ => None,
                    }
                });
                if let Some(default) = default {
                    object.insert(name.clone(), default);
                }
            }
            if let Some(value) = object.get_mut(name) {
                fill_defaults(property, root, value);
            }
        }
    } else if let (Some(items), Some(values)) = (schema.get("items"), payload.as_array_mut()) {
        for value in values {
            fill_defaults(items, root, value);
        }
    }
}

static VALIDATORS: LazyLock<Result<HashMap<AssistantKind, Validator>, RecordError>> =
    LazyLock::new(|| {
        let inventory = SCHEMAS.as_ref().map_err(|_| RecordError::Schema)?;
        AssistantKind::ALL
            .into_iter()
            .map(|kind| {
                let mut schema = inventory["entities"][kind.as_str()].clone();
                if !schema.is_object() {
                    return Err(RecordError::Schema);
                }
                require_canonical_fields(&mut schema);
                let validator = jsonschema::draft202012::options()
                    .with_format("date-time", recorded_datetime)
                    .should_validate_formats(true)
                    .build(&schema)
                    .map_err(|_| RecordError::Schema)?;
                Ok((kind, validator))
            })
            .collect()
    });

fn aware(value: &Value) -> Result<DateTime<FixedOffset>, RecordError> {
    value
        .as_str()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .ok_or(RecordError::Invariant("timestamp must include a timezone"))
}

fn require(condition: bool, invariant: &'static str) -> Result<(), RecordError> {
    if condition {
        Ok(())
    } else {
        Err(RecordError::Invariant(invariant))
    }
}

fn claim_is_atomic(payload: &Value) -> bool {
    let count = [
        "execution_owner_id",
        "execution_claim_id",
        "execution_claimed_at",
    ]
    .into_iter()
    .filter(|key| !payload[key].is_null())
    .count();
    count == 0 || count == 3
}

fn validate_invariants(kind: AssistantKind, p: &Value) -> Result<(), RecordError> {
    require(
        aware(&p["updated_at"])? >= aware(&p["created_at"])?,
        "updated_at precedes created_at",
    )?;
    match kind {
        AssistantKind::Session => {
            let coherent = if p["backend"] == "provider" {
                truthy(&p["provider_profile_id"])
                    && p["harness_profile_id"].is_null()
                    && p["harness_session_id"].is_null()
            } else {
                truthy(&p["harness_profile_id"])
                    && truthy(&p["harness_session_id"])
                    && p["provider_profile_id"].is_null()
            };
            require(coherent, "session backend binding is incoherent")?;
        }
        AssistantKind::Message => {
            require(
                p["role"] != "user"
                    || p["content"]
                        .as_str()
                        .is_some_and(|value| !value.trim().is_empty()),
                "user messages require content",
            )?;
            for block in p["content_blocks"].as_array().ok_or(RecordError::Schema)? {
                let coherent = match block["type"].as_str() {
                    Some("text" | "code") => !block["text"].is_null(),
                    Some("image" | "artifact") => truthy(&block["artifact_id"]),
                    Some("activity") => truthy(&block["activity_id"]),
                    _ => true,
                };
                require(coherent, "content block is missing its required reference")?;
            }
        }
        AssistantKind::Goal => {
            require(
                p["status"] != "blocked" || truthy(&p["blocked_reason"]),
                "blocked goals require a reason",
            )?;
            require(
                p["status"] != "completed"
                    || (truthy(&p["completion_summary"]) && truthy(&p["completion_evidence"])),
                "completed goals require a summary and evidence",
            )?;
            require(
                claim_is_atomic(p),
                "goal execution ownership must be recorded atomically",
            )?;
        }
        AssistantKind::Turn => {
            require(
                if p["backend"] == "provider" {
                    truthy(&p["provider_profile_id"])
                } else {
                    p["provider_profile_id"].is_null()
                },
                "turn backend binding is incoherent",
            )?;
            require(
                claim_is_atomic(p),
                "turn execution ownership must be recorded atomically",
            )?;
        }
        AssistantKind::Subagent => {
            require(
                (p["status"] == "running") == p["finished_at"].is_null(),
                "finished_at is required exactly for finished subagents",
            )?;
        }
        AssistantKind::SubagentMessage => {
            require(
                p["awaiting_reply"] != true || p["expects_reply"] == true,
                "only a question can await a reply",
            )?;
            require(
                p["expects_reply"] != true || p["direction"] == "to_parent",
                "only a subagent asks its parent a question",
            )?;
        }
        AssistantKind::AgentMessage => {
            require(
                p["sender_session_id"] != p["recipient_session_id"],
                "agent messages require different sender and recipient",
            )?;
            require(
                (p["status"] == "pending") == p["delivered_at"].is_null(),
                "delivered_at is required exactly for terminal agent messages",
            )?;
        }
        AssistantKind::Schedule => {
            aware(&p["next_run_at"])?;
            if !p["last_run_at"].is_null() {
                aware(&p["last_run_at"])?;
            }
        }
        _ => {}
    }
    Ok(())
}
