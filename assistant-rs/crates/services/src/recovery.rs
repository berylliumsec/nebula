//! Reconcile trustworthy saved effects when a client observes pending work.
//! These methods never resume a turn, run a hook, or invoke a tool/provider.
use crate::{AssistantRecords, Error, Result, recorded_effects};
use nebula_assistant_domain::{
    dependencies::StoredDependency,
    records::{AssistantKind, RecordError},
};
use nebula_assistant_storage::entities::{
    Error as StorageError, HookAdoption, RecoveryReceiptKind, RecoveryReceiptSnapshot, RecoveryTurn,
};
use num_bigint::BigInt;
use serde_json::{Value, json};
use std::sync::Arc;

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|n| n != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}
fn missing(kind: AssistantKind, id: &str) -> Error {
    Error::EntityNotFound {
        kind: kind.as_str(),
        id: id.into(),
    }
}
fn identity(id: &str) -> Result<()> {
    if id.is_empty() || id.chars().count() > 200 {
        return Err(missing(AssistantKind::Session, id));
    }
    Ok(())
}
fn read_error(error: StorageError) -> Error {
    match error {
        StorageError::RecoveryHookNotFound(id) => Error::EntityNotFound {
            kind: "native_hook_executions",
            id,
        },
        StorageError::Record(RecordError::TooLarge) => StorageError::ReadLimit.into(),
        StorageError::Record(_) | StorageError::CorruptEnvelope => Error::LegacyStorageUnhandled,
        error => error.into(),
    }
}
fn lookup_error(error: StorageError, kind: AssistantKind, id: &str) -> Error {
    if matches!(error, StorageError::NotFound) {
        missing(kind, id)
    } else {
        read_error(error)
    }
}
fn retryable(error: &StorageError) -> bool {
    matches!(
        error,
        StorageError::Conflict | StorageError::RevisionConflict { .. }
    )
}

fn is_pending(turn: &RecoveryTurn) -> Result<bool> {
    let p = turn.record.payload();
    if p["status"] != "interrupted" {
        return Ok(true);
    }
    match p["request_snapshot"].get("recovery") {
        None => Ok(false),
        Some(Value::Object(recovery)) => Ok(recovery.get("required").is_some_and(truthy)
            || recovery.get("automatic_retry_pending").is_some_and(truthy)),
        Some(_) => Err(Error::LegacyUnhandled),
    }
}
fn latest(turns: Vec<RecoveryTurn>) -> Result<Option<RecoveryTurn>> {
    // Entity timestamps are normalized by the retained decoder. Compare their
    // instants, not variable-width RFC3339 strings.
    let mut selected = None;
    let mut selected_key = None;
    for turn in turns {
        let p = turn.record.payload();
        let time = chrono::DateTime::parse_from_rfc3339(
            p["created_at"].as_str().ok_or(Error::LegacyUnhandled)?,
        )
        .map_err(|_| Error::LegacyUnhandled)?
        .timestamp_micros();
        let key = (
            time,
            p["id"].as_str().ok_or(Error::LegacyUnhandled)?.to_owned(),
        );
        if selected_key.as_ref().is_none_or(|previous| &key > previous) {
            selected_key = Some(key);
            selected = Some(turn);
        }
    }
    Ok(selected)
}
fn failed_answer(turn: &RecoveryTurn) -> bool {
    let p = turn.record.payload();
    if p["status"] != "failed" || !p["final_message_id"].is_null() {
        return false;
    }
    let Some(Value::Number(attempts)) =
        p["request_snapshot"]["final_answer_recovery"].get("attempts")
    else {
        return false;
    };
    // Python bool is excluded and float 2.0 is not an int. Arbitrary integer
    // counters are valid even when they exceed the platform's integer range.
    attempts
        .to_string()
        .parse::<BigInt>()
        .is_ok_and(|n| n >= BigInt::from(2))
}

struct Lookup {
    id: String,
    record: Option<StoredDependency>,
    error: Option<Error>,
}
fn inputs(snapshot: RecoveryReceiptSnapshot) -> (RecoveryTurn, Vec<Lookup>) {
    let rows = snapshot
        .receipts
        .into_iter()
        .map(|row| {
            let (record, error) = match row.record {
                Ok(record) => (record, None),
                Err(error) => (None, Some(read_error(error))),
            };
            Lookup {
                id: row.id,
                record,
                error,
            }
        })
        .collect();
    (snapshot.turn, rows)
}

