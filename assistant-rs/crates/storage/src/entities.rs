//! Assistant records in Nebula's existing SQLite entity envelope.
//!
//! This layer performs no schema migrations, authorization, dispatch or workflow
//! deletion. Services must enforce those policies before submitting a transaction.
//! Development and migration tests use isolated databases, never live Core state.

use std::{
    fs::File,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use chrono::{DateTime, SecondsFormat, Utc};
use fs2::FileExt;
use futures_util::TryStreamExt;
use nebula_assistant_domain::auth::PairedDevice;
use nebula_assistant_domain::records::{
    AssistantKind, MAX_RECORD_BYTES, RecordError, StoredAssistantRecord,
};
use serde_json::{Map, Value};
use sqlx::{
    Connection, QueryBuilder, Row, Sqlite, SqliteConnection, SqlitePool,
    sqlite::{
        SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteRow, SqliteSynchronous,
    },
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot};

mod catchup;
pub use catchup::{CatchupSnapshot, PendingSnapshot};

const MAX_TRANSACTION_BYTES: usize = 16 * 1024 * 1024;
const MAX_MUTATIONS: usize = 64;
const SELECT_RECORD: &str = "SELECT id, kind, engagement_id, revision, chat_session_id, created_at, updated_at, length(CAST(payload AS BLOB)) AS payload_bytes, CASE WHEN length(CAST(payload AS BLOB)) <= 16777216 THEN payload ELSE NULL END AS payload FROM entities";

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(
        "database is not at supported Nebula schema 5 / 0016_chat_session_lookup; migrate an isolated copy before opening it"
    )]
    IncompatibleSchema,
    #[error("another Rust assistant writer owns this database")]
    AlreadyOwned,
    #[error("assistant storage admission is full; retry after capacity becomes available")]
    Capacity,
    #[error(
        "Assistant collection exceeds 10,000 records or 16 MiB; use paginated record access to inspect the retained history"
    )]
    ReadLimit,
    #[error(
        "assistant storage is closing; inspect durable state before retrying an uncertain mutation"
    )]
    Closed,
    #[error("assistant record was not found")]
    NotFound,
    #[error("assistant record revision or identity conflicts; reload the current record")]
    Conflict,
    #[error("entity already exists: {0}")]
    AlreadyExists(String),
    #[error("revision conflict: expected {expected}, found {found}")]
    RevisionConflict { expected: i64, found: i64 },
    #[error("stored assistant envelope and payload disagree")]
    CorruptEnvelope,
    #[error("invalid assistant storage configuration or query bounds")]
    InvalidBounds,
    #[error("transaction must contain 1 to 64 mutations and at most 16 MiB")]
    TransactionLimit,
    #[error("patches cannot replace id, created_at, updated_at or revision")]
    ProtectedField,
    #[error("assistant revision is exhausted")]
    RevisionExhausted,
    #[error(transparent)]
    Record(#[from] RecordError),
    // Database messages can include values. Do not expose their display strings
    // through an API error; a future transport can attach a diagnostic reference.
    #[error("assistant database operation failed")]
    Database(#[source] sqlx::Error),
    #[error("assistant database file could not be opened")]
    Io(#[source] std::io::Error),
    #[error("assistant record JSON could not be encoded")]
    Json(#[source] serde_json::Error),
}

impl From<sqlx::Error> for Error {
    fn from(error: sqlx::Error) -> Self {
        if matches!(&error, sqlx::Error::Database(error) if error.is_unique_violation()) {
            Self::Conflict
        } else {
            Self::Database(error)
        }
    }
}
impl From<std::io::Error> for Error {
    fn from(error: std::io::Error) -> Self {
        Self::Io(error)
    }
}
impl From<serde_json::Error> for Error {
    fn from(error: serde_json::Error) -> Self {
        Self::Json(error)
    }
}
type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Copy, Debug)]
pub struct Config {
    pub writer_capacity: usize,
    pub queued_bytes: usize,
    pub readers: u32,
    pub read_capacity: usize,
    pub page_bytes: usize,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            writer_capacity: 128,
            queued_bytes: 16 * 1024 * 1024,
            readers: 4,
            read_capacity: 128,
            page_bytes: 4 * 1024 * 1024,
        }
    }
}
impl Config {
    fn validate(self) -> Result<()> {
        if !(1..=2048).contains(&self.writer_capacity)
            || !(1..=64 * 1024 * 1024).contains(&self.queued_bytes)
            || !(1..=16).contains(&self.readers)
            || !(self.readers as usize..=4096).contains(&self.read_capacity)
            || !(1..=16 * 1024 * 1024).contains(&self.page_bytes)
        {
            return Err(Error::InvalidBounds);
        }
        Ok(())
    }
}

pub enum Mutation {
    Create(StoredAssistantRecord),
    Patch {
        kind: AssistantKind,
        id: String,
        expected_revision: i64,
        changes: Map<String, Value>,
    },
    Delete {
        kind: AssistantKind,
        id: String,
        expected_revision: i64,
    },
}

