//! Retained activity, follow-up queue, and hook outcomes. These projections do
//! not reconcile uncertain effects or grant any execution capability.
use crate::{AssistantRecords, Error, Result, timestamp};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Utc};
use nebula_assistant_domain::records::{AssistantKind as Kind, RecordError};
use nebula_assistant_storage::entities::Error as StorageError;
use serde_json::{Value, json};
use std::collections::HashMap;

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|number| number != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn read_error(error: StorageError) -> Error {
    match error {
        StorageError::Record(RecordError::TooLarge) => StorageError::ReadLimit.into(),
        StorageError::Record(_) | StorageError::CorruptEnvelope => Error::LegacyUnhandled,
        error => error.into(),
    }
}
fn identity(kind: Kind, id: &str) -> Result<()> {
    if id.is_empty() || id.chars().count() > 200 {
        Err(missing(kind, id))
    } else {
        Ok(())
    }
}
fn missing(kind: Kind, id: &str) -> Error {
    Error::EntityNotFound {
        kind: kind.as_str(),
        id: id.into(),
    }
}
fn lookup_error(error: StorageError, kind: Kind, id: &str) -> Error {
    if matches!(error, StorageError::NotFound) {
        missing(kind, id)
    } else {
        read_error(error)
    }
}

// Counting serialization bounds encoded responses before they are retained in
// an array. It does not allocate a second copy of opaque queue/hook metadata.
struct ByteBudget(usize);
impl std::io::Write for ByteBudget {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > 16 * 1024 * 1024 {
            Err(std::io::Error::other(
                "Status response exceeds its byte limit",
            ))
        } else {
            Ok(bytes.len())
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl ByteBudget {
    fn charge(&mut self, value: &Value) -> Result<()> {
        serde_json::to_writer(self, value).map_err(|_| StorageError::ReadLimit.into())
    }
    fn push(&mut self, array: &mut Vec<Value>, value: Value) -> Result<()> {
        if array.len() >= 10_000 {
            return Err(StorageError::ReadLimit.into());
        }
        self.0 += usize::from(!array.is_empty());
        self.charge(&value)?;
        array.push(value);
        Ok(())
    }
}

// Aware hook timestamps sort by instant; naive timestamps remain a separate
// Python-compatible domain. Comparing those domains is a source error.
fn hook_time(value: &Value) -> Result<(bool, NaiveDateTime, String)> {
    let text = value.as_str().ok_or(Error::LegacyUnhandled)?;
    if let Ok(time) = DateTime::parse_from_rfc3339(text) {
        let output = time.to_rfc3339_opts(
            if time.timestamp_subsec_micros() == 0 {
                SecondsFormat::Secs
            } else {
                SecondsFormat::Micros
            },
            true,
        );
        Ok((true, time.naive_utc(), output))
    } else {
        let time = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f")
            .map_err(|_| Error::LegacyUnhandled)?;
        let output = time
            .format(if time.and_utc().timestamp_subsec_micros() == 0 {
                "%Y-%m-%dT%H:%M:%S"
            } else {
                "%Y-%m-%dT%H:%M:%S%.6f"
            })
            .to_string();
        Ok((false, time, output))
    }
}

impl AssistantRecords {
    pub async fn session_activity(&self, project: &str) -> Result<Value> {
        let snapshot = self
            .store
            .activity_snapshot(project)
            .await
            .map_err(read_error)?;
        let mut pending = HashMap::new();
        for turn in &snapshot.unfinished_turns {
            let p = turn.payload();
            if p["status"] == "interrupted" {
                let recovery = p["request_snapshot"].get("recovery");
                let is_pending = match recovery {
                    None => false,
                    Some(Value::Object(fields)) => {
                        fields.get("required").is_some_and(truthy)
                            || fields.get("automatic_retry_pending").is_some_and(truthy)
                    }
                    Some(_) => return Err(Error::LegacyUnhandled),
                };
                if !is_pending {
                    continue;
                }
            }
            let session_id = p["session_id"].as_str().ok_or(Error::LegacyUnhandled)?;
            if pending.insert(session_id, p).is_some() {
                return Err(Error::HistoryConflict(
                    "chat session has multiple active turns",
                ));
            }
        }
        // Defer session decode errors until after the project-wide pending
        // conflict check, matching Python's observable error precedence.
        let sessions = snapshot.sessions.map_err(read_error)?;
        let mut output = Vec::new();
        let mut budget = ByteBudget(2);
        for session in sessions {
            let p = session.payload();
            if p["metadata"]["temporary_assistant"] == true {
                continue;
            }
            let id = p["id"].as_str().ok_or(Error::LegacyUnhandled)?;
            let turn = pending.get(id);
            let state = match turn {
                None => "idle",
                Some(turn)
                    if !p["metadata"]["subagent_id"].is_string()
                        && matches!(
                            turn["status"].as_str(),
                            Some("waiting_approval" | "interrupted")
                        ) =>
                {
                    "waiting"
                }
                Some(_) => "working",
            };
            budget.push(
                &mut output,
                json!({"session_id":id,"state":state,
                "turn_id":turn.map(|turn| &turn["id"])}),
            )?;
        }
        Ok(output.into())
    }

    pub async fn saved_queue(&self, session: &str, now: DateTime<Utc>) -> Result<Value> {
        identity(Kind::Session, session)?;
        let snapshot = self
            .store
            .queue_snapshot(session)
            .await
            .map_err(|error| lookup_error(error, Kind::Session, session))?;
        let response = if let Some(queue) = snapshot.queue {
            queue.into_payload()
        } else {
            let id = format!("chat-queue-{session}");
            if id.chars().count() > 200 {
                return Err(Error::ModelValidation(vec![json!({
                    "type":"string_too_long","loc":["id"],
                    "msg":"String should have at most 200 characters",
                    "input":id,"ctx":{"max_length":200}
                })]));
            }
            let at = timestamp(now, true);
            json!({"id":id,"created_at":at,"updated_at":at,"revision":0,
                "engagement_id":snapshot.session.payload()["engagement_id"],
                "session_id":session,"paused":false,"resume_after_turn_id":null,"items":[]})
        };
        ByteBudget(0).charge(&response)?;
        Ok(response)
    }

    pub async fn turn_hooks(&self, turn: &str) -> Result<Value> {
        identity(Kind::Turn, turn)?;
        let snapshot = self
            .store
            .turn_hooks_snapshot(turn)
            .await
            .map_err(|error| lookup_error(error, Kind::Turn, turn))?;
        let turn_id = snapshot.turn.payload()["id"]
            .as_str()
            .ok_or(Error::LegacyUnhandled)?;
        let mut ordered = Vec::new();
        let mut awareness = None;
        for hook in &snapshot.hooks {
            let p = hook.payload();
            if p["chat_turn_id"] != turn_id {
                continue;
            }
            let (aware, time, started) = hook_time(&p["started_at"])?;
            if awareness.is_some_and(|previous| previous != aware) {
                return Err(Error::LegacyUnhandled);
            }
            awareness = Some(aware);
            ordered.push((
                time,
                p["id"].as_str().ok_or(Error::LegacyUnhandled)?,
                started,
                p,
            ));
        }
        ordered.sort_by(|a, b| (a.0, a.1).cmp(&(b.0, b.1)));
        let mut output = Vec::new();
        let mut budget = ByteBudget(2);
        for (_, _, started, p) in ordered {
            let completed = if p["completed_at"].is_null() {
                None
            } else {
                Some(hook_time(&p["completed_at"])?.2)
            };
            budget.push(&mut output, json!({"id":p["id"],"hook_id":p["hook_id"],"event_name":p["event_name"],
                "status":p["status"],"side_effects":p["side_effects"],"started_at":started,"completed_at":completed,
                "error":p["error"],"late_outcome_status":p["late_outcome"]["status"],
                "late_outcome_exit_code":p["late_outcome"]["exit_code"],"reconciliation":p["reconciliation"]}))?;
        }
        Ok(output.into())
    }
}
