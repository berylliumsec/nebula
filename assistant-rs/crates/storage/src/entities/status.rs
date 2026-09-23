//! Bounded snapshots for retained activity, queue and hook views. No execution.
use super::*;
use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};

#[derive(Debug)]
pub struct ActivitySnapshot {
    /// Services check pending-turn conflicts before propagating this read error,
    /// preserving Python's validation order without holding a read transaction.
    pub sessions: Result<Vec<StoredAssistantRecord>>,
    pub unfinished_turns: Vec<StoredAssistantRecord>,
}
#[derive(Debug)]
pub struct QueueSnapshot {
    pub session: StoredAssistantRecord,
    /// Absence is a response-only default; this read never creates a queue.
    pub queue: Option<StoredAssistantRecord>,
}
#[derive(Debug)]
pub struct TurnHooksSnapshot {
    pub turn: StoredAssistantRecord,
    /// Every same-session attempt is validated before services filter by turn.
    pub hooks: Vec<StoredDependency>,
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
    pub async fn activity_snapshot(&self, project_id: &str) -> Result<ActivitySnapshot> {
        // A retained project scope is an opaque string, not an Entity.id lookup.
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'chat_turns' AND engagement_id = ")
            .push_bind(project_id)
            .push(" AND json_extract(payload, '$.status') IN ('routing', 'waiting_approval', 'waiting_callback', 'finalizing', 'interrupted') ORDER BY created_at, id LIMIT 10001");
        // Orphan and temporary-session turns still participate in the service's
        // project-wide pending conflict check. Never join to visible sessions.
        let unfinished_turns = records(&mut tx, &mut budget, &mut q).await?;
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'chat_sessions' AND engagement_id = ")
            .push_bind(project_id)
            .push(" AND coalesce(json_extract(payload, '$.metadata.temporary_assistant'), 0) IS 0 ORDER BY created_at, id LIMIT 10001");
        let sessions = records(&mut tx, &mut budget, &mut q).await;
        tx.commit().await?;
        Ok(ActivitySnapshot {
            sessions,
            unfinished_turns,
        })
    }

    pub async fn queue_snapshot(&self, session_id: &str) -> Result<QueueSnapshot> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let session = one(&mut tx, &mut budget, AssistantKind::Session, session_id)
            .await?
            .ok_or(Error::NotFound)?;
        // The canonical ID is the legacy association. Do not add project or
        // session payload filters, and do not persist a missing queue default.
        let queue_id = format!("chat-queue-{session_id}");
        let queue = one(&mut tx, &mut budget, AssistantKind::Queue, &queue_id).await?;
        tx.commit().await?;
        Ok(QueueSnapshot { session, queue })
    }

    pub async fn turn_hooks_snapshot(&self, turn_id: &str) -> Result<TurnHooksSnapshot> {
        validate_id(turn_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let turn = one(&mut tx, &mut budget, AssistantKind::Turn, turn_id)
            .await?
            .ok_or(Error::NotFound)?;
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind = 'native_hook_executions' AND chat_session_id = ")
            .push_bind(
                turn.payload()["session_id"]
                    .as_str()
                    .ok_or(Error::CorruptEnvelope)?,
            )
            .push(" ORDER BY created_at, id LIMIT 10001");
        // Do not check parent-session existence, project, owner kind, or turn
        // here. Python validates every same-session hook before turn filtering.
        let statement = q.build();
        let mut rows = statement.fetch(&mut *tx);
        let mut hooks = Vec::new();
        while let Some(row) = rows.try_next().await? {
            budget.charge(&row)?;
            let record = StoredDependency::decode(
                DependencyKind::NativeHookExecution,
                row.try_get::<&str, _>("payload")?.as_bytes(),
            )?;
            let p = record.payload();
            if p["id"].as_str() != Some(row.try_get("id")?)
                || p["revision"].as_i64() != Some(row.try_get("revision")?)
                || p["engagement_id"].as_str() != row.try_get::<Option<&str>, _>("engagement_id")?
                || p["chat_session_id"].as_str()
                    != row.try_get::<Option<&str>, _>("chat_session_id")?
                || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
                || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
            {
                return Err(Error::CorruptEnvelope);
            }
            hooks.push(record);
        }
        drop(rows);
        tx.commit().await?;
        Ok(TurnHooksSnapshot { turn, hooks })
    }
}

async fn one(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    kind: AssistantKind,
    id: &str,
) -> Result<Option<StoredAssistantRecord>> {
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind = ")
        .push_bind(kind.as_str())
        .push(" AND id = ")
        .push_bind(id);
    Ok(records(tx, budget, &mut q).await?.pop())
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
