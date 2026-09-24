//! Authoritative display state. Only the derived display watermark is written;
//! observing this endpoint never dispatches or reconciles work.
use crate::{AssistantRecords, Error, Result};
use nebula_assistant_domain::records::{AssistantKind, RecordError};
use nebula_assistant_storage::entities::{Error as StorageError, StateObservations};
use serde_json::Value;

fn read_error(error: Error) -> Error {
    match error {
        Error::Record(RecordError::TooLarge)
        | Error::Storage(StorageError::Record(RecordError::TooLarge)) => {
            StorageError::ReadLimit.into()
        }
        Error::Record(_)
        | Error::Storage(StorageError::Record(_) | StorageError::CorruptEnvelope) => {
            // Python's Database.session() records this failure as storage
            // before the API builds its sanitized unhandled-error envelope.
            Error::LegacyStorageUnhandled
        }
        error => error,
    }
}

impl AssistantRecords {
    pub async fn session_state(
        &self,
        session_id: &str,
        observations: StateObservations,
    ) -> Result<Value> {
        // Preserve the public lookup contract before the transactional projector
        // reloads identity. The latter must never use this potentially stale row.
        self.get(AssistantKind::Session, session_id)
            .await
            .map_err(read_error)?;
        self.store
            .session_state(session_id, observations)
            .await
            .map_err(|error| match error {
                StorageError::InvalidStateProjectionDuringWrite => Error::LegacyUnhandled,
                StorageError::NotFound => {
                    Error::RetainedNotFound(format!("chat session not found: {session_id}"))
                }
                error => read_error(error.into()),
            })
    }
}
