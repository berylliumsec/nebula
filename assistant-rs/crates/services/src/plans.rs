//! Read-only goals, child plans and schedules. Live elapsed time changes only
//! the response: durable claims, revisions, budgets and next runs are retained.
use crate::{AssistantRecords, Error, Result};
use chrono::{DateTime, Timelike, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, RecordError};
use nebula_assistant_storage::entities::Error as StorageError;
use serde_json::{Value, json};

fn valid_identity(id: &str) -> bool {
    !id.is_empty() && id.chars().count() <= 200
}
fn missing_session(id: &str) -> Error {
    Error::EntityNotFound {
        kind: Kind::Session.as_str(),
        id: id.into(),
    }
}
fn read_error(error: StorageError) -> Error {
    match error {
        StorageError::RetainedModelValidation(report) => Error::RetainedModelValidation(report),
        StorageError::WrappedRecord(_) => Error::LegacyStorageUnhandled,
        StorageError::Record(RecordError::TooLarge) => StorageError::ReadLimit.into(),
        StorageError::Record(_) | StorageError::CorruptEnvelope => Error::LegacyUnhandled,
        error => error.into(),
    }
}
fn session_error(error: StorageError, session: &str) -> Error {
    if matches!(error, StorageError::NotFound) {
        missing_session(session)
    } else {
        read_error(error)
    }
}

struct ResponseBudget(usize);
impl std::io::Write for ResponseBudget {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > 16 * 1024 * 1024 {
            return Err(std::io::Error::other(
                "Retained plan response exceeds its byte bound",
            ));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl ResponseBudget {
    fn charge(&mut self, value: &Value) -> Result<()> {
        serde_json::to_writer(self, value).map_err(|_| StorageError::ReadLimit.into())
    }
}

impl AssistantRecords {
    pub async fn session_goal(&self, session: &str, now: DateTime<Utc>) -> Result<Value> {
        if !valid_identity(session) {
            return Err(missing_session(session));
        }
        let snapshot = self
            .store
            .session_plans_snapshot(Kind::Goal, session)
            .await
            .map_err(|error| session_error(error, session))?;
        if snapshot.records.len() > 1 {
            return Err(Error::Conflict(
                "conversation has more than one authoritative goal",
            ));
        }
        let mut goal = snapshot
            .records
            .into_iter()
            .next()
            .ok_or_else(|| {
                Error::RetainedNotFound(format!("chat goal not found for session: {session}"))
            })?
            .into_payload();
        if goal["status"] == "running" && !goal["active_since"].is_null() {
            // Python datetime arithmetic has microsecond resolution. A naive
            // active_since cannot be compared with the trusted aware clock.
            let since = DateTime::parse_from_rfc3339(
                goal["active_since"]
                    .as_str()
                    .ok_or(Error::LegacyUnhandled)?,
            )
            .map_err(|_| Error::LegacyUnhandled)?;
            let now = now
                .with_nanosecond(now.nanosecond() / 1000 * 1000)
                .ok_or(Error::LegacyUnhandled)?;
            let micros = now
                .signed_duration_since(since)
                .num_microseconds()
                .ok_or(Error::LegacyUnhandled)?;
            let elapsed = goal["elapsed_seconds"]
                .as_f64()
                .ok_or(Error::LegacyUnhandled)?
                + (micros as f64 / 1_000_000.0).max(0.0);
            goal["elapsed_seconds"] = serde_json::Number::from_f64(elapsed)
                .ok_or(Error::LegacyUnhandled)?
                .into();
        }
        ResponseBudget(0).charge(&goal)?;
        Ok(goal)
    }

    pub async fn goal_children(&self, session: &str) -> Result<Value> {
        // Legacy children treats a missing parent session like an absent goal.
        if !valid_identity(session) {
            return Ok(json!([]));
        }
        let snapshot = match self.store.goal_children_snapshot(session).await {
            Ok(snapshot) => snapshot,
            Err(StorageError::NotFound) => return Ok(json!([])),
            Err(error) => return Err(read_error(error)),
        };
        if snapshot.goals.len() > 1 {
            return Err(Error::Conflict(
                "conversation has more than one authoritative goal",
            ));
        }
        if snapshot.goals.is_empty() {
            return Ok(json!([]));
        }
        let mut items = Vec::new();
        let mut budget = ResponseBudget(2);
        for child in snapshot.children {
            if items.len() >= 10_000 {
                return Err(StorageError::ReadLimit.into());
            }
            let child = child.into_payload();
            budget.0 += usize::from(!items.is_empty());
            budget.charge(&child)?;
            items.push(child);
        }
        Ok(items.into())
    }

    pub async fn session_schedule(&self, session: &str) -> Result<Value> {
        if !valid_identity(session) {
            return Err(missing_session(session));
        }
        let snapshot = self
            .store
            .session_plans_snapshot(Kind::Schedule, session)
            .await
            .map_err(|error| session_error(error, session))?;
        let schedule = snapshot
            .records
            .into_iter()
            .next()
            .ok_or_else(|| {
                Error::RetainedNotFound(format!("chat schedule not found for session: {session}"))
            })?
            .into_payload();
        ResponseBudget(0).charge(&schedule)?;
        Ok(schedule)
    }
}
