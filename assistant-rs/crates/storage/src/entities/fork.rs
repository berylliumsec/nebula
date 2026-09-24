//! Retained conversation forks: bounded reads and one narrow commit per source
//! stage. Harness creation/deletion here grants no execution or general mutation.
use super::*;
use nebula_assistant_domain::dependencies::{
    DependencyEnvironment, DependencyKind, StoredDependency,
};
use serde::{
    Deserialize, Deserializer,
    de::{IgnoredAny, MapAccess, Visitor},
};
use serde_json::value::RawValue;
use std::{
    collections::{HashMap, HashSet},
    io::Write,
};

pub struct ForkRecord {
    pub record: StoredAssistantRecord,
    pub raw_payload: String,
}
impl ForkRecord {
    pub fn new(record: StoredAssistantRecord, original: &str) -> Result<Self> {
        if !matches!(
            record.kind(),
            AssistantKind::Session
                | AssistantKind::Message
                | AssistantKind::Decision
                | AssistantKind::Goal
        ) {
            return Err(Error::InvalidBounds);
        }
        let raw_payload = retained_json(original, record.payload())?;
        Ok(Self {
            record,
            raw_payload,
        })
    }
}
pub struct ForkHarness {
    pub record: StoredDependency,
    pub raw_payload: String,
}
impl std::fmt::Debug for ForkRecord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ForkRecord")
            .field("kind", &self.record.kind())
            .finish_non_exhaustive()
    }
}
impl std::fmt::Debug for ForkHarness {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ForkHarness")
            .field("kind", &self.record.kind())
            .finish_non_exhaustive()
    }
}
impl ForkHarness {
    pub fn new(record: StoredDependency, original: &str) -> Result<Self> {
        if record.kind() != DependencyKind::HarnessSession {
            return Err(Error::InvalidBounds);
        }
        let raw_payload = retained_json(original, record.payload())?;
        Ok(Self {
            record,
            raw_payload,
        })
    }
}

pub(super) struct ForkRequest {
    pub operation: Operation,
    pub _bytes: OwnedSemaphorePermit,
    pub reply: oneshot::Sender<Result<()>>,
}
pub(super) enum Operation {
    Assistant(ForkRecord),
    Harness(ForkHarness),
    Cleanup(String),
}

