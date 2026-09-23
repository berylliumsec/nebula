//! Bounded read snapshots for catch-up. No dispatch or shared-entity writes.
use super::*;
use chrono::FixedOffset;
use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};
use std::collections::HashMap;

#[derive(Debug)]
pub struct PendingSnapshot {
    pub turns: Vec<StoredAssistantRecord>,
    pub approvals: Vec<StoredDependency>,
    pub questions: Vec<StoredDependency>,
    pub harnesses: Vec<StoredDependency>,
}
#[derive(Debug)]
pub struct CatchupSnapshot {
    pub turns: Vec<StoredAssistantRecord>,
    pub sources: HashMap<String, Option<StoredAssistantRecord>>,
    pub messages: Vec<StoredAssistantRecord>,
    pub prompts: HashMap<String, String>,
    pub pending: PendingSnapshot,
}

#[derive(Default)]
struct Budget {
    bytes: usize,
    rows: usize,
}
impl Budget {
    fn charge(&mut self, row: &SqliteRow) -> Result<()> {
        let size: i64 = row.try_get("payload_bytes")?;
        if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
            return Err(RecordError::TooLarge.into());
        }
        self.bytes += size as usize;
        self.rows += 1;
        if self.bytes > 16 * 1024 * 1024 || self.rows > 10000 {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
}

impl SqliteAssistantStore {
    pub async fn source_message(
        &self,
        session_id: &str,
        turn: &StoredAssistantRecord,
    ) -> Result<Option<StoredAssistantRecord>> {
        validate_id(session_id)?;
        if turn.kind() != AssistantKind::Turn {
            return Err(Error::InvalidBounds);
        }
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let message = source(&mut tx, &mut Budget::default(), session_id, turn).await?;
        tx.commit().await?;
        Ok(message)
    }

    pub async fn catchup_snapshot(
        &self,
        session_id: &str,
        project_id: &str,
        cursor_through: Option<DateTime<FixedOffset>>,
        through: DateTime<Utc>,
    ) -> Result<CatchupSnapshot> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'chat_sessions' AND id = ")
            .push_bind(session_id);
        let session = records(&mut tx, &mut budget, &mut q)
            .await?
            .pop()
            .ok_or(Error::NotFound)?;
        if session.payload()["engagement_id"] != project_id {
            return Err(Error::Conflict);
        }
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'chat_turns' AND chat_session_id = ")
            .push_bind(session_id)
            .push(" ORDER BY updated_at DESC LIMIT 101");
        let turns = records(&mut tx, &mut budget, &mut q).await?;
        let mut sources = HashMap::new();
        for turn in turns.iter().take(100) {
            let message = source(&mut tx, &mut budget, session_id, turn).await?;
            sources.insert(
                turn.payload()["id"]
                    .as_str()
                    .ok_or(Error::CorruptEnvelope)?
                    .into(),
                message,
            );
        }
        let mut messages = Vec::new();
        let mut prompts = HashMap::new();
        if let Some(after) = cursor_through {
            let mut q = QueryBuilder::new(SELECT_RECORD);
            q.push(" WHERE kind = 'chat_messages' AND chat_session_id = ")
                .push_bind(session_id)
                .push(" AND json_extract(payload, '$.role') = 'assistant' AND created_at > ")
                // SQLite's legacy SQLAlchemy datetime binder drops the offset
                // without converting wall-clock fields. Cursor timestamps have
                // no UTC field validator, so preserve that query behavior.
                .push_bind(after.format("%Y-%m-%d %H:%M:%S%.6f").to_string())
                .push(" AND created_at <= ")
                .push_bind(through.format("%Y-%m-%d %H:%M:%S%.6f").to_string())
                .push(" ORDER BY created_at DESC LIMIT 101");
            messages = records(&mut tx, &mut budget, &mut q).await?;
            for message in messages
                .iter()
                .filter(|message| !message.is_replaced_message())
                .take(100)
            {
                let mut q = QueryBuilder::new(SELECT_RECORD);
                q.push(" WHERE kind = 'chat_messages' AND chat_session_id = ").push_bind(session_id)
                    .push(" AND json_extract(payload, '$.role') = 'user' AND json_extract(payload, '$.sequence') < ")
                    .push_bind(message.payload()["sequence"].as_i64().ok_or(Error::InvalidBounds)?)
                    .push(" ORDER BY json_extract(payload, '$.sequence') DESC LIMIT 1");
                let row = q.build().fetch_optional(&mut *tx).await?;
                let prompt = if let Some(row) = row {
                    budget.charge(&row)?;
                    // Python reads this one content field without validating the
                    // predecessor as a ChatMessage; preserve that projection.
                    let payload: Value = serde_json::from_str(row.try_get("payload")?)?;
                    payload
                        .get("content")
                        .unwrap_or(&Value::String(String::new()))
                        .as_str()
                        .ok_or(Error::CorruptEnvelope)?
                        .to_owned()
                } else {
                    String::new()
                };
                prompts.insert(
                    message.payload()["id"]
                        .as_str()
                        .ok_or(Error::CorruptEnvelope)?
                        .into(),
                    prompt,
                );
            }
        }
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'chat_turns' AND engagement_id = ")
            .push_bind(project_id)
            .push(" AND chat_session_id = ")
            .push_bind(session_id)
            .push(" ORDER BY created_at DESC, id DESC LIMIT 10001");
        let pending_turns = records(&mut tx, &mut budget, &mut q).await?;
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'approvals' AND engagement_id = ").push_bind(project_id)
            .push(" AND (chat_session_id = ").push_bind(session_id)
            .push(" OR EXISTS (SELECT 1 FROM entities AS owner WHERE owner.kind = 'chat_turns' AND owner.engagement_id = ")
            .push_bind(project_id).push(" AND owner.chat_session_id = ").push_bind(session_id)
            .push(" AND (owner.id = json_extract(entities.payload, '$.chat_turn_id') OR json_extract(owner.payload, '$.approval_id') = entities.id))) LIMIT 10001");
        let approvals = dependencies(&mut tx, &mut budget, &mut q).await?;
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'harness_interactions' AND engagement_id = ")
            .push_bind(project_id)
            .push(" AND chat_session_id = ")
            .push_bind(session_id)
            .push(" LIMIT 10001");
        let questions = dependencies(&mut tx, &mut budget, &mut q).await?;
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'harness_turns' AND engagement_id = ").push_bind(project_id)
            .push(" AND EXISTS (SELECT 1 FROM entities AS owner WHERE owner.kind = 'chat_turns' AND owner.engagement_id = ")
            .push_bind(project_id).push(" AND owner.chat_session_id = ").push_bind(session_id)
            .push(" AND json_extract(owner.payload, '$.harness_turn_id') = entities.id) LIMIT 10001");
        let harnesses = dependencies(&mut tx, &mut budget, &mut q).await?;
        tx.commit().await?;
        Ok(CatchupSnapshot {
            turns,
            sources,
            messages,
            prompts,
            pending: PendingSnapshot {
                turns: pending_turns,
                approvals,
                questions,
                harnesses,
            },
        })
    }
}