struct ResponseBudget(usize);
impl std::io::Write for ResponseBudget {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > 16 * 1024 * 1024 {
            return Err(std::io::Error::other(
                "recovery response exceeds byte bound",
            ));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn summary(turn: RecoveryTurn) -> Result<Value> {
    let p = turn.record.payload();
    let recovery = &p["request_snapshot"]["recovery"];
    let tools = recovery
        .get("unknown_tool_call_ids")
        .and_then(Value::as_array);
    let hooks = recovery
        .get("unknown_hook_execution_ids")
        .and_then(Value::as_array);
    let strings = |values: Option<&Vec<Value>>| -> Vec<Value> {
        values
            .into_iter()
            .flatten()
            .filter(|v| v.is_string())
            .cloned()
            .collect()
    };
    let (results_url, process_id) = crate::subagents::callback_fields(&turn.raw_payload)?;
    let output = json!({
        "id":p["id"],"session_id":p["session_id"],"started_at":p["created_at"],
        "status":p["status"],"content":p["content"],"reasoning":p["reasoning"],
        "approval_id":p["approval_id"],"harness_turn_id":p["harness_turn_id"],
        "tool_call_ids":p["tool_call_ids"],"revision":p["revision"],"error":p["error"],
        "recovery_blocked":tools.is_some_and(|v| !v.is_empty()) || hooks.is_some_and(|v| !v.is_empty()),
        "unresolved_tool_call_ids":strings(tools),"unresolved_hook_execution_ids":strings(hooks),
        "results_url":results_url,"process_id":process_id
    });
    serde_json::to_writer(&mut ResponseBudget(0), &output).map_err(|_| StorageError::ReadLimit)?;
    Ok(output)
}

impl AssistantRecords {
    async fn reconcile_tools(&self, turn_id: &str) -> Result<RecoveryTurn> {
        for _ in 0..3 {
            let snapshot = self
                .store
                .recovery_receipts_snapshot(turn_id, RecoveryReceiptKind::Tool)
                .await
                .map_err(|e| lookup_error(e, AssistantKind::Turn, turn_id))?;
            let (turn, mut rows) = inputs(snapshot);
            let mut lookups = rows.iter_mut();
            let changes = recorded_effects::tools(&turn.record, |id| {
                let row = lookups.next().ok_or(Error::LegacyUnhandled)?;
                if row.id != id {
                    return Err(Error::LegacyUnhandled);
                }
                if let Some(error) = row.error.take() {
                    return Err(error);
                }
                Ok(row.record.as_ref())
            })?;
            let Some(changes) = changes else {
                return Ok(turn);
            };
            let revision = turn.record.payload()["revision"]
                .as_i64()
                .ok_or(Error::LegacyUnhandled)?;
            match self
                .store
                .adopt_recorded_tools(turn_id, revision, changes, Arc::new(self.clock))
                .await
            {
                Ok(turn) => return Ok(turn),
                Err(error) if retryable(&error) => continue,
                Err(error) => return Err(lookup_error(error, AssistantKind::Turn, turn_id)),
            }
        }
        Err(Error::HistoryConflict(
            "interrupted response changed while recovering recorded effects; reload",
        ))
    }

    async fn reconcile_hooks(&self, turn_id: &str) -> Result<RecoveryTurn> {
        for _ in 0..3 {
            let snapshot = self
                .store
                .recovery_receipts_snapshot(turn_id, RecoveryReceiptKind::Hook)
                .await
                .map_err(|e| lookup_error(e, AssistantKind::Turn, turn_id))?;
            let (turn, mut rows) = inputs(snapshot);
            let mut lookups = rows.iter_mut();
            let repair = recorded_effects::hooks(&turn.record, |id| {
                let row = lookups.next().ok_or(Error::LegacyUnhandled)?;
                if row.id != id {
                    return Err(Error::LegacyUnhandled);
                }
                if let Some(error) = row.error.take() {
                    return Err(error);
                }
                Ok(row.record.as_ref())
            })?;
            let Some(repair) = repair else {
                return Ok(turn);
            };
            let revision = turn.record.payload()["revision"]
                .as_i64()
                .ok_or(Error::LegacyUnhandled)?;
            let late_hooks = repair
                .late_hooks
                .into_iter()
                .map(|hook| HookAdoption {
                    id: hook.id,
                    expected_revision: hook.expected_revision,
                })
                .collect();
            match self
                .store
                .adopt_recorded_hooks(
                    turn_id,
                    revision,
                    repair.turn_changes,
                    late_hooks,
                    Arc::new(self.clock),
                )
                .await
            {
                Ok(turn) => return Ok(turn),
                Err(error) if retryable(&error) => continue,
                Err(error) => return Err(lookup_error(error, AssistantKind::Turn, turn_id)),
            }
        }
        Err(Error::HistoryConflict(
            "interrupted hook state changed while recovering recorded effects; reload",
        ))
    }