struct Counter(usize);
impl Write for Counter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > MAX_TRANSACTION_BYTES {
            return Err(std::io::Error::other("fork payload exceeds byte limit"));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
pub(super) fn size(value: &Value) -> Result<usize> {
    let mut count = Counter(0);
    serde_json::to_writer(&mut count, value).map_err(|_| Error::ReadLimit)?;
    Ok(count.0)
}
pub(super) fn recovery_session(row: &SqliteRow) -> Result<StoredAssistantRecord> {
    let raw: &str = row.try_get("payload")?;
    let record =
        StoredAssistantRecord::decode_fork_persisted_direct(AssistantKind::Session, raw.as_bytes())
            .map_err(|e| model_error(e, true))?;
    verify(row, record.payload(), None)?;
    Ok(record)
}
#[derive(Default)]
struct Budget {
    rows: usize,
    bytes: usize,
}
impl Budget {
    fn error(&mut self, error: RecordError, wrapped: bool) -> Error {
        if let RecordError::ModelValidation(report) = &error
            && self.add(report.retained_bytes()).is_err()
        {
            return Error::ReadLimit;
        }
        model_error(error, wrapped)
    }
    fn add(&mut self, bytes: usize) -> Result<()> {
        self.bytes = self.bytes.saturating_add(bytes);
        if self.bytes > MAX_TRANSACTION_BYTES {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
    fn row(&mut self, row: &SqliteRow) -> Result<()> {
        self.rows += 1;
        if self.rows > 10_000 {
            return Err(Error::ReadLimit);
        }
        let bytes: i64 = row.try_get("payload_bytes")?;
        if bytes < 0 || bytes as u64 > MAX_RECORD_BYTES as u64 {
            return Err(Error::ReadLimit);
        }
        self.add(bytes as usize)
    }
    fn decoded(&mut self, payload: &Value, raw: &str) -> Result<()> {
        self.add(size(payload)?)?;
        self.add(raw.len())
    }
}
fn model_error(error: RecordError, wrapped: bool) -> Error {
    match error {
        RecordError::TooLarge => Error::ReadLimit,
        error @ (RecordError::ModelValidation(_)
        | RecordError::Shape(_)
        | RecordError::Invariant(_))
            if wrapped =>
        {
            Error::WrappedRecord(error)
        }
        RecordError::ModelValidation(report) => Error::RetainedModelValidation(report),
        error => Error::Record(error),
    }
}
fn verify(row: &SqliteRow, p: &Value, session: Option<&str>) -> Result<()> {
    if p["id"].as_str() != Some(row.try_get("id")?)
        || p["revision"].as_i64() != Some(row.try_get("revision")?)
        || p["engagement_id"].as_str() != row.try_get::<Option<&str>, _>("engagement_id")?
        || session != row.try_get::<Option<&str>, _>("chat_session_id")?
        || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
        || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
    {
        return Err(Error::CorruptEnvelope);
    }
    Ok(())
}
fn decode(row: &SqliteRow, budget: &mut Budget, wrapped: bool) -> Result<ForkRecord> {
    budget.row(row)?;
    let raw: &str = row.try_get("payload")?;
    let kind = AssistantKind::try_from(row.try_get::<&str, _>("kind")?)?;
    let record = StoredAssistantRecord::decode_fork_persisted_direct(kind, raw.as_bytes())
        .map_err(|e| budget.error(e, wrapped))?;
    verify(row, record.payload(), session_projection(&record))?;
    let decoded = ForkRecord::new(record, raw)?;
    budget.decoded(decoded.record.payload(), &decoded.raw_payload)?;
    Ok(decoded)
}
fn id_lookup(id: &str) -> Result<()> {
    if id.is_empty() || id.chars().count() > 200 {
        Err(Error::NotFound)
    } else {
        Ok(())
    }
}
struct ClockEnvironment(Arc<StateClock>);
impl DependencyEnvironment for ClockEnvironment {
    fn now(&mut self) -> DateTime<Utc> {
        (self.0)()
    }
    fn expand_user(&mut self, _: &str) -> std::result::Result<String, RecordError> {
        Err(RecordError::Invariant(
            "fork hydration has no host-path authority",
        ))
    }
}
impl SqliteAssistantStore {
    /// A complete detached fork owns this separate admission slot until its
    /// final commit/cleanup. Store shutdown drains queued writes, not these
    /// workflows; shutdown coordination must stop/join application work first.
    pub fn fork_workflow(&self) -> Result<OwnedSemaphorePermit> {
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        self.fork_workflows
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::Capacity)
    }
    pub async fn fork_session(&self, id: &str) -> Result<ForkRecord> {
        id_lookup(id)?;
        let _read = self.read_permit()?;
        let row = sqlx::query(&format!(
            "{SELECT_RECORD} WHERE kind='chat_sessions' AND id=?"
        ))
        .bind(id)
        .fetch_optional(&self.readers)
        .await?
        .ok_or(Error::NotFound)?;
        decode(&row, &mut Budget::default(), true)
    }
    /// Full source transcript before retraction filtering. Sort happens in the
    /// service on hydrated exact integers and UTC timestamps, never SQLite REAL.
    pub async fn fork_messages(&self, session: &str) -> Result<Vec<ForkRecord>> {
        self.fork_collection(AssistantKind::Message, session, None)
            .await
    }
    pub async fn fork_decisions(&self, session: &str, project: &str) -> Result<Vec<ForkRecord>> {
        self.fork_collection(AssistantKind::Decision, session, Some(project))
            .await
    }
    pub async fn fork_goals(&self, session: &str) -> Result<Vec<ForkRecord>> {
        self.fork_collection(AssistantKind::Goal, session, None)
            .await
    }
    async fn fork_collection(
        &self,
        kind: AssistantKind,
        session: &str,
        project: Option<&str>,
    ) -> Result<Vec<ForkRecord>> {
        let _read = self.read_permit()?;
        let mut query = QueryBuilder::new(SELECT_RECORD);
        query.push(" WHERE kind=").push_bind(kind.as_str());
        if let Some(project) = project {
            query
                .push(" AND engagement_id=")
                .push_bind(project)
                .push(" AND (chat_session_id=")
                .push_bind(session)
                .push(" OR json_extract(payload,'$.scope')='project')");
        } else {
            query.push(" AND chat_session_id=").push_bind(session);
        }
        query.push(" ORDER BY created_at,id LIMIT 10001");
        let sql = query.build();
        let mut rows = sql.fetch(&self.readers);
        let mut budget = Budget::default();
        let mut records = Vec::new();
        while let Some(row) = rows.try_next().await? {
            records.push(decode(&row, &mut budget, false)?);
        }
        Ok(records)
    }
    /// A missing vendor last_activity_at invokes only the trusted clock. CPU
    /// hydration is bounded and detached work retains its slot after timeout;
    /// neither a SQL connection nor a reader permit survives into that work.
    pub async fn fork_harness(&self, id: &str, clock: Arc<StateClock>) -> Result<ForkHarness> {
        id_lookup(id)?;
        let slot = self
            .fork_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::DependencyUnavailable)?;
        let row = {
            let _read = self.read_permit()?;
            sqlx::query(&format!(
                "{SELECT_RECORD} WHERE kind='harness_sessions' AND id=?"
            ))
            .bind(id)
            .fetch_optional(&self.readers)
            .await?
            .ok_or(Error::NotFound)?
        };
        let mut budget = Budget::default();
        budget.row(&row)?;
        let task = tokio::task::spawn_blocking(move || {
            let _slot = slot;
            let raw: &str = row.try_get("payload")?;
            let record = StoredDependency::decode_with_environment(
                DependencyKind::HarnessSession,
                raw.as_bytes(),
                &mut ClockEnvironment(clock),
            )
            .map_err(|e| budget.error(e, true))?;
            verify(&row, record.payload(), None)?;
            let result = ForkHarness::new(record, raw)?;
            budget.decoded(result.record.payload(), &result.raw_payload)?;
            Ok(result)
        });
        tokio::time::timeout(Duration::from_secs(5), task)
            .await
            .map_err(|_| Error::DependencyTimeout)?
            .map_err(|_| Error::DependencyUnavailable)?
    }
    pub async fn create_fork_record(&self, record: ForkRecord) -> Result<()> {
        self.fork_command(Operation::Assistant(record)).await
    }
    pub async fn create_fork_harness(&self, record: ForkHarness) -> Result<()> {
        self.fork_command(Operation::Harness(record)).await
    }
    /// Match NebulaStore.delete, not StoreTransaction.delete: cleanup removes
    /// only the vendor entity, without hydration, revision or search mutation.
    pub async fn cleanup_fork_harness(&self, id: &str) -> Result<()> {
        id_lookup(id)?;
        self.fork_command(Operation::Cleanup(id.to_owned())).await
    }
    async fn fork_command(&self, operation: Operation) -> Result<()> {
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        match &operation {
            Operation::Assistant(row)
                if !matches!(
                    row.record.kind(),
                    AssistantKind::Session
                        | AssistantKind::Message
                        | AssistantKind::Decision
                        | AssistantKind::Goal
                ) =>
            {
                return Err(Error::InvalidBounds);
            }
            Operation::Harness(row) if row.record.kind() != DependencyKind::HarnessSession => {
                return Err(Error::InvalidBounds);
            }
            _ => {}
        }
        let bytes = match &operation {
            Operation::Assistant(row) => {
                size(row.record.payload())?.saturating_add(row.raw_payload.len())
            }
            Operation::Harness(row) => {
                size(row.record.payload())?.saturating_add(row.raw_payload.len())
            }
            Operation::Cleanup(id) => id.len(),
        };
        if bytes > MAX_TRANSACTION_BYTES {
            return Err(Error::TransactionLimit);
        }
        let permit = self
            .bytes
            .clone()
            .try_acquire_many_owned(bytes as u32)
            .map_err(|_| Error::Capacity)?;
        let slot = self.commands.try_reserve().map_err(|e| match e {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        slot.send(Command::Fork(ForkRequest {
            operation,
            _bytes: permit,
            reply,
        }));
        result.await.map_err(|_| Error::Closed)?
    }
}
pub(super) async fn write(connection: &mut SqliteConnection, operation: &Operation) -> Result<()> {
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    let (kind, p, raw, session) = match operation {
        Operation::Assistant(row) => (
            row.record.kind().as_str(),
            row.record.payload(),
            &row.raw_payload,
            session_projection(&row.record),
        ),
        Operation::Harness(row) => (
            DependencyKind::HarnessSession.as_str(),
            row.record.payload(),
            &row.raw_payload,
            None,
        ),
        Operation::Cleanup(id) => {
            let result = sqlx::query("DELETE FROM entities WHERE kind='harness_sessions' AND id=?")
                .bind(id)
                .execute(&mut *tx)
                .await
                .map_err(Error::Database)?;
            if result.rows_affected() != 1 {
                return Err(Error::NotFound);
            }
            tx.commit().await?;
            return Ok(());
        }
    };
    // Public raw fragments carry ordering only, never unvalidated values.
    if raw.len() > MAX_RECORD_BYTES || serde_json::from_str::<Value>(raw)? != *p {
        return Err(Error::CorruptEnvelope);
    }
    sqlx::query("INSERT INTO entities(id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)")
        .bind(p["id"].as_str()).bind(kind).bind(p["engagement_id"].as_str()).bind(p["revision"].as_i64().ok_or(Error::CorruptEnvelope)?).bind(raw).bind(session).bind(sql_time(&p["created_at"])?).bind(sql_time(&p["updated_at"])?).execute(&mut *tx).await.map_err(|e|match Error::from(e) {Error::Conflict=>Error::AlreadyExists(p["id"].as_str().unwrap_or_default().into()),e=>e})?;
    if let Operation::Assistant(row) = operation {
        update_search(&mut tx, &row.record).await?;
    } else {
        sqlx::query("DELETE FROM search_documents WHERE id=?")
            .bind(p["id"].as_str())
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(())
}

struct Output(Vec<u8>);
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("fork JSON byte limit"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl Output {
    fn bytes(&mut self, bytes: &[u8]) -> Result<()> {
        self.write_all(bytes).map_err(|_| Error::ReadLimit)
    }
    fn json<T: serde::Serialize>(&mut self, value: &T) -> Result<()> {
        serde_json::to_writer(self, value).map_err(|_| Error::ReadLimit)
    }
}
struct Keys(Vec<String>);
impl<'de> Deserialize<'de> for Keys {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Keys;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a retained object")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Keys, A::Error> {
                let mut keys = Vec::new();
                let mut seen = HashSet::new();
                while let Some((key, _)) = map.next_entry::<String, IgnoredAny>()? {
                    if keys.len() >= 10_000 {
                        return Err(serde::de::Error::custom("fork object exceeds entry bound"));
                    }
                    if seen.insert(key.clone()) {
                        keys.push(key);
                    }
                }
                Ok(Keys(keys))
            }
        }
        d.deserialize_map(V)
    }
}
fn typed_dictionary(path: &[String]) -> bool {
    matches!(path,[key] if key=="metadata")
        || matches!(path,[key,index] if ["mcp_snapshot","history","skill_snapshots","completion_evidence"].contains(&key.as_str()) && index=="[]")
        || matches!(path,[key,index,field] if key=="content_blocks" && index=="[]" && field=="metadata")
}
fn traversed_container(path: &[String]) -> bool {
    path.is_empty()
        || typed_dictionary(path)
        || matches!(path, [key] if ["usage", "content_blocks", "citations", "mcp_snapshot", "history", "skill_snapshots", "completion_evidence"].contains(&key.as_str()))
        || matches!(path, [key, index] if ["content_blocks", "citations"].contains(&key.as_str()) && index == "[]")
}
fn retained_json(original: &str, next: &Value) -> Result<String> {
    if original.len() > MAX_RECORD_BYTES {
        return Err(Error::ReadLimit);
    }
    let mut output = Output(Vec::new());
    preserve(&mut output, original, next, &mut Vec::new())?;
    String::from_utf8(output.0).map_err(|_| Error::CorruptEnvelope)
}
fn preserve(out: &mut Output, raw: &str, next: &Value, path: &mut Vec<String>) -> Result<()> {
    // Descend only through known model/typed-map containers. Arbitrary opaque
    // data may be deep: a changed leaf must not retain a full parsed ancestor
    // at every depth. Equal opaque values still keep their original spelling.
    let before: Value = serde_json::from_str(raw)?;
    if !typed_dictionary(path) && before == *next {
        return out.bytes(raw.as_bytes());
    }
    if !traversed_container(path) {
        return out.json(next);
    }
    if let (Some(_), Some(fields)) = (before.as_object(), next.as_object()) {
        let raw_fields: HashMap<String, &RawValue> = serde_json::from_str(raw)?;
        // JSON/object shape was checked above; this decoder's remaining
        // rejection is its bounded key inventory, not a malformed record.
        let Keys(keys) = serde_json::from_str(raw).map_err(|_| Error::ReadLimit)?;
        let typed = typed_dictionary(path);
        let mut entries = Vec::new();
        let mut positions: HashMap<String, usize> = HashMap::new();
        for key in keys {
            let name = if typed {
                key.trim().to_owned()
            } else {
                key.clone()
            };
            let value = *raw_fields.get(&key).ok_or(Error::CorruptEnvelope)?;
            if let Some(&index) = positions.get(&name) {
                entries[index] = (name, value);
            } else {
                positions.insert(name.clone(), entries.len());
                entries.push((name, value));
            }
        }
        out.bytes(b"{")?;
        let mut count = 0;
        let mut written = HashSet::new();
        for (key, old) in entries {
            let Some(value) = fields.get(&key) else {
                continue;
            };
            if count > 0 {
                out.bytes(b",")?;
            }
            count += 1;
            out.json(&key)?;
            out.bytes(b":")?;
            path.push(key.clone());
            preserve(out, old.get(), value, path)?;
            path.pop();
            written.insert(key);
        }
        for (key, value) in fields {
            if !written.contains(key) {
                if count > 0 {
                    out.bytes(b",")?;
                }
                count += 1;
                out.json(key)?;
                out.bytes(b":")?;
                out.json(value)?;
            }
        }
        out.bytes(b"}")
    } else if let (Some(old), Some(values)) = (before.as_array(), next.as_array()) {
        if old.len() != values.len() {
            return out.json(next);
        }
        let raw_values: Vec<&RawValue> = serde_json::from_str(raw)?;
        out.bytes(b"[")?;
        for (i, (old, value)) in raw_values.iter().zip(values).enumerate() {
            if i > 0 {
                out.bytes(b",")?;
            }
            path.push("[]".into());
            preserve(out, old.get(), value, path)?;
            path.pop();
        }
        out.bytes(b"]")
    } else {
        out.json(next)
    }
}
