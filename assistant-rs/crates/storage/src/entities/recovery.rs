//! Bounded retained receipts and narrowly scoped recovery writes. No execution.
use super::*;
use chrono::{NaiveDateTime, Timelike};
use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};

pub struct RecoveryTurn {
    pub record: StoredAssistantRecord,
    /// Original spelling keeps opaque callback values' Python rendering stable.
    pub raw_payload: String,
}
impl std::fmt::Debug for RecoveryTurn {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RecoveryTurn")
            .field("record", &self.record)
            .finish_non_exhaustive()
    }
}
#[derive(Debug)]
pub struct RecoveryTurnsSnapshot {
    pub session: StoredAssistantRecord,
    /// Complete ascending created_at/id collection; services choose ownership.
    pub turns: Vec<RecoveryTurn>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RecoveryReceiptKind {
    Tool,
    Hook,
}
impl RecoveryReceiptKind {
    fn kind(self) -> DependencyKind {
        match self {
            Self::Tool => DependencyKind::ToolCall,
            Self::Hook => DependencyKind::NativeHookExecution,
        }
    }
    fn field(self) -> &'static str {
        match self {
            Self::Tool => "unknown_tool_call_ids",
            Self::Hook => "unknown_hook_execution_ids",
        }
    }
}
#[derive(Debug)]
pub struct RecoveryReceiptRow {
    pub id: String,
    /// Errors remain in source reference order, including duplicate references.
    pub record: Result<Option<StoredDependency>>,
}
#[derive(Debug)]
pub struct RecoveryReceiptSnapshot {
    pub turn: RecoveryTurn,
    pub receipts: Vec<RecoveryReceiptRow>,
}
#[derive(Clone, Debug)]
pub struct HookAdoption {
    pub id: String,
    pub expected_revision: i64,
}
pub(super) struct RepairRequest {
    id: String,
    expected_revision: i64,
    changes: Map<String, Value>,
    kind: RecoveryReceiptKind,
    hooks: Vec<HookAdoption>,
    clock: Arc<StateClock>,
    _bytes: OwnedSemaphorePermit,
    pub(super) reply: oneshot::Sender<Result<RecoveryTurn>>,
}
#[derive(Default)]
struct Budget {
    rows: usize,
    bytes: usize,
}
impl Budget {
    fn add(&mut self, bytes: usize) -> Result<()> {
        self.rows += 1;
        self.bytes = self.bytes.saturating_add(bytes);
        if self.rows > 10_000 || self.bytes > MAX_TRANSACTION_BYTES {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
    fn charge(&mut self, row: &SqliteRow, raw_copy: bool) -> Result<()> {
        let bytes: i64 = row.try_get("payload_bytes")?;
        if bytes < 0 || bytes as u64 > MAX_RECORD_BYTES as u64 {
            return Err(RecordError::TooLarge.into());
        }
        self.add(bytes as usize * if raw_copy { 2 } else { 1 })
    }
}

impl SqliteAssistantStore {
    pub async fn unfinished_turns_snapshot(
        &self,
        session_id: &str,
    ) -> Result<RecoveryTurnsSnapshot> {
        self.recovery_turns(session_id, true, false).await
    }
    pub async fn session_turns_snapshot(&self, session_id: &str) -> Result<RecoveryTurnsSnapshot> {
        self.recovery_turns(session_id, false, false).await
    }
    /// Fork uses complete wrapped Session hydration without broadening other
    /// retained readers. Turn selection and effect reconciliation stay shared.
    pub async fn fork_unfinished_turns_snapshot(
        &self,
        session_id: &str,
    ) -> Result<RecoveryTurnsSnapshot> {
        self.recovery_turns(session_id, true, true).await
    }
    async fn recovery_turns(
        &self,
        session_id: &str,
        unfinished: bool,
        fork_session: bool,
    ) -> Result<RecoveryTurnsSnapshot> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let row = sqlx::query(&format!(
            "{SELECT_RECORD} WHERE kind='chat_sessions' AND id=?"
        ))
        .bind(session_id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or(Error::NotFound)?;
        budget.charge(&row, false)?;
        let session = if fork_session {
            let result = fork::recovery_session(&row);
            let additional = match &result {
                Ok(record) => fork::size(record.payload())?,
                Err(Error::WrappedRecord(RecordError::ModelValidation(report))) => {
                    report.retained_bytes()
                }
                _ => 0,
            };
            budget.bytes = budget.bytes.saturating_add(additional);
            if budget.bytes > MAX_TRANSACTION_BYTES {
                return Err(Error::ReadLimit);
            }
            result?
        } else {
            decode_row(row)?
        };
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind='chat_turns' AND chat_session_id=")
            .push_bind(session_id);
        if unfinished {
            q.push(" AND json_extract(payload,'$.status') IN ('routing','waiting_approval','waiting_callback','finalizing','interrupted')");
        }
        q.push(" ORDER BY created_at,id LIMIT 10001");
        let statement = q.build();
        let mut rows = statement.fetch(&mut *tx);
        let mut turns = Vec::new();
        while let Some(row) = rows.try_next().await? {
            turns.push(decode_turn(row, &mut budget)?);
        }
        drop(rows);
        tx.commit().await?;
        Ok(RecoveryTurnsSnapshot { session, turns })
    }
    pub async fn recovery_receipts_snapshot(
        &self,
        turn_id: &str,
        kind: RecoveryReceiptKind,
    ) -> Result<RecoveryReceiptSnapshot> {
        validate_id(turn_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let turn = turn(&mut tx, &mut budget, turn_id).await?;
        let p = turn.record.payload();
        let mut ids = Vec::new();
        if p["status"] == "interrupted"
            && let Some(recovery) = p["request_snapshot"]["recovery"].as_object()
            && let Some(unknown) = recovery.get(kind.field()).and_then(Value::as_array)
        {
            for reference in unknown {
                let id = reference.as_str();
                // Ignored values still consume an entry; their payload bytes
                // are already included in the retained Turn's shared budget.
                budget.add(id.map_or(0, str::len))?;
                if let Some(id) = id {
                    ids.push(id);
                }
            }
        }
        let mut receipts = Vec::new();
        if !ids.is_empty() {
            let parameters = serde_json::to_string(&ids)?;
            budget.add(parameters.len())?;
            // Retain reference positions, including missing, wrong-kind and
            // duplicate IDs. Sort only after shared-budget checks so SQLite
            // cannot materialize duplicated payloads in an unbounded sorter.
            let sql = "WITH refs AS (SELECT CAST(key AS INTEGER) AS position,value AS requested_id FROM json_each(?)) SELECT refs.position AS requested_position,refs.requested_id,entities.id,entities.kind,entities.engagement_id,entities.revision,entities.chat_session_id,entities.created_at,entities.updated_at,length(CAST(entities.payload AS BLOB)) AS payload_bytes,CASE WHEN length(CAST(entities.payload AS BLOB))<=16777216 THEN entities.payload ELSE NULL END AS payload FROM refs LEFT JOIN entities ON entities.id=refs.requested_id AND entities.kind=?";
            let statement = sqlx::query(sql).bind(parameters).bind(kind.kind().as_str());
            let mut rows = statement.fetch(&mut *tx);
            while let Some(row) = rows.try_next().await? {
                let position: i64 = row.try_get("requested_position")?;
                let id: String = row.try_get("requested_id")?;
                let record = if row.try_get::<Option<&str>, _>("id")?.is_none() {
                    Ok(None)
                } else {
                    budget.charge(&row, false)?;
                    decode_dependency(&row, kind.kind()).map(Some)
                };
                receipts.push((position, RecoveryReceiptRow { id, record }));
            }
        }
        tx.commit().await?;
        receipts.sort_unstable_by_key(|(position, _)| *position);
        let receipts = receipts.into_iter().map(|(_, receipt)| receipt).collect();
        Ok(RecoveryReceiptSnapshot { turn, receipts })
    }
    /// Only recorded terminal ToolCall evidence is projected by the service;
    /// this transaction deliberately adds no new receipt CAS or execution claim.
    pub async fn adopt_recorded_tools(
        &self,
        turn_id: &str,
        expected_revision: i64,
        turn_changes: Map<String, Value>,
        clock: Arc<StateClock>,
    ) -> Result<RecoveryTurn> {
        self.adopt(
            turn_id,
            expected_revision,
            turn_changes,
            RecoveryReceiptKind::Tool,
            Vec::new(),
            clock,
        )
        .await
    }
    /// Hook changes are derived from freshly validated saved outcomes; callers
    /// cannot patch arbitrary dependency fields through this API.
    pub async fn adopt_recorded_hooks(
        &self,
        turn_id: &str,
        expected_revision: i64,
        turn_changes: Map<String, Value>,
        late_hooks: Vec<HookAdoption>,
        clock: Arc<StateClock>,
    ) -> Result<RecoveryTurn> {
        self.adopt(
            turn_id,
            expected_revision,
            turn_changes,
            RecoveryReceiptKind::Hook,
            late_hooks,
            clock,
        )
        .await
    }
    async fn adopt(
        &self,
        id: &str,
        expected_revision: i64,
        changes: Map<String, Value>,
        kind: RecoveryReceiptKind,
        hooks: Vec<HookAdoption>,
        clock: Arc<StateClock>,
    ) -> Result<RecoveryTurn> {
        validate_id(id)?;
        if expected_revision < 1 {
            return Err(Error::InvalidBounds);
        }
        let allowed = match kind {
            RecoveryReceiptKind::Tool => &[
                "tool_call_ids",
                "tool_history",
                "next_step",
                "execution_tool_calls",
                "artifact_queries",
                "error",
                "request_snapshot",
            ][..],
            RecoveryReceiptKind::Hook => &["error", "request_snapshot"][..],
        };
        if changes.is_empty() || changes.keys().any(|key| !allowed.contains(&key.as_str())) {
            return Err(Error::ProtectedField);
        }
        if hooks.len() > 10_000 {
            return Err(Error::TransactionLimit);
        }
        let mut size = id.len() + serde_json::to_vec(&changes)?.len() + 128;
        for hook in &hooks {
            validate_id(&hook.id)?;
            if hook.expected_revision < 1 {
                return Err(Error::InvalidBounds);
            }
            size = size.saturating_add(hook.id.len() + 16);
        }
        if size > MAX_TRANSACTION_BYTES {
            return Err(Error::TransactionLimit);
        }
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        let bytes = self
            .bytes
            .clone()
            .try_acquire_many_owned(size as u32)
            .map_err(|_| Error::Capacity)?;
        let slot = self.commands.try_reserve().map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        slot.send(Command::Recover(RepairRequest {
            id: id.into(),
            expected_revision,
            changes,
            kind,
            hooks,
            clock,
            _bytes: bytes,
            reply,
        }));
        result.await.map_err(|_| Error::Closed)?
    }
}

fn decode_turn(row: SqliteRow, budget: &mut Budget) -> Result<RecoveryTurn> {
    budget.charge(&row, true)?;
    let record = decode_row_ref(&row)?;
    let raw_payload = row.try_get("payload")?;
    Ok(RecoveryTurn {
        record,
        raw_payload,
    })
}
async fn turn(
    connection: &mut SqliteConnection,
    budget: &mut Budget,
    id: &str,
) -> Result<RecoveryTurn> {
    let row = sqlx::query(&format!("{SELECT_RECORD} WHERE kind='chat_turns' AND id=?"))
        .bind(id)
        .fetch_optional(connection)
        .await?
        .ok_or(Error::NotFound)?;
    decode_turn(row, budget)
}
fn decode_dependency(row: &SqliteRow, kind: DependencyKind) -> Result<StoredDependency> {
    let record = StoredDependency::decode(kind, row.try_get::<&str, _>("payload")?.as_bytes())?;
    let p = record.payload();
    if p["id"].as_str() != Some(row.try_get("id")?)
        || p["revision"].as_i64() != Some(row.try_get("revision")?)
        || p["engagement_id"].as_str() != row.try_get::<Option<&str>, _>("engagement_id")?
        || p["chat_session_id"].as_str() != row.try_get::<Option<&str>, _>("chat_session_id")?
        || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
        || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
    {
        return Err(Error::CorruptEnvelope);
    }
    Ok(record)
}
fn stamp(now: DateTime<Utc>) -> String {
    now.to_rfc3339_opts(
        if now.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        true,
    )
}
fn recorded_timestamp(value: &Value) -> Result<Value> {
    let raw = value.as_str().ok_or(Error::CorruptEnvelope)?;
    let text = if let Ok(time) = DateTime::parse_from_rfc3339(raw) {
        time.to_rfc3339_opts(
            if time.timestamp_subsec_micros() == 0 {
                SecondsFormat::Secs
            } else {
                SecondsFormat::Micros
            },
            true,
        )
    } else {
        let time = NaiveDateTime::parse_from_str(raw, "%Y-%m-%dT%H:%M:%S%.f")
            .map_err(|_| Error::CorruptEnvelope)?;
        time.format(if time.nanosecond() / 1000 == 0 {
            "%Y-%m-%dT%H:%M:%S"
        } else {
            "%Y-%m-%dT%H:%M:%S%.6f"
        })
        .to_string()
    };
    Ok(text.into())
}
pub(super) async fn repair(
    connection: &mut SqliteConnection,
    request: &RepairRequest,
) -> Result<RecoveryTurn> {
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    let mut budget = Budget::default();
    budget.add(serde_json::to_vec(&request.changes)?.len())?;
    for adoption in &request.hooks {
        budget.add(adoption.id.len() + 16)?;
        let row = sqlx::query(&format!(
            "{SELECT_RECORD} WHERE kind='native_hook_executions' AND id=?"
        ))
        .bind(&adoption.id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(|| Error::RecoveryHookNotFound(adoption.id.clone()))?;
        budget.charge(&row, false)?;
        let current = decode_dependency(&row, DependencyKind::NativeHookExecution)?;
        let p = current.payload();
        let revision = p["revision"].as_i64().ok_or(Error::CorruptEnvelope)?;
        if revision != adoption.expected_revision {
            return Err(Error::RevisionConflict {
                expected: adoption.expected_revision,
                found: revision,
            });
        }
        let outcome = &p["late_outcome"];
        if p["chat_turn_id"] != request.id
            || p["status"] != "interrupted"
            || !outcome.is_object()
            || outcome["status"] != "complete"
            || outcome["exit_code"].as_i64() != Some(0)
        {
            return Err(Error::Conflict);
        }
        let mut next = p.clone();
        next["status"] = "complete".into();
        next["completed_at"] = recorded_timestamp(&outcome["observed_at"])?;
        next["exit_code"] = outcome["exit_code"].clone();
        next["stdout"] = outcome["stdout"].clone();
        next["stderr"] = outcome["stderr"].clone();
        next["error"] = Value::Null;
        next["revision"] = revision
            .checked_add(1)
            .ok_or(Error::RevisionExhausted)?
            .into();
        next["updated_at"] = stamp((request.clock)()).into();
        let raw = serde_json::to_string(&next)?;
        budget.add(raw.len())?;
        let validated =
            StoredDependency::decode(DependencyKind::NativeHookExecution, raw.as_bytes())?;
        let updated=sqlx::query("UPDATE entities SET payload=?,revision=?,updated_at=? WHERE id=? AND kind='native_hook_executions' AND revision=?")
            .bind(&raw).bind(validated.payload()["revision"].as_i64()).bind(sql_time(&validated.payload()["updated_at"])?).bind(&adoption.id).bind(revision).execute(&mut *tx).await?;
        if updated.rows_affected() != 1 {
            return Err(Error::Conflict);
        }
    }
    // Source transaction order is hooks first, Turn CAS last. Repeated late
    // hook references therefore conflict and roll the entire transaction back.
    let current = turn(&mut tx, &mut budget, &request.id).await?;
    let revision = record_revision(&current.record)?;
    if revision != request.expected_revision {
        return Err(Error::RevisionConflict {
            expected: request.expected_revision,
            found: revision,
        });
    }
    if current.record.payload()["status"] != "interrupted"
        || !current.record.payload()["request_snapshot"]["recovery"].is_object()
        || current.record.payload()["request_snapshot"]["recovery"][request.kind.field()]
            .as_array()
            .is_none_or(Vec::is_empty)
    {
        return Err(Error::Conflict);
    }
    let mut next = current.record.into_payload();
    next.as_object_mut()
        .ok_or(Error::CorruptEnvelope)?
        .extend(request.changes.clone());
    next["revision"] = revision
        .checked_add(1)
        .ok_or(Error::RevisionExhausted)?
        .into();
    next["updated_at"] = stamp((request.clock)()).into();
    let raw_payload =
        nebula_assistant_domain::retained_json::repair_turn_json(&current.raw_payload, &next)?;
    budget.add(raw_payload.len() * 2)?;
    let record =
        StoredAssistantRecord::decode_persisted(AssistantKind::Turn, raw_payload.as_bytes())?;
    let changed=sqlx::query("UPDATE entities SET payload=?,revision=?,updated_at=? WHERE id=? AND kind='chat_turns' AND revision=?")
        .bind(&raw_payload).bind(record_revision(&record)?).bind(sql_time(&record.payload()["updated_at"])?).bind(&request.id).bind(revision).execute(&mut *tx).await?;
    if changed.rows_affected() != 1 {
        return Err(Error::Conflict);
    }
    update_search(&mut tx, &record).await?;
    tx.commit().await?;
    Ok(RecoveryTurn {
        record,
        raw_payload,
    })
}