    /// The two commits deliberately remain ordered and separate. A failed hook
    /// repair must not erase a tool result that was already durably adopted.
    pub async fn reconcile_recorded_effects(&self, turn_id: &str) -> Result<RecoveryTurn> {
        self.reconcile_tools(turn_id).await?;
        self.reconcile_hooks(turn_id).await
    }

    async fn pending_record(&self, session_id: &str) -> Result<Option<RecoveryTurn>> {
        identity(session_id)?;
        let snapshot = self
            .store
            .unfinished_turns_snapshot(session_id)
            .await
            .map_err(|e| lookup_error(e, AssistantKind::Session, session_id))?;
        self.pending_from_turns(snapshot.turns).await
    }

    async fn pending_from_turns(&self, turns: Vec<RecoveryTurn>) -> Result<Option<RecoveryTurn>> {
        let mut selected = None;
        // Python constructs the entire filtered list before checking conflicts.
        // A malformed later recovery object must still surface first.
        let mut count = 0;
        for turn in turns {
            if is_pending(&turn)? {
                count += 1;
                selected = Some(turn);
            }
        }
        if count > 1 {
            return Err(Error::HistoryConflict(
                "chat session has multiple active turns",
            ));
        }
        match selected {
            Some(turn) => self
                .reconcile_recorded_effects(
                    turn.record.payload()["id"]
                        .as_str()
                        .ok_or(Error::LegacyUnhandled)?,
                )
                .await
                .map(Some),
            None => Ok(None),
        }
    }

    pub async fn pending_turn(&self, session_id: &str) -> Result<Value> {
        let turn = match self.pending_record(session_id).await? {
            Some(turn) => Some(turn),
            None => {
                let snapshot = self
                    .store
                    .session_turns_snapshot(session_id)
                    .await
                    .map_err(|e| lookup_error(e, AssistantKind::Session, session_id))?;
                latest(snapshot.turns)?.filter(failed_answer)
            }
        };
        turn.map(summary)
            .transpose()
            .map(|v| v.unwrap_or(Value::Null))
    }

    /// Mutation guards use the internal selector, without the read endpoint's
    /// failed-answer fallback or display projection. Selection may repair saved
    /// effects before the caller decides whether its own write is permitted.
    pub(crate) async fn has_pending_turn(&self, session_id: &str) -> Result<bool> {
        self.pending_record(session_id)
            .await
            .map(|turn| turn.is_some())
    }

    pub(crate) async fn has_pending_fork(&self, session_id: &str) -> Result<bool> {
        identity(session_id)?;
        let snapshot = self
            .store
            .fork_unfinished_turns_snapshot(session_id)
            .await
            .map_err(|error| match error {
                StorageError::WrappedRecord(_) => Error::LegacyStorageUnhandled,
                error => lookup_error(error, AssistantKind::Session, session_id),
            })?;
        self.pending_from_turns(snapshot.turns)
            .await
            .map(|turn| turn.is_some())
    }

    pub async fn session_hooks(&self, session_id: &str) -> Result<Value> {
        let turn = match self.pending_record(session_id).await? {
            Some(turn) => Some(turn),
            None => {
                let snapshot = self
                    .store
                    .session_turns_snapshot(session_id)
                    .await
                    .map_err(|e| lookup_error(e, AssistantKind::Session, session_id))?;
                latest(snapshot.turns)?
            }
        };
        match turn {
            Some(turn) => {
                self.turn_hooks(
                    turn.record.payload()["id"]
                        .as_str()
                        .ok_or(Error::LegacyUnhandled)?,
                )
                .await
            }
            None => Ok(json!([])),
        }
    }
}
