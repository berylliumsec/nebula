//! Retained child-agent view inputs. Reads never dispatch or deliver messages.
use super::*;
use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};
use std::collections::BTreeMap;

#[derive(Debug)]
pub struct SubagentSnapshot {
    pub session: StoredAssistantRecord,
    /// Source collection order: created_at, id. Services sort by started_at, id
    /// before consuming the deferred dependencies in Python projection order.
    pub records: Vec<SubagentViewRow>,
}

#[derive(Debug)]
pub struct SubagentViewRow {
    pub record: StoredAssistantRecord,
    pub turn: Result<Option<RawSubagentTurn>>,
    pub approval: Result<Option<StoredDependency>>,
    pub messages: Result<Vec<StoredAssistantRecord>>,
    pub child_session: Result<Option<StoredAssistantRecord>>,
}

pub struct RawSubagentTurn {
    pub record: StoredAssistantRecord,
    /// Preserves object insertion order for Python's str(history entry) view.
    /// This additional retained copy is charged to the snapshot byte budget.
    pub raw: String,
}
impl std::fmt::Debug for RawSubagentTurn {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RawSubagentTurn")
            .field("record", &self.record)
            .finish_non_exhaustive()
    }
}

#[derive(Default)]
struct Budget {
    bytes: usize,
    rows: usize,
}
impl Budget {
    fn charge(&mut self, row: &SqliteRow) -> Result<()> {
        self.rows += 1;
        if self.rows > 10_000 {
            return Err(Error::ReadLimit);
        }
        self.bytes(row)
    }

    fn bytes(&mut self, row: &SqliteRow) -> Result<()> {
        let bytes: i64 = row.try_get("payload_bytes")?;
        if bytes < 0 || bytes as u64 > MAX_RECORD_BYTES as u64 {
            return Err(RecordError::TooLarge.into());
        }
        self.bytes += bytes as usize;
        if self.bytes > 16 * 1024 * 1024 {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
}

// IDs are already decoded bounded strings. A single JSON parameter keeps batch
// reads independent of SQLite's variable-count limit, without scanning/decoding
// unrelated entity kinds or imposing a hidden 1,000-record page boundary.
type References = BTreeMap<String, Vec<usize>>;
fn reference(refs: &mut References, id: &str, index: usize) {
    refs.entry(id.to_owned()).or_default().push(index);
}
fn ids(refs: &References) -> Result<String> {
    Ok(serde_json::to_string(&refs.keys().collect::<Vec<_>>())?)
}
fn query_refs(kind: &str, refs: &References) -> Result<QueryBuilder<'static, Sqlite>> {
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind = ")
        .push_bind(kind.to_owned())
        .push(" AND id IN (SELECT value FROM json_each(")
        .push_bind(ids(refs)?)
        .push(")) ORDER BY created_at, id");
    Ok(q)
}

impl SqliteAssistantStore {
    pub async fn subagents_snapshot(&self, session_id: &str) -> Result<SubagentSnapshot> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'chat_sessions' AND id = ")
            .push_bind(session_id);
        let row = q
            .build()
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(Error::NotFound)?;
        budget.charge(&row)?;
        let session = decode_row(row)?;

        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(
            " WHERE kind = 'chat_subagents' AND json_extract(payload, '$.parent_session_id') = ",
        )
        .push_bind(session_id)
        .push(" ORDER BY created_at, id LIMIT 10001");
        let statement = q.build();
        let mut rows = statement.fetch(&mut *tx);
        let mut records = Vec::new();
        while let Some(row) = rows.try_next().await? {
            budget.charge(&row)?;
            records.push(SubagentViewRow {
                record: decode_row(row)?,
                turn: Ok(None),
                approval: Ok(None),
                messages: Ok(Vec::new()),
                child_session: Ok(None),
            });
        }
        drop(rows);

