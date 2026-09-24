//! Complete, bounded snapshots of retained Assistant outputs. No artifact I/O.
use super::*;
use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};
use std::collections::HashMap;

#[derive(Debug)]
pub struct ResultsSnapshot {
    /// Stored-row page: roles and retracted rows still consume the cursor.
    pub messages: Vec<StoredAssistantRecord>,
    pub next_offset: Option<u64>,
    pub calls: Vec<StoredDependency>,
    pub call_turns: HashMap<String, StoredAssistantRecord>,
    pub diffs: HashMap<String, Vec<StoredDependency>>,
}

#[derive(Default)]
struct Budget {
    bytes: usize,
    rows: usize,
}
impl Budget {
    fn charge(&mut self, row: &SqliteRow) -> Result<()> {
        let bytes: i64 = row.try_get("payload_bytes")?;
        if bytes < 0 || bytes as u64 > MAX_RECORD_BYTES as u64 {
            return Err(RecordError::TooLarge.into());
        }
        self.bytes += bytes as usize;
        self.rows += 1;
        if self.bytes > 16 * 1024 * 1024 || self.rows > 10_000 {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
}

impl SqliteAssistantStore {
    pub async fn results_snapshot(
        &self,
        session_id: &str,
        project_id: &str,
        offset: u64,
        limit: u32,
        include_artifacts: bool,
    ) -> Result<ResultsSnapshot> {
        validate_bounds(session_id, offset, limit)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        validate_session(&mut tx, &mut budget, session_id, project_id).await?;
        let page = message_page(
            &mut tx,
            &mut budget,
            session_id,
            project_id,
            offset,
            limit,
            false,
        )
        .await?;
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'tool_calls' AND engagement_id = ")
            .push_bind(project_id)
            .push(" AND chat_session_id = ")
            .push_bind(session_id);
        // Deliberately no ORDER BY: the legacy query retains SQLite row order.
        let calls = dependencies(&mut tx, &mut budget, &mut q).await?;
        let mut call_turns = HashMap::new();
        let mut diffs = HashMap::new();
        let visible = || {
            page.records.iter().filter(|message| {
                !message.is_replaced_message() && message.payload()["role"] == "assistant"
            })
        };
        if visible().next().is_some() {
            // Python get(ChatTurn, id) checks kind but does not re-scope this
            // retained reference to the current session or project.
            let mut q = QueryBuilder::new(SELECT_RECORD);
            q.push(" WHERE kind = 'chat_turns' AND id IN (SELECT json_extract(payload, '$.chat_turn_id') FROM entities WHERE kind = 'tool_calls' AND engagement_id = ")
                .push_bind(project_id).push(" AND chat_session_id = ").push_bind(session_id)
                .push(" AND json_extract(payload, '$.chat_turn_id') <> '')");
            for turn in records(&mut tx, &mut budget, &mut q).await? {
                call_turns.insert(turn.payload()["id"].as_str().unwrap().to_owned(), turn);
            }
        }
        if include_artifacts {
            for message in visible() {
                let turn_id = &message.payload()["metadata"]["harness_turn_id"];
                if !truthy(turn_id) {
                    continue;
                }
                let mut q = QueryBuilder::new(SELECT_RECORD);
                q.push(" WHERE kind = 'artifacts' AND engagement_id = ")
                    .push_bind(project_id)
                    .push(" AND json_extract(payload, '$.source') = 'harness-file-diff' AND json_extract(payload, '$.metadata.harness_turn_id') = ");
                // Metadata is opaque. Match SQLite's native scalar comparison,
                // not a stringified substitute for a numeric/bool identifier.
                match turn_id {
                    Value::String(value) => {
                        q.push_bind(value);
                    }
                    Value::Bool(value) => {
                        q.push_bind(*value);
                    }
                    Value::Number(value) if value.is_i64() => {
                        q.push_bind(value.as_i64().unwrap());
                    }
                    Value::Number(value) if value.is_f64() => {
                        q.push_bind(value.as_f64().unwrap());
                    }
                    // SQLite/Python also refuses unsupported binding shapes.
                    _ => return Err(Error::InvalidBounds),
                }
                let retained = dependencies(&mut tx, &mut budget, &mut q).await?;
                diffs.insert(
                    message.payload()["id"].as_str().unwrap().to_owned(),
                    retained,
                );
            }
        }
        tx.commit().await?;
        Ok(ResultsSnapshot {
            messages: page.records,
            next_offset: page.next_offset,
            calls,
            call_turns,
            diffs,
        })
    }

    pub async fn context_sources_page(
        &self,
        session_id: &str,
        project_id: &str,
        offset: u64,
    ) -> Result<Page> {
        validate_bounds(session_id, offset, 40)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        validate_session(&mut tx, &mut budget, session_id, project_id).await?;
        let page = message_page(
            &mut tx,
            &mut budget,
            session_id,
            project_id,
            offset,
            40,
            true,
        )
        .await?;
        tx.commit().await?;
        Ok(page)
    }
}

fn validate_bounds(session_id: &str, offset: u64, limit: u32) -> Result<()> {
    validate_id(session_id)?;
    if offset > i64::MAX as u64 || !(1..=100).contains(&limit) {
        return Err(Error::InvalidBounds);
    }
    Ok(())
}

async fn validate_session(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    session_id: &str,
    project_id: &str,
) -> Result<()> {
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind = 'chat_sessions' AND id = ")
        .push_bind(session_id);
    let session = records(tx, budget, &mut q)
        .await?
        .pop()
        .ok_or(Error::NotFound)?;
    if session.payload()["engagement_id"] != project_id {
        return Err(Error::Conflict);
    }
    Ok(())
}

async fn message_page(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    session_id: &str,
    project_id: &str,
    offset: u64,
    limit: u32,
    descending: bool,
) -> Result<Page> {
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind = 'chat_messages' AND engagement_id = ")
        .push_bind(project_id)
        .push(" AND chat_session_id = ")
        .push_bind(session_id)
        .push(" ORDER BY json_extract(payload, '$.sequence')")
        .push(if descending { " DESC" } else { " ASC" })
        .push(" LIMIT ")
        .push_bind(i64::from(limit) + 1)
        .push(" OFFSET ")
        .push_bind(offset as i64);
    let mut records = records(tx, budget, &mut q).await?;
    let next_offset = (records.len() > limit as usize).then_some(offset + u64::from(limit));
    records.truncate(limit as usize);
    Ok(Page {
        records,
        next_offset,
    })
}

async fn records(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    query: &mut QueryBuilder<'_, Sqlite>,
) -> Result<Vec<StoredAssistantRecord>> {
    let statement = query.build();
    let mut rows = statement.fetch(&mut **tx);
    let mut result = Vec::new();
    while let Some(row) = rows.try_next().await? {
        budget.charge(&row)?;
        result.push(decode_row(row)?);
    }
    Ok(result)
}

async fn dependencies(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    query: &mut QueryBuilder<'_, Sqlite>,
) -> Result<Vec<StoredDependency>> {
    let statement = query.build();
    let mut rows = statement.fetch(&mut **tx);
    let mut result = Vec::new();
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
        result.push(record);
    }
    Ok(result)
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}
