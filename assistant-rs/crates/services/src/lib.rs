//! Assistant application services. Transport authentication is a separate boundary:
//! never expose these methods directly to an unauthenticated caller.
pub mod artifact_preview;
pub mod catchup;
pub mod context;
pub mod generated;
pub mod goal_conversations;
pub mod goal_drafts;
pub mod navigation;
pub mod plans;
pub mod recorded_effects;
pub mod recovery;
pub mod results;
pub mod session_state;
pub mod settings;
pub mod status;
pub mod subagents;
mod unicode_casefold;

use chrono::{DateTime, SecondsFormat, Utc};
use nebula_assistant_domain::records::{AssistantKind, RecordError, StoredAssistantRecord};
use nebula_assistant_storage::entities::{
    Error as StorageError, Precondition, SqliteAssistantStore,
};
use serde_json::{Value, json};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("{0}")]
    Unavailable(&'static str),
    #[error("{0}")]
    Timeout(&'static str),
    #[error("{0}")]
    Invalid(&'static str),
    #[error("{0}")]
    NotFound(&'static str),
    #[error("{0}")]
    StorageNotFound(&'static str),
    #[error("{0}")]
    RetainedNotFound(String),
    #[error("{0}")]
    Conflict(&'static str),
    #[error("{0}")]
    DynamicConflict(String),
    #[error("{0}")]
    HistoryConflict(&'static str),
    #[error("Retained Assistant state cannot be projected")]
    LegacyUnhandled,
    #[error("{0}")]
    LegacyValueError(String),
    #[error("Retained Assistant state cannot be projected in its storage snapshot")]
    LegacyStorageUnhandled,
    #[error("Assistant model validation failed")]
    ModelValidation(Vec<Value>),
    #[error("Retained Assistant model validation failed")]
    RetainedModelValidation(nebula_assistant_domain::model_validation::ValidationReport),
    #[error("{kind} entity not found: {id}")]
    EntityNotFound { kind: &'static str, id: String },
    #[error("revision conflict: expected {expected}, found {found}")]
    RevisionConflict { expected: String, found: i64 },
    #[error(transparent)]
    Storage(#[from] StorageError),
    #[error(transparent)]
    Record(#[from] RecordError),
    #[error("assistant response could not be encoded")]
    Json(#[from] serde_json::Error),
}
pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone)]
pub struct AssistantRecords {
    store: SqliteAssistantStore,
    clock: fn() -> DateTime<Utc>,
}
impl AssistantRecords {
    pub fn new(store: SqliteAssistantStore) -> Self {
        Self {
            store,
            clock: Utc::now,
        }
    }
    /// Trusted dependency injection for deterministic compatibility fixtures.
    /// HTTP request bodies must never supply the clock.
    pub fn with_clock(store: SqliteAssistantStore, clock: fn() -> DateTime<Utc>) -> Self {
        Self { store, clock }
    }
    async fn get(&self, kind: AssistantKind, id: &str) -> Result<StoredAssistantRecord> {
        // Canonical identities cannot have this shape, so the Python lookup
        // result is a missing entity, not a retryable storage-capacity failure.
        if id.is_empty() || id.chars().count() > 200 {
            return Err(Error::EntityNotFound {
                kind: kind.as_str(),
                id: id.into(),
            });
        }
        self.store.get(kind, id).await.map_err(|error| match error {
            StorageError::NotFound => Error::EntityNotFound {
                kind: kind.as_str(),
                id: id.into(),
            },
            error => error.into(),
        })
    }
    fn create_record(
        &self,
        kind: AssistantKind,
        mut fields: Value,
    ) -> Result<StoredAssistantRecord> {
        fields["revision"] = json!(1);
        let now = (self.clock)();
        fields["created_at"] = timestamp(now, true).into();
        fields["updated_at"] = fields["created_at"].clone();
        Ok(StoredAssistantRecord::decode_persisted(
            kind,
            &serde_json::to_vec(&fields)?,
        )?)
    }
}

fn timestamp(time: DateTime<Utc>, z: bool) -> String {
    time.to_rfc3339_opts(
        if time.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        z,
    )
}
fn history_timestamp(value: &Value) -> Result<String> {
    let time = DateTime::parse_from_rfc3339(
        value
            .as_str()
            .ok_or(Error::Invalid("Recorded timestamp is missing"))?,
    )
    .map_err(|_| Error::Invalid("Recorded timestamp is invalid"))?;
    Ok(time.to_rfc3339_opts(
        if time.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        false,
    ))
}
fn guard(record: &StoredAssistantRecord) -> Result<Precondition> {
    Ok(Precondition {
        kind: record.kind(),
        id: record.payload()["id"]
            .as_str()
            .ok_or(StorageError::CorruptEnvelope)?
            .into(),
        revision: record.payload()["revision"]
            .as_i64()
            .ok_or(StorageError::CorruptEnvelope)?,
    })
}
