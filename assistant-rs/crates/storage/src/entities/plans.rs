//! Complete retained goal/schedule reads. No elapsed projection or scheduling.
use super::*;

fn wrapped_model_error(error: Error) -> Error {
    match error {
        Error::Record(
            error @ (RecordError::Shape(_)
            | RecordError::Invariant(_)
            | RecordError::ModelValidation(_)),
        ) => Error::WrappedRecord(error),
        error => error,
    }
}

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
    fn hydrated_goal(&mut self, record: &StoredAssistantRecord, raw_bytes: usize) -> Result<()> {
        if record.kind() != AssistantKind::Goal {
            return Ok(());
        }
        // Defaults/coercions can make a valid legacy Goal larger than its
        // retained JSON. Count into a bounded sink, without another payload.
        struct Counter {
            bytes: usize,
            limit: usize,
        }
        impl std::io::Write for Counter {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                if bytes.len() > self.limit.saturating_sub(self.bytes) {
                    return Err(std::io::Error::other("hydrated goal byte limit"));
                }
                self.bytes += bytes.len();
                Ok(bytes.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let mut counter = Counter {
            bytes: 0,
            limit: MAX_TRANSACTION_BYTES
                .saturating_sub(self.bytes)
                .saturating_add(raw_bytes),
        };
        serde_json::to_writer(&mut counter, record.payload()).map_err(|_| Error::ReadLimit)?;
        self.bytes = self
            .bytes
            .saturating_add(counter.bytes.saturating_sub(raw_bytes));
        Ok(())
    }
    fn report(&mut self, error: Error) -> Error {
        if let Error::RetainedModelValidation(report) = &error {
            // The report shares its input internally; charge that retained
            // allocation and issue metadata in addition to preceding rows.
            self.bytes = self.bytes.saturating_add(report.retained_bytes());
            if self.bytes > MAX_TRANSACTION_BYTES {
                return Error::ReadLimit;
            }
        }
        error
    }
    fn charge(&mut self, row: &SqliteRow) -> Result<usize> {
        let bytes: i64 = row.try_get("payload_bytes")?;
        if bytes < 0 || bytes as u64 > MAX_RECORD_BYTES as u64 {
            return Err(RecordError::TooLarge.into());
        }
        self.bytes += bytes as usize;
        self.rows += 1;
        if self.bytes > 16 * 1024 * 1024 || self.rows > 10_000 {
            return Err(Error::ReadLimit);
        }
        Ok(bytes as usize)
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
                let raw_bytes = budget.charge(&row)?;
                let goal = decode_row_ref(&row).map_err(wrapped_model_error)?;
                budget.hydrated_goal(&goal, raw_bytes)?;
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
    let session = records(tx, budget, &mut q, ValidationSurface::WrappedRecord)
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
    let records = records(tx, budget, &mut q, ValidationSurface::DirectModel).await?;
    Ok(SessionPlansSnapshot { session, records })
}

async fn records(
    tx: &mut sqlx::Transaction<'_, Sqlite>,
    budget: &mut Budget,
    query: &mut QueryBuilder<'_, Sqlite>,
    surface: ValidationSurface,
) -> Result<Vec<StoredAssistantRecord>> {
    let statement = query.build();
    let mut rows = statement.fetch(&mut **tx);
    let mut result = Vec::new();
    while let Some(row) = rows.try_next().await? {
        let raw_bytes = budget.charge(&row)?;
        let record = decode_row_ref_on(&row, surface).map_err(|error| {
            let error = match surface {
                ValidationSurface::WrappedRecord => wrapped_model_error(error),
                ValidationSurface::DirectModel => error,
            };
            budget.report(error)
        })?;
        budget.hydrated_goal(&record, raw_bytes)?;
        result.push(record);
    }
    Ok(result)
}