        let mut turns = References::new();
        let mut sessions = References::new();
        for (index, row) in records.iter().enumerate() {
            let p = row.record.payload();
            if let Some(id) = p["child_turn_id"].as_str().filter(|id| !id.is_empty()) {
                reference(&mut turns, id, index);
            }
            if p["model"].is_null() {
                reference(
                    &mut sessions,
                    p["child_session_id"]
                        .as_str()
                        .ok_or(Error::CorruptEnvelope)?,
                    index,
                );
            }
        }
        if !turns.is_empty() {
            let mut q = query_refs("chat_turns", &turns)?;
            let statement = q.build();
            let mut rows = statement.fetch(&mut *tx);
            while let Some(row) = rows.try_next().await? {
                for &index in &turns[row.try_get::<&str, _>("id")?] {
                    // Repeated references produce repeated view inputs and
                    // therefore consume the aggregate budget independently.
                    budget.charge(&row)?;
                    let decoded = decode_row_ref(&row);
                    if decoded.is_ok() {
                        budget.bytes(&row)?;
                    }
                    records[index].turn = match decoded {
                        Ok(record) => Ok(Some(RawSubagentTurn {
                            record,
                            raw: row.try_get("payload")?,
                        })),
                        Err(error) => Err(error),
                    };
                }
            }
        }

        let mut approvals = References::new();
        let mut questions = References::new();
        for (index, row) in records.iter().enumerate() {
            if row.record.payload()["status"] != "running" {
                continue;
            }
            let Ok(Some(turn)) = &row.turn else {
                continue;
            };
            reference(
                &mut questions,
                row.record.payload()["id"]
                    .as_str()
                    .ok_or(Error::CorruptEnvelope)?,
                index,
            );
            if turn.record.payload()["status"] == "waiting_approval"
                && let Some(id) = turn.record.payload()["approval_id"]
                    .as_str()
                    .filter(|id| !id.is_empty())
            {
                reference(&mut approvals, id, index);
            }
        }
        if !approvals.is_empty() {
            let mut q = query_refs("approvals", &approvals)?;
            let statement = q.build();
            let mut rows = statement.fetch(&mut *tx);
            while let Some(row) = rows.try_next().await? {
                for &index in &approvals[row.try_get::<&str, _>("id")?] {
                    budget.charge(&row)?;
                    records[index].approval = approval(&row).map(Some);
                }
            }
        }
        if !questions.is_empty() {
            let mut q =
                QueryBuilder::new("SELECT json_extract(payload, '$.subagent_id') AS subagent_id, ");
            q.push(&SELECT_RECORD["SELECT ".len()..])
                .push(" WHERE kind = 'chat_subagent_messages' AND json_extract(payload, '$.direction') = 'to_parent' AND json_extract(payload, '$.subagent_id') IN (SELECT value FROM json_each(")
                .push_bind(ids(&questions)?)
                .push(")) ORDER BY created_at, id LIMIT 10001");
            let statement = q.build();
            let mut rows = statement.fetch(&mut *tx);
            while let Some(row) = rows.try_next().await? {
                for &index in &questions[row.try_get::<&str, _>("subagent_id")?] {
                    budget.charge(&row)?;
                    if let Ok(messages) = &mut records[index].messages {
                        match decode_row_ref(&row) {
                            Ok(message) => messages.push(message),
                            Err(error) => records[index].messages = Err(error),
                        }
                    }
                }
            }
        }
        if !sessions.is_empty() {
            let mut q = query_refs("chat_sessions", &sessions)?;
            let statement = q.build();
            let mut rows = statement.fetch(&mut *tx);
            while let Some(row) = rows.try_next().await? {
                for &index in &sessions[row.try_get::<&str, _>("id")?] {
                    budget.charge(&row)?;
                    records[index].child_session = decode_row_ref(&row).map(Some);
                }
            }
        }
        tx.commit().await?;
        Ok(SubagentSnapshot { session, records })
    }
}

fn approval(row: &SqliteRow) -> Result<StoredDependency> {
    let record = StoredDependency::decode(
        DependencyKind::Approval,
        row.try_get::<&str, _>("payload")?.as_bytes(),
    )?;
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