/// Rechecked inside the writer transaction after service validation.
pub struct Precondition {
    pub kind: AssistantKind,
    pub id: String,
    pub revision: i64,
}

/// A primitive entity query, matching legacy `(created_at, id)` ordering.
/// `session_id` uses the indexed projection for the eight session-owned kinds.
/// Peer/subagent relationships need their explicit relationship queries instead.
pub struct ListQuery {
    pub kind: AssistantKind,
    pub engagement_id: Option<String>,
    pub session_id: Option<String>,
    pub statuses: Option<Vec<String>>,
    pub include_temporary: bool,
    pub newest_first: bool,
    pub offset: u64,
    pub limit: u32,
}
impl ListQuery {
    pub fn new(kind: AssistantKind) -> Self {
        Self {
            kind,
            engagement_id: None,
            session_id: None,
            statuses: None,
            include_temporary: false,
            newest_first: false,
            offset: 0,
            limit: 100,
        }
    }
}

#[derive(Debug)]
pub struct Page {
    pub records: Vec<StoredAssistantRecord>,
    /// Continue from this offset when either the row or byte limit was reached.
    /// A first record larger than the page budget is returned alone, so reads
    /// always make progress; the per-record 16 MiB bound still applies.
    pub next_offset: Option<u64>,
}

/// Candidate search over canonical messages. The service supplies Python-stripped
/// text and filters retracted messages only after this stored-row page is formed.
pub struct NavigationQuery {
    pub project_id: String,
    pub session_id: Option<String>,
    pub text: String,
    pub bookmarked: bool,
    pub offset: u64,
    pub limit: u32,
}

/// Existing generated read-only catalog routes return an array, not a cursor.
pub struct GeneratedListQuery {
    pub kind: AssistantKind,
    pub engagement_id: Option<String>,
    pub offset: u64,
    pub limit: u32,
}

#[derive(Debug)]
pub struct Admission {
    pub available_queue_entries: usize,
    pub available_bytes: usize,
    pub available_reads: usize,
}

struct WriteRequest {
    preconditions: Vec<Precondition>,
    mutations: Vec<Mutation>,
    _bytes: OwnedSemaphorePermit,
    reply: oneshot::Sender<Result<Vec<Option<StoredAssistantRecord>>>>,
}
enum Command {
    Apply(WriteRequest),
    TouchDevice {
        id: String,
        revision: i64,
        now: DateTime<Utc>,
        _bytes: OwnedSemaphorePermit,
        reply: oneshot::Sender<Result<bool>>,
    },
    Shutdown(oneshot::Sender<Result<()>>),
}

#[derive(Clone)]
pub struct SqliteAssistantStore {
    commands: mpsc::Sender<Command>,
    readers: SqlitePool,
    bytes: Arc<Semaphore>,
    read_slots: Arc<Semaphore>,
    closing: Arc<AtomicBool>,
    page_bytes: usize,
}

impl SqliteAssistantStore {
    /// An instantaneous diagnostic snapshot, not a reservation or a promise
    /// that a subsequent admission will succeed.
    pub fn admission(&self) -> Admission {
        Admission {
            available_queue_entries: self.commands.capacity(),
            available_bytes: self.bytes.available_permits(),
            available_reads: self.read_slots.available_permits(),
        }
    }

