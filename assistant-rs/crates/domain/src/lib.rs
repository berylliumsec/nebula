//! Assistant laboratory event types. Legacy assistant API parity is not established.

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub mod auth;
pub mod dependencies;
mod harness_profile;
pub mod records;
pub mod session_state;

pub const MAX_EVENT_BYTES: usize = 1024 * 1024;

#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct ValidationError(pub &'static str);

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AssistantEvent {
    pub id: String,
    pub turn_id: String,
    pub sequence: i64,
    pub event_type: String,
    pub payload: Map<String, Value>,
    pub actor_id: Option<String>,
    pub occurred_at: DateTime<Utc>,
    pub idempotency_key: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AppendEvent {
    pub turn_id: String,
    pub event_type: String,
    #[serde(default)]
    pub payload: Map<String, Value>,
    pub actor_id: Option<String>,
    pub idempotency_key: Option<String>,
}

impl AppendEvent {
    pub fn validate(&self) -> Result<(), ValidationError> {
        bounded(&self.turn_id, 200)?;
        bounded(&self.event_type, 200)?;
        if let Some(value) = &self.actor_id {
            bounded(value, 200)?;
        }
        if let Some(value) = &self.idempotency_key {
            bounded(value, 300)?;
        }
        if serde_json::to_vec(self)
            .map_err(|_| ValidationError("invalid JSON"))?
            .len()
            > MAX_EVENT_BYTES
        {
            return Err(ValidationError("event exceeds 1 MiB"));
        }
        Ok(())
    }

    pub fn matches(&self, existing: &AssistantEvent) -> bool {
        self.turn_id == existing.turn_id
            && self.event_type == existing.event_type
            && self.payload == existing.payload
            && self.actor_id == existing.actor_id
            && self.idempotency_key == existing.idempotency_key
    }
}

pub fn bounded(value: &str, max_chars: usize) -> Result<(), ValidationError> {
    if value.is_empty() || value.chars().count() > max_chars || value.contains('\0') {
        return Err(ValidationError(
            "identifier is empty, contains NUL, or is too long",
        ));
    }
    Ok(())
}