async fn source(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    session_id: &str,
    turn: &StoredAssistantRecord,
) -> Result<Option<StoredAssistantRecord>> {
    if let Some(id) = turn.payload()["final_message_id"]
        .as_str()
        .filter(|id| !id.is_empty())
    {
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE id = ")
            .push_bind(id)
            .push(" AND json_extract(payload, '$.session_id') = ")
            .push_bind(session_id);
        if let Some(record) = records(tx, budget, &mut q).await?.pop() {
            if record.kind() != AssistantKind::Message {
                return Err(RecordError::Shape("chat_messages").into());
            }
            return Ok(Some(record));
        }
    }
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind = 'chat_messages' AND chat_session_id = ")
        .push_bind(session_id)
        .push(" AND json_extract(payload, '$.role') = 'user' AND created_at >= ")
        .push_bind(sql_time(&turn.payload()["created_at"])?)
        .push(" ORDER BY created_at LIMIT 1");
    Ok(records(tx, budget, &mut q).await?.pop())
}

async fn records(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    query: &mut QueryBuilder<'_, Sqlite>,
) -> Result<Vec<StoredAssistantRecord>> {
    let statement = query.build();
    let mut rows = statement.fetch(&mut **tx);
    let mut records = Vec::new();
    while let Some(row) = rows.try_next().await? {
        budget.charge(&row)?;
        records.push(decode_row(row)?);
    }
    Ok(records)
}
async fn dependencies(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    query: &mut QueryBuilder<'_, Sqlite>,
) -> Result<Vec<StoredDependency>> {
    let statement = query.build();
    let mut rows = statement.fetch(&mut **tx);
    let mut records = Vec::new();
    while let Some(row) = rows.try_next().await? {
        budget.charge(&row)?;
        let kind = DependencyKind::try_from(row.try_get::<&str, _>("kind")?)?;
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
        records.push(record);
    }
    Ok(records)
}