    /// Open an existing current-schema database. Caller owns process-level
    /// cutover: this lock excludes other Rust stores, not a legacy Python Core.
    pub async fn open(path: &Path, config: Config) -> Result<Self> {
        config.validate()?;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)?;
        file.try_lock_exclusive().map_err(|error| {
            if error.kind() == std::io::ErrorKind::WouldBlock {
                Error::AlreadyOwned
            } else {
                Error::Io(error)
            }
        })?;
        // Refuse unknown/future schemas before changing journal or durability
        // pragmas. No version marker or other product's schema is rewritten.
        let mut probe = SqliteConnection::connect_with(&read_options(path)).await?;
        validate_schema(&mut probe).await?;
        probe.close().await?;
        let connection = SqliteConnection::connect_with(
            &SqliteConnectOptions::new()
                .filename(path)
                .create_if_missing(false)
                .foreign_keys(true)
                .journal_mode(SqliteJournalMode::Wal)
                .synchronous(SqliteSynchronous::Normal)
                .busy_timeout(Duration::from_secs(5)),
        )
        .await?;
        let readers = SqlitePoolOptions::new()
            .max_connections(config.readers)
            .acquire_timeout(Duration::from_secs(5))
            .connect_with(read_options(path))
            .await?;
        let (commands, receiver) = mpsc::channel(config.writer_capacity);
        let closing = Arc::new(AtomicBool::new(false));
        tokio::spawn(writer(connection, file, receiver, closing.clone()));
        Ok(Self {
            commands,
            readers,
            bytes: Arc::new(Semaphore::new(config.queued_bytes)),
            read_slots: Arc::new(Semaphore::new(config.read_capacity)),
            closing,
            page_bytes: config.page_bytes,
        })
    }

    /// Only the successful return acknowledges durability. Dropping this future
    /// after enqueue does not retract the transaction: inspect its identities
    /// and revisions before retrying. A Capacity error means nothing was queued.
    pub async fn apply(
        &self,
        mutations: Vec<Mutation>,
    ) -> Result<Vec<Option<StoredAssistantRecord>>> {
        self.apply_guarded(Vec::new(), mutations).await
    }

    pub async fn apply_guarded(
        &self,
        preconditions: Vec<Precondition>,
        mutations: Vec<Mutation>,
    ) -> Result<Vec<Option<StoredAssistantRecord>>> {
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        let mut bytes = request_size(&mutations)?;
        if preconditions.len() > 64 {
            return Err(Error::TransactionLimit);
        }
        for guard in &preconditions {
            validate_id(&guard.id)?;
            if guard.revision < 1 {
                return Err(Error::InvalidBounds);
            }
            bytes += guard.id.len() + 64;
        }
        if bytes > MAX_TRANSACTION_BYTES {
            return Err(Error::TransactionLimit);
        }
        let byte_permit = self
            .bytes
            .clone()
            .try_acquire_many_owned(bytes as u32)
            .map_err(|_| Error::Capacity)?;
        let slot = self.commands.try_reserve().map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        slot.send(Command::Apply(WriteRequest {
            preconditions,
            mutations,
            _bytes: byte_permit,
            reply,
        }));
        result.await.map_err(|_| Error::Closed)?
    }

    fn read_permit(&self) -> Result<OwnedSemaphorePermit> {
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        self.read_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::Capacity)
    }

    pub async fn get(&self, kind: AssistantKind, id: &str) -> Result<StoredAssistantRecord> {
        validate_id(id)?;
        let _permit = self.read_permit()?;
        let row = sqlx::query(&format!("{SELECT_RECORD} WHERE kind = ? AND id = ?"))
            .bind(kind.as_str())
            .bind(id)
            .fetch_optional(&self.readers)
            .await?
            .ok_or(Error::NotFound)?;
        decode_row(row)
    }

    /// Shared authentication dependency only; Assistant cannot create, revoke,
    /// or change device permissions through this storage surface.
    pub async fn paired_device(&self, token_sha256: &str) -> Result<Option<PairedDevice>> {
        if token_sha256.len() != 64
            || !token_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(Error::InvalidBounds);
        }
        let _permit = self.read_permit()?;
        let query = format!(
            "{SELECT_RECORD} WHERE kind = 'paired_device_sessions' AND json_extract(payload, '$.token_sha256') = ? ORDER BY created_at, id LIMIT 1"
        );
        sqlx::query(&query)
            .bind(token_sha256)
            .fetch_optional(&self.readers)
            .await?
            .map(decode_device)
            .transpose()
    }

    /// Revalidate expiry/revocation inside the writer before extending idle
    /// expiry. A conflict requires a fresh authentication check.
    pub async fn touch_device(&self, id: &str, revision: i64, now: DateTime<Utc>) -> Result<bool> {
        validate_id(id)?;
        if revision < 1 {
            return Err(Error::InvalidBounds);
        }
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        let bytes = self
            .bytes
            .clone()
            .try_acquire_many_owned((id.len() + 128) as u32)
            .map_err(|_| Error::Capacity)?;
        let slot = self.commands.try_reserve().map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        slot.send(Command::TouchDevice {
            id: id.into(),
            revision,
            now,
            _bytes: bytes,
            reply,
        });
        result.await.map_err(|_| Error::Closed)?
    }

    pub async fn list(&self, query: ListQuery) -> Result<Page> {
        if !(1..=1000).contains(&query.limit)
            || query.offset > (i64::MAX - 1001) as u64
            || query.session_id.is_some() && !session_owned(query.kind)
            || query
                .statuses
                .as_ref()
                .is_some_and(|items| items.len() > 64 || items.iter().any(|s| s.len() > 200))
        {
            return Err(Error::InvalidBounds);
        }
        let _permit = self.read_permit()?;
        let mut sql = QueryBuilder::<Sqlite>::new(SELECT_RECORD);
        sql.push(" WHERE kind = ").push_bind(query.kind.as_str());
        if let Some(project) = &query.engagement_id {
            sql.push(" AND engagement_id = ").push_bind(project);
        }
        if let Some(session) = &query.session_id {
            sql.push(" AND chat_session_id = ").push_bind(session);
        }
        if query.kind == AssistantKind::Session && !query.include_temporary {
            sql.push(
                " AND coalesce(json_extract(payload, '$.metadata.temporary_assistant'), 0) IS 0",
            );
        }
        if let Some(statuses) = &query.statuses {
            if statuses.is_empty() {
                return Ok(Page {
                    records: vec![],
                    next_offset: None,
                });
            }
            sql.push(" AND json_extract(payload, '$.status') IN (");
            let mut values = sql.separated(", ");
            for status in statuses {
                values.push_bind(status);
            }
            values.push_unseparated(")");
        }
        sql.push(if query.newest_first {
            " ORDER BY created_at DESC, id DESC"
        } else {
            " ORDER BY created_at, id"
        })
        .push(" LIMIT ")
        .push_bind(i64::from(query.limit) + 1)
        .push(" OFFSET ")
        .push_bind(query.offset as i64);
        let statement = sql.build();
        let mut rows = statement.fetch(&self.readers);
        let mut records = Vec::new();
        let mut bytes = 0usize;
        while let Some(row) = rows.try_next().await? {
            let size = row.try_get::<i64, _>("payload_bytes")?;
            if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
                return Err(RecordError::TooLarge.into());
            }
            if records.len() == query.limit as usize
                || !records.is_empty() && bytes + size as usize > self.page_bytes
            {
                return Ok(Page {
                    next_offset: Some(query.offset + records.len() as u64),
                    records,
                });
            }
            bytes += size as usize;
            records.push(decode_row(row)?);
        }
        Ok(Page {
            records,
            next_offset: None,
        })
    }

    pub async fn list_complete_page(
        &self,
        query: GeneratedListQuery,
    ) -> Result<Vec<StoredAssistantRecord>> {
        if !matches!(query.kind, AssistantKind::Session | AssistantKind::Message)
            || !(1..=1000).contains(&query.limit)
            || query.offset > i64::MAX as u64
        {
            return Err(Error::InvalidBounds);
        }
        let _permit = self.read_permit()?;
        let mut sql = QueryBuilder::<Sqlite>::new(SELECT_RECORD);
        sql.push(" WHERE kind = ").push_bind(query.kind.as_str());
        if let Some(project) = &query.engagement_id {
            sql.push(" AND engagement_id = ").push_bind(project);
        }
        if query.kind == AssistantKind::Session {
            sql.push(
                " AND coalesce(json_extract(payload, '$.metadata.temporary_assistant'), 0) IS 0",
            );
        }
        sql.push(" ORDER BY created_at, id LIMIT ")
            .push_bind(i64::from(query.limit))
            .push(" OFFSET ")
            .push_bind(query.offset as i64);
        complete_records(&mut sql, &self.readers, query.limit as usize).await
    }

    pub async fn latest_message_sequence(&self, session_id: &str) -> Result<i64> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        Ok(sqlx::query_scalar("SELECT coalesce(max(json_extract(payload, '$.sequence')), 0) FROM entities WHERE kind = 'chat_messages' AND chat_session_id = ?")
            .bind(session_id).fetch_one(&self.readers).await?)
    }

    /// Read-only shared project identity dependency. Project schema ownership and
    /// mutation stay outside Assistant; this does not decode an Engagement model.
    pub async fn project_exists(&self, project_id: &str) -> Result<bool> {
        validate_id(project_id)?;
        let _permit = self.read_permit()?;
        Ok(sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM entities WHERE kind = 'engagements' AND id = ?)",
        )
        .bind(project_id)
        .fetch_one(&self.readers)
        .await?)
    }

    /// Complete, bounded transcript snapshot, including retracted messages.
    /// Sorting exact integer sequences in Rust avoids SQLite converting large
    /// Python integers to imprecise REAL values during JSON extraction.
    pub async fn session_messages(&self, session_id: &str) -> Result<Vec<StoredAssistantRecord>> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut query = QueryBuilder::<Sqlite>::new(SELECT_RECORD);
        query
            .push(" WHERE kind = 'chat_messages' AND chat_session_id = ")
            .push_bind(session_id)
            .push(" ORDER BY created_at, id LIMIT 10001");
        let records = complete_records(&mut query, &self.readers, 10000).await?;
        let mut keyed = Vec::with_capacity(records.len());
        for record in records {
            let sequence = record.payload()["sequence"]
                .as_number()
                .ok_or(Error::CorruptEnvelope)?
                .to_string();
            if !sequence.bytes().all(|c| c.is_ascii_digit()) {
                return Err(Error::CorruptEnvelope);
            }
            keyed.push((sequence, record));
        }
        // Stable sorting preserves the SQL (created_at, id) order for ties.
        keyed.sort_by(|(left, _), (right, _)| {
            left.len().cmp(&right.len()).then_with(|| left.cmp(right))
        });
        Ok(keyed.into_iter().map(|(_, record)| record).collect())
    }

    /// Inactive marks remain visible so clients can retain their saved revision.
    /// Match the Python collection query, which does not specify an order.
    pub async fn bookmarks(
        &self,
        session_id: &str,
        project_id: &str,
    ) -> Result<Vec<StoredAssistantRecord>> {
        validate_id(session_id)?;
        validate_id(project_id)?;
        let _permit = self.read_permit()?;
        let mut query = QueryBuilder::<Sqlite>::new(SELECT_RECORD);
        query
            .push(" WHERE kind = 'chat_bookmarks' AND engagement_id = ")
            .push_bind(project_id)
            .push(" AND chat_session_id = ")
            .push_bind(session_id)
            .push(" LIMIT 10001");
        complete_records(&mut query, &self.readers, 10000).await
    }

    /// The page cursor counts stored candidates before retracted-message
    /// filtering. Byte exhaustion fails the whole page; it cannot change offsets
    /// or silently hide retained messages.
    pub async fn search_messages(&self, query: NavigationQuery) -> Result<Page> {
        validate_id(&query.project_id)?;
        if !(1..=100).contains(&query.limit)
            || query.text.chars().count() > 512
            || query.offset > (i64::MAX - 101) as u64
        {
            return Err(Error::InvalidBounds);
        }
        let _permit = self.read_permit()?;
        let mut sql = QueryBuilder::<Sqlite>::new(SELECT_RECORD);
        sql.push(" WHERE kind = 'chat_messages' AND engagement_id = ")
            .push_bind(&query.project_id)
            .push(" AND EXISTS (SELECT 1 FROM entities AS visible_chat WHERE visible_chat.kind = 'chat_sessions' AND visible_chat.id = entities.chat_session_id AND coalesce(json_extract(visible_chat.payload, '$.metadata.temporary_assistant'), 0) IS 0)");
        if let Some(session) = query.session_id.as_deref().filter(|id| !id.is_empty()) {
            validate_id(session)?;
            sql.push(" AND chat_session_id = ").push_bind(session);
        }
        if !query.text.is_empty() {
            let escaped = query
                .text
                .replace('/', "//")
                .replace('%', "/%")
                .replace('_', "/_");
            sql.push(" AND lower(json_extract(payload, '$.content')) LIKE '%' || lower(")
                .push_bind(escaped)
                .push(") || '%' ESCAPE '/'");
        }
        if query.bookmarked {
            sql.push(" AND EXISTS (SELECT 1 FROM entities AS mark WHERE mark.kind = 'chat_bookmarks' AND mark.engagement_id = ")
                .push_bind(&query.project_id)
                .push(" AND json_extract(mark.payload, '$.message_id') = entities.id AND json_extract(mark.payload, '$.active') IS 1)");
        }
        sql.push(" ORDER BY created_at, id LIMIT ")
            .push_bind(i64::from(query.limit) + 1)
            .push(" OFFSET ")
            .push_bind(query.offset as i64);
        let mut records =
            complete_records(&mut sql, &self.readers, query.limit as usize + 1).await?;
        let next_offset =
            (records.len() > query.limit as usize).then_some(query.offset + u64::from(query.limit));
        records.truncate(query.limit as usize);
        Ok(Page {
            records,
            next_offset,
        })
    }

    /// One SQLite statement/snapshot, so a concurrent promotion cannot fall
    /// between pages. Never return a silently incomplete context collection.
    pub async fn decisions(
        &self,
        session_id: &str,
        project_id: &str,
        active_only: bool,
    ) -> Result<Vec<StoredAssistantRecord>> {
        validate_id(session_id)?;
        validate_id(project_id)?;
        let _permit = self.read_permit()?;
        let query = format!(
            "{SELECT_RECORD} WHERE kind = 'chat_decisions' AND engagement_id = ? AND (chat_session_id = ? OR json_extract(payload, '$.scope') = 'project') AND (? = 0 OR json_extract(payload, '$.status') = 'active') ORDER BY created_at, id LIMIT 10001"
        );
        let mut rows = sqlx::query(&query)
            .bind(project_id)
            .bind(session_id)
            .bind(active_only)
            .fetch(&self.readers);
        let mut records = Vec::new();
        let mut bytes = 0usize;
        while let Some(row) = rows.try_next().await? {
            let size: i64 = row.try_get("payload_bytes")?;
            if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
                return Err(RecordError::TooLarge.into());
            }
            bytes += size as usize;
            if records.len() == 10000 || bytes > 16 * 1024 * 1024 {
                return Err(Error::ReadLimit);
            }
            records.push(decode_row(row)?);
        }
        Ok(records)
    }

    pub async fn shutdown(&self) -> Result<()> {
        if self.closing.swap(true, Ordering::AcqRel) {
            return Err(Error::Closed);
        }
        let (reply, result) = oneshot::channel();
        self.commands
            .send(Command::Shutdown(reply))
            .await
            .map_err(|_| Error::Closed)?;
        let outcome = result.await.map_err(|_| Error::Closed)?;
        self.readers.close().await;
        outcome
    }
}

