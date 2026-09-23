//! Complete retained goal/schedule reads. No elapsed projection or scheduling.
use super::*;

#[derive(Debug)]
pub struct SessionPlansSnapshot {
    pub session: StoredAssistantRecord,
    pub records: Vec<StoredAssistantRecord>,
}

#[derive(Debug)]
pub struct GoalChildrenSnapshot {
    pub session: StoredAssistantRecord,
    pub goals: Vec<StoredAssistantRecord>,
    pub children: Vec<StoredAssistantRecord>,
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
    pub async fn session_plans_snapshot(
        &self,
        kind: AssistantKind,
        session_id: &str,
    ) -> Result<SessionPlansSnapshot> {
        if !matches!(kind, AssistantKind::Goal | AssistantKind::Schedule) {
            return Err(Error::InvalidBounds);
        }
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let snapshot = session_plans(&mut tx, &mut Budget::default(), kind, session_id).await?;
        tx.commit().await?;
        Ok(snapshot)
    }

    pub async fn goal_children_snapshot(&self, session_id: &str) -> Result<GoalChildrenSnapshot> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let SessionPlansSnapshot {
            session,
            records: goals,
        } = session_plans(&mut tx, &mut budget, AssistantKind::Goal, session_id).await?;
        let mut children = Vec::new();
        if goals.len() == 1 {
            let parent_id = goals[0].payload()["id"]
                .as_str()
                .ok_or(Error::CorruptEnvelope)?;
            let mut q = QueryBuilder::new(SELECT_RECORD);
            q.push(" WHERE kind = 'chat_goals' ORDER BY created_at, id LIMIT 10001");
            let statement = q.build();
            let mut rows = statement.fetch(&mut *tx);
            while let Some(row) = rows.try_next().await? {
                // The Python collection validates unrelated goals before
                // filtering. They consume the budget even when not retained.
                budget.charge(&row)?;
                let goal = decode_row(row)?;
                if goal.payload()["parent_goal_id"] == parent_id {
                    children.push(goal);
                }
            }
        }
        // No global scan when a service will report absent/ambiguous parents.
        tx.commit().await?;
        Ok(GoalChildrenSnapshot {
            session,
            goals,
            children,
        })
    }
}

async fn session_plans(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    kind: AssistantKind,
    session_id: &str,
) -> Result<SessionPlansSnapshot> {
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind = 'chat_sessions' AND id = ")
        .push_bind(session_id);
    let session = records(tx, budget, &mut q)
        .await?
        .pop()
        .ok_or(Error::NotFound)?;
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind = ")
        .push_bind(kind.as_str())
        .push(" AND chat_session_id = ")
        .push_bind(session_id)
        .push(" ORDER BY created_at, id LIMIT 10001");
    // Validate every candidate before services choose the authoritative goal
    // or oldest schedule. Historical project/backend mismatches remain visible.
    let records = records(tx, budget, &mut q).await?;
    Ok(SessionPlansSnapshot { session, records })
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
