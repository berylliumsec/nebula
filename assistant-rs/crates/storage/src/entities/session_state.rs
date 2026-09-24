//! Durable display revisions derived from retained state and passive observations.
//! This writer command can change only session_projections, never execution state.
use super::*;
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    session_state::{self as projection, ConnectionState, StateError, StateInputs},
};
use std::collections::HashMap;

pub type StateClock = dyn Fn() -> DateTime<Utc> + Send + Sync;
/// Trusted synchronous observation of existing transport state. Implementations
/// must not start work, perform network/file I/O, or wait for child processes.
pub type ConnectionObserver = dyn Fn(&str) -> ConnectionState + Send + Sync;
#[derive(Clone)]
pub struct StateObservations {
    pub clock: Arc<StateClock>,
    pub connection: Option<Arc<ConnectionObserver>>,
}
pub(super) struct StateRequest {
    pub(super) id: String,
    pub(super) observations: StateObservations,
    pub(super) _bytes: OwnedSemaphorePermit,
    pub(super) reply: oneshot::Sender<Result<Value>>,
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
        self.add(size as usize)
    }
    fn add(&mut self, bytes: usize) -> Result<()> {
        self.bytes += bytes;
        self.rows += 1;
        if self.bytes > MAX_TRANSACTION_BYTES || self.rows > 10_000 {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
}
fn project_error(error: StateError) -> Error {
    match error {
        StateError::Invalid => Error::CorruptEnvelope,
        StateError::TooLarge => Error::ReadLimit,
    }
}

impl SqliteAssistantStore {
    /// An unchanged view uses pooled readers only. A changed view is reloaded
    /// after acquiring the writer lock; no queued snapshot receives a revision.
    /// Cancellation after admission does not retract an accepted watermark write.
    pub async fn session_state(
        &self,
        session_id: &str,
        observations: StateObservations,
    ) -> Result<Value> {
        validate_id(session_id)?;
        {
            let _permit = self.read_permit()?;
            let mut tx = self.readers.begin().await?;
            let mut budget = Budget::default();
            let (mut projected, digest) =
                read_project(&mut tx, &mut budget, session_id, &observations).await?;
            let cached = watermark(&mut tx, &mut budget, session_id).await?;
            tx.commit().await?;
            if let Some((revision, old_digest)) = cached
                && digest == old_digest
            {
                projected["revision"] = revision.into();
                return Ok(projected);
            }
        }
        // Release the read connection, snapshot, payloads and permit before
        // writer admission. Only this bounded identity and observation handles
        // remain queued; observation is sampled again under BEGIN IMMEDIATE.
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        let bytes = self
            .bytes
            .clone()
            .try_acquire_many_owned((session_id.len() + 128) as u32)
            .map_err(|_| Error::Capacity)?;
        let slot = self.commands.try_reserve().map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        slot.send(Command::SessionState(StateRequest {
            id: session_id.to_owned(),
            observations,
            _bytes: bytes,
            reply,
        }));
        result.await.map_err(|_| Error::Closed)?
    }
}

pub(super) async fn write_state(
    connection: &mut SqliteConnection,
    session_id: &str,
    observations: &StateObservations,
) -> Result<Value> {
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    let mut budget = Budget::default();
    let (mut projected, digest) = read_project(&mut tx, &mut budget, session_id, observations)
        .await
        .map_err(|error| match error {
            Error::Record(RecordError::TooLarge) => Error::Record(RecordError::TooLarge),
            Error::CorruptEnvelope | Error::Record(_) => Error::InvalidStateProjectionDuringWrite,
            error => error,
        })?;
    let cached = watermark(&mut tx, &mut budget, session_id).await?;
    let revision = match cached {
        None => {
            sqlx::query(
                "INSERT INTO session_projections(session_id,revision,digest) VALUES (?,1,?)",
            )
            .bind(session_id)
            .bind(&digest)
            .execute(&mut *tx)
            .await?;
            1
        }
        Some((revision, old_digest)) if old_digest == digest => revision,
        Some((revision, _)) => {
            let revision = revision.checked_add(1).ok_or(Error::RevisionExhausted)?;
            sqlx::query("UPDATE session_projections SET revision=?,digest=? WHERE session_id=?")
                .bind(revision)
                .bind(&digest)
                .bind(session_id)
                .execute(&mut *tx)
                .await?;
            revision
        }
    };
    projected["revision"] = revision.into();
    tx.commit().await?;
    Ok(projected)
}

async fn watermark(
    connection: &mut SqliteConnection,
    budget: &mut Budget,
    session_id: &str,
) -> Result<Option<(i64, String)>> {
    let row = sqlx::query("SELECT revision,length(CAST(digest AS BLOB)) AS digest_bytes,CASE WHEN length(CAST(digest AS BLOB))<=64 THEN digest ELSE NULL END AS digest FROM session_projections WHERE session_id=?")
        .bind(session_id).fetch_optional(connection).await?;
    let Some(row) = row else { return Ok(None) };
    let size: i64 = row.try_get("digest_bytes")?;
    if size != 64 {
        return Err(Error::CorruptEnvelope);
    }
    budget.add(size as usize + 8)?;
    let revision: i64 = row.try_get("revision")?;
    let digest: String = row.try_get("digest")?;
    if revision < 1
        || !digest
            .bytes()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
    {
        return Err(Error::CorruptEnvelope);
    }
    Ok(Some((revision, digest)))
}

async fn read_project(
    connection: &mut SqliteConnection,
    budget: &mut Budget,
    session_id: &str,
    observations: &StateObservations,
) -> Result<(Value, String)> {
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind='chat_sessions' AND id=")
        .push_bind(session_id);
    let session = records(connection, budget, &mut q)
        .await?
        .pop()
        .ok_or(Error::NotFound)?;
    let project = session.payload()["engagement_id"]
        .as_str()
        .ok_or(Error::CorruptEnvelope)?;
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind='chat_turns' AND engagement_id=")
        .push_bind(project)
        .push(" AND chat_session_id=")
        .push_bind(session_id)
        .push(" ORDER BY created_at DESC,id DESC LIMIT 10001");
    let turns = records(connection, budget, &mut q).await?;
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind='approvals' AND engagement_id=").push_bind(project)
        .push(" AND (chat_session_id=").push_bind(session_id)
        .push(" OR EXISTS (SELECT 1 FROM entities AS owner WHERE owner.kind='chat_turns' AND owner.engagement_id=")
        .push_bind(project).push(" AND owner.chat_session_id=").push_bind(session_id)
        .push(" AND (owner.id=json_extract(entities.payload,'$.chat_turn_id') OR json_extract(owner.payload,'$.approval_id')=entities.id))) LIMIT 10001");
    let approvals = dependencies(connection, budget, &mut q).await?;
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind='harness_interactions' AND engagement_id=")
        .push_bind(project)
        .push(" AND chat_session_id=")
        .push_bind(session_id)
        .push(" LIMIT 10001");
    let questions = dependencies(connection, budget, &mut q).await?;
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind='harness_turns' AND engagement_id=").push_bind(project)
        .push(" AND EXISTS (SELECT 1 FROM entities AS owner WHERE owner.kind='chat_turns' AND owner.engagement_id=")
        .push_bind(project).push(" AND owner.chat_session_id=").push_bind(session_id)
        .push(" AND json_extract(owner.payload,'$.harness_turn_id')=entities.id) LIMIT 10001");
    let harnesses = dependencies(connection, budget, &mut q).await?;
    let mut inputs = StateInputs {
        session,
        turns,
        approvals,
        questions,
        harnesses,
        profile: None,
        progress: HashMap::new(),
    };
    let now = (observations.clock)();
    // Shared pure selection validates pending expiry before ledger/profile
    // reads, and avoids reimplementing selected-turn/decision ownership here.
    let requests = projection::progress_requests(&inputs, now).map_err(project_error)?;
    if !requests.is_empty() {
        let mut parameters = Vec::with_capacity(requests.len());
        for request in requests {
            budget.add(request.approval_id.len() + request.harness_turn_id.len() + 8)?;
            parameters.push(serde_json::json!([
                request.approval_id,
                request.harness_turn_id,
                request.after_sequence
            ]));
        }
        // One bounded query, with indexed per-operation MIN lookups. Only
        // immutable sequence scalars enter the snapshot, never event payloads.
        let statement = sqlx::query("WITH requested AS (SELECT json_extract(value,'$[0]') AS approval_id,json_extract(value,'$[1]') AS operation_id,json_extract(value,'$[2]') AS after_sequence FROM json_each(?)) SELECT approval_id,(SELECT MIN(sequence) FROM operation_events WHERE operation_events.operation_id=requested.operation_id AND operation_kind='harness_turn' AND sequence>requested.after_sequence AND event_type IN ('harness.message_delta','harness.completed','harness.tool_started','harness.tool_completed')) AS sequence FROM requested")
            .bind(serde_json::to_string(&parameters)?);
        let mut rows = statement.fetch(&mut *connection);
        while let Some(row) = rows.try_next().await? {
            let id: String = row.try_get("approval_id")?;
            let sequence: Option<i64> = row.try_get("sequence")?;
            budget.add(id.len() + 8)?;
            inputs.progress.insert(id, sequence);
        }
    }
    if let Some(id) = inputs.session.payload()["harness_profile_id"]
        .as_str()
        .filter(|id| !id.is_empty())
    {
        let mut q = QueryBuilder::new(SELECT_RECORD);
        q.push(" WHERE kind=")
            .push_bind(DependencyKind::HarnessProfile.as_str())
            .push(" AND id=")
            .push_bind(id);
        inputs.profile = dependencies(connection, budget, &mut q).await?.pop();
    }
    let connection_state = match (
        &observations.connection,
        inputs.session.payload()["harness_session_id"]
            .as_str()
            .filter(|id| !id.is_empty()),
    ) {
        (Some(observer), Some(id)) => observer(id),
        _ => ConnectionState::Unknown,
    };
    let projected = projection::project(&inputs, now, connection_state).map_err(project_error)?;
    let digest = projection::digest(&projected).map_err(project_error)?;
    Ok((projected, digest))
}

async fn records(
    connection: &mut SqliteConnection,
    budget: &mut Budget,
    query: &mut QueryBuilder<'_, Sqlite>,
) -> Result<Vec<StoredAssistantRecord>> {
    let statement = query.build();
    let mut rows = statement.fetch(connection);
    let mut records = Vec::new();
    while let Some(row) = rows.try_next().await? {
        budget.charge(&row)?;
        records.push(decode_row(row)?);
    }
    Ok(records)
}
async fn dependencies(
    connection: &mut SqliteConnection,
    budget: &mut Budget,
    query: &mut QueryBuilder<'_, Sqlite>,
) -> Result<Vec<StoredDependency>> {
    let statement = query.build();
    let mut rows = statement.fetch(connection);
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