async fn complete_records(
    query: &mut QueryBuilder<'_, Sqlite>,
    readers: &SqlitePool,
    limit: usize,
) -> Result<Vec<StoredAssistantRecord>> {
    let statement = query.build();
    let mut rows = statement.fetch(readers);
    let mut records = Vec::new();
    let mut bytes = 0usize;
    while let Some(row) = rows.try_next().await? {
        let size: i64 = row.try_get("payload_bytes")?;
        if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
            return Err(RecordError::TooLarge.into());
        }
        bytes += size as usize;
        if records.len() == limit || bytes > 16 * 1024 * 1024 {
            return Err(Error::ReadLimit);
        }
        records.push(decode_row(row)?);
    }
    Ok(records)
}

fn read_options(path: &Path) -> SqliteConnectOptions {
    SqliteConnectOptions::new()
        .filename(path)
        .read_only(true)
        .foreign_keys(true)
        .busy_timeout(Duration::from_secs(5))
        // SQLx otherwise prefetches 50 rows, each potentially 16 MiB, before
        // our collection byte accounting can stop the stream.
        .row_buffer_size(1)
        .pragma("query_only", "ON")
}

async fn validate_schema(connection: &mut SqliteConnection) -> Result<()> {
    let result: std::result::Result<_, sqlx::Error> = async {
        let version: Option<i64> = sqlx::query_scalar("SELECT max(version) FROM schema_versions").fetch_one(&mut *connection).await?;
        let revisions: Vec<String> = sqlx::query_scalar("SELECT version_num FROM alembic_version").fetch_all(&mut *connection).await?;
        sqlx::query("SELECT id, kind, engagement_id, revision, payload, chat_session_id, created_at, updated_at FROM entities LIMIT 0").execute(&mut *connection).await?;
        sqlx::query("SELECT id, project_id, resource_kind, resource_id, revision, label, description, breadcrumb, content, updated_at FROM search_documents LIMIT 0").execute(&mut *connection).await?;
        let index_columns: Vec<String> = sqlx::query_scalar("SELECT name FROM pragma_index_info('ix_entities_kind_chat_session_created') ORDER BY seqno").fetch_all(&mut *connection).await?;
        Ok((version, revisions, index_columns))
    }.await;
    match result {
        Ok((Some(5), revisions, columns))
            if revisions == ["0016_chat_session_lookup"]
                && columns == ["kind", "chat_session_id", "created_at", "id"] =>
        {
            Ok(())
        }
        _ => Err(Error::IncompatibleSchema),
    }
}

fn validate_id(id: &str) -> Result<()> {
    if id.is_empty() || id.chars().count() > 200 {
        Err(Error::InvalidBounds)
    } else {
        Ok(())
    }
}
fn request_size(mutations: &[Mutation]) -> Result<usize> {
    if mutations.is_empty() || mutations.len() > MAX_MUTATIONS {
        return Err(Error::TransactionLimit);
    }
    let mut bytes = 0usize;
    for mutation in mutations {
        bytes = bytes
            .checked_add(match mutation {
                Mutation::Create(record) => serde_json::to_vec(record.payload())?.len(),
                Mutation::Patch {
                    id,
                    expected_revision,
                    changes,
                    ..
                } => {
                    validate_id(id)?;
                    if *expected_revision < 1 {
                        return Err(Error::InvalidBounds);
                    }
                    if ["id", "created_at", "updated_at", "revision"]
                        .iter()
                        .any(|key| changes.contains_key(*key))
                    {
                        return Err(Error::ProtectedField);
                    }
                    id.len() + serde_json::to_vec(changes)?.len() + 64
                }
                Mutation::Delete {
                    id,
                    expected_revision,
                    ..
                } => {
                    validate_id(id)?;
                    if *expected_revision < 1 {
                        return Err(Error::InvalidBounds);
                    }
                    id.len() + 64
                }
            })
            .ok_or(Error::TransactionLimit)?;
        if bytes > MAX_TRANSACTION_BYTES {
            return Err(Error::TransactionLimit);
        }
    }
    Ok(bytes.max(1))
}

fn session_owned(kind: AssistantKind) -> bool {
    matches!(
        kind,
        AssistantKind::Bookmark
            | AssistantKind::Decision
            | AssistantKind::Goal
            | AssistantKind::Message
            | AssistantKind::Queue
            | AssistantKind::ReadCursor
            | AssistantKind::Schedule
            | AssistantKind::Turn
    )
}
fn session_projection(record: &StoredAssistantRecord) -> Option<&str> {
    if session_owned(record.kind()) {
        record.payload()["session_id"].as_str()
    } else {
        None
    }
}
fn record_revision(record: &StoredAssistantRecord) -> Result<i64> {
    record.payload()["revision"]
        .as_i64()
        .filter(|v| *v >= 1)
        .ok_or(Error::CorruptEnvelope)
}
fn sql_time(value: &Value) -> Result<String> {
    let time = value
        .as_str()
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .ok_or(Error::CorruptEnvelope)?;
    Ok(time
        .with_timezone(&Utc)
        .format("%Y-%m-%d %H:%M:%S%.6f")
        .to_string())
}
fn decode_row(row: SqliteRow) -> Result<StoredAssistantRecord> {
    let size: i64 = row.try_get("payload_bytes")?;
    if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
        return Err(RecordError::TooLarge.into());
    }
    let payload: String = row.try_get("payload")?;
    let kind = AssistantKind::try_from(row.try_get::<&str, _>("kind")?)?;
    let record = StoredAssistantRecord::decode_persisted(kind, payload.as_bytes())?;
    let p = record.payload();
    if p["id"].as_str() != Some(row.try_get::<&str, _>("id")?)
        || record_revision(&record)? != row.try_get::<i64, _>("revision")?
        || p["engagement_id"].as_str() != row.try_get::<Option<&str>, _>("engagement_id")?
        || session_projection(&record) != row.try_get::<Option<&str>, _>("chat_session_id")?
        || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
        || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
    {
        return Err(Error::CorruptEnvelope);
    }
    Ok(record)
}

fn decode_device(row: SqliteRow) -> Result<PairedDevice> {
    let size: i64 = row.try_get("payload_bytes")?;
    if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
        return Err(RecordError::TooLarge.into());
    }
    let payload: String = row.try_get("payload")?;
    let device = PairedDevice::decode(payload.as_bytes())?;
    if device.id() != row.try_get::<&str, _>("id")?
        || device.revision() != row.try_get::<i64, _>("revision")?
        || row.try_get::<&str, _>("kind")? != "paired_device_sessions"
        || row.try_get::<Option<&str>, _>("engagement_id")?.is_some()
        || row.try_get::<Option<&str>, _>("chat_session_id")?.is_some()
        || sql_time(&device.payload()["created_at"])? != row.try_get::<&str, _>("created_at")?
        || sql_time(&device.payload()["updated_at"])? != row.try_get::<&str, _>("updated_at")?
    {
        return Err(Error::CorruptEnvelope);
    }
    Ok(device)
}

async fn writer(
    mut connection: SqliteConnection,
    file: File,
    mut receiver: mpsc::Receiver<Command>,
    closing: Arc<AtomicBool>,
) {
    let mut shutdown = None;
    while let Some(command) = receiver.recv().await {
        match command {
            Command::Apply(request) => {
                let result =
                    apply_transaction(&mut connection, request.preconditions, request.mutations)
                        .await;
                let _ = request.reply.send(result);
            }
            Command::TouchDevice {
                id,
                revision,
                now,
                _bytes,
                reply,
            } => {
                let outcome = touch_device_transaction(&mut connection, &id, revision, now).await;
                let _ = reply.send(outcome);
            }
            Command::Shutdown(reply) => {
                receiver.close();
                shutdown = Some(reply);
            }
        }
    }
    closing.store(true, Ordering::Release);
    let result = connection.close().await.map_err(Error::from);
    drop(file);
    if let Some(reply) = shutdown {
        let _ = reply.send(result);
    }
}

async fn touch_device_transaction(
    connection: &mut SqliteConnection,
    id: &str,
    revision: i64,
    now: DateTime<Utc>,
) -> Result<bool> {
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    let row = sqlx::query(&format!(
        "{SELECT_RECORD} WHERE kind='paired_device_sessions' AND id=?"
    ))
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(row) = row else { return Ok(false) };
    let device = decode_device(row)?;
    if !device.valid_at(now) {
        return Ok(false);
    }
    if device.revision() != revision {
        return Err(Error::Conflict);
    }
    if device.refresh_due(now) {
        let refreshed = device.refreshed(now)?;
        sqlx::query("UPDATE entities SET payload=?, revision=?, updated_at=? WHERE kind='paired_device_sessions' AND id=? AND revision=?")
            .bind(serde_json::to_string(refreshed.payload())?).bind(refreshed.revision()).bind(sql_time(&refreshed.payload()["updated_at"])?).bind(id).bind(revision).execute(&mut *tx).await?;
    }
    tx.commit().await?;
    Ok(true)
}

async fn apply_transaction(
    connection: &mut SqliteConnection,
    preconditions: Vec<Precondition>,
    mutations: Vec<Mutation>,
) -> Result<Vec<Option<StoredAssistantRecord>>> {
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    for guard in preconditions {
        let revision: Option<i64> =
            sqlx::query_scalar("SELECT revision FROM entities WHERE kind = ? AND id = ?")
                .bind(guard.kind.as_str())
                .bind(guard.id)
                .fetch_optional(&mut *tx)
                .await?;
        if revision != Some(guard.revision) {
            return Err(Error::Conflict);
        }
    }
    let mut changed = Vec::with_capacity(mutations.len());
    let mut changed_bytes = 0usize;
    for mutation in mutations {
        match mutation {
            Mutation::Create(record) => {
                let record = StoredAssistantRecord::decode_persisted(
                    record.kind(),
                    &serde_json::to_vec(record.payload())?,
                )?;
                let p = record.payload();
                sqlx::query("INSERT INTO entities (id, kind, engagement_id, revision, payload, chat_session_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
                    .bind(p["id"].as_str()).bind(record.kind().as_str()).bind(p["engagement_id"].as_str())
                    .bind(record_revision(&record)?).bind(serde_json::to_string(p)?).bind(session_projection(&record))
                    .bind(sql_time(&p["created_at"])?).bind(sql_time(&p["updated_at"])?).execute(&mut *tx).await.map_err(|error| match Error::from(error) {
                        Error::Conflict => Error::AlreadyExists(p["id"].as_str().unwrap_or_default().into()),
                        error => error,
                    })?;
                update_search(&mut tx, &record).await?;
                changed.push(Some(record));
            }
            Mutation::Patch {
                kind,
                id,
                expected_revision,
                changes,
            } => {
                let row = sqlx::query(&format!("{SELECT_RECORD} WHERE id = ? AND kind = ?"))
                    .bind(&id)
                    .bind(kind.as_str())
                    .fetch_optional(&mut *tx)
                    .await?
                    .ok_or(Error::NotFound)?;
                let current = decode_row(row)?;
                if record_revision(&current)? != expected_revision {
                    return Err(Error::RevisionConflict {
                        expected: expected_revision,
                        found: record_revision(&current)?,
                    });
                }
                let mut payload = current.into_payload();
                payload
                    .as_object_mut()
                    .ok_or(Error::CorruptEnvelope)?
                    .extend(changes);
                payload["revision"] = Value::from(
                    expected_revision
                        .checked_add(1)
                        .ok_or(Error::RevisionExhausted)?,
                );
                payload["updated_at"] =
                    Value::String(Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true));
                let record =
                    StoredAssistantRecord::decode_persisted(kind, &serde_json::to_vec(&payload)?)?;
                let result = sqlx::query("UPDATE entities SET payload = ?, engagement_id = ?, chat_session_id = ?, revision = ?, updated_at = ? WHERE id = ? AND kind = ? AND revision = ?")
                    .bind(serde_json::to_string(record.payload())?).bind(record.payload()["engagement_id"].as_str()).bind(session_projection(&record))
                    .bind(record_revision(&record)?).bind(sql_time(&payload["updated_at"])?).bind(id).bind(kind.as_str()).bind(expected_revision)
                    .execute(&mut *tx).await?;
                if result.rows_affected() != 1 {
                    return Err(Error::Conflict);
                }
                update_search(&mut tx, &record).await?;
                changed.push(Some(record));
            }
            Mutation::Delete {
                kind,
                id,
                expected_revision,
            } => {
                let revision: Option<i64> =
                    sqlx::query_scalar("SELECT revision FROM entities WHERE id = ? AND kind = ?")
                        .bind(&id)
                        .bind(kind.as_str())
                        .fetch_optional(&mut *tx)
                        .await?;
                if revision.ok_or(Error::NotFound)? != expected_revision {
                    return Err(Error::Conflict);
                }
                sqlx::query("DELETE FROM search_documents WHERE id = ?")
                    .bind(&id)
                    .execute(&mut *tx)
                    .await?;
                sqlx::query("DELETE FROM entities WHERE id = ? AND kind = ? AND revision = ?")
                    .bind(id)
                    .bind(kind.as_str())
                    .bind(expected_revision)
                    .execute(&mut *tx)
                    .await?;
                changed.push(None);
            }
        }
        // A tiny patch can expand into a large existing record. Bound the
        // actual retained response as well as the queued request; exceeding
        // either limit rolls the entire transaction back.
        if let Some(Some(record)) = changed.last() {
            changed_bytes += serde_json::to_vec(record.payload())?.len();
            if changed_bytes > MAX_TRANSACTION_BYTES {
                return Err(Error::TransactionLimit);
            }
        }
    }
    tx.commit().await?;
    Ok(changed)
}

async fn update_search(
    connection: &mut SqliteConnection,
    record: &StoredAssistantRecord,
) -> Result<()> {
    let p = record.payload();
    let id = p["id"].as_str().ok_or(Error::CorruptEnvelope)?;
    if record.kind() != AssistantKind::Session || p["metadata"]["temporary_assistant"] == true {
        sqlx::query("DELETE FROM search_documents WHERE id = ?")
            .bind(id)
            .execute(connection)
            .await?;
        return Ok(());
    }
    let label: String = p["title"]
        .as_str()
        .ok_or(Error::CorruptEnvelope)?
        .trim()
        .chars()
        .take(500)
        .collect();
    let description: String = p["model"]
        .as_str()
        .ok_or(Error::CorruptEnvelope)?
        .trim()
        .chars()
        .take(300)
        .collect();
    sqlx::query("INSERT INTO search_documents (id, project_id, resource_kind, resource_id, revision, label, description, breadcrumb, content, updated_at) VALUES (?, ?, 'conversation', ?, ?, ?, ?, 'Workbench', '', ?) ON CONFLICT(id) DO UPDATE SET project_id = excluded.project_id, resource_kind = excluded.resource_kind, resource_id = excluded.resource_id, revision = excluded.revision, label = excluded.label, description = excluded.description, breadcrumb = excluded.breadcrumb, content = excluded.content, updated_at = excluded.updated_at")
        .bind(id).bind(p["engagement_id"].as_str()).bind(id).bind(record_revision(record)?)
        .bind(if label.is_empty() { "Conversation" } else { &label }).bind(description).bind(sql_time(&p["updated_at"])?).execute(connection).await?;
    Ok(())
}
