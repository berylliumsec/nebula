//! Isolated assistant event journal: bounded writer, durable replay, no execution.

pub mod entities;

use std::{
    collections::HashMap,
    fs::File,
    path::Path,
    sync::{Arc, Mutex, Weak},
    time::Duration,
};

use chrono::Utc;
use fs2::FileExt;
use futures_util::TryStreamExt;
use nebula_assistant_domain::{AppendEvent, AssistantEvent, ValidationError, bounded};
use sqlx::{
    Connection, Row, SqliteConnection, SqlitePool,
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous},
};
use tokio::sync::{mpsc, oneshot, watch};
use uuid::Uuid;

const APPLICATION_ID: i64 = 1_312_964_940;
const SCHEMA_VERSION: i64 = 1;
const REPLAY_BYTES: usize = 4 * 1024 * 1024;
const REPLAY_EVENTS: i64 = 256;
const MAX_SUBSCRIPTIONS: usize = 4096;

#[derive(Clone, Copy, Default)]
struct Progress {
    generation: u64,
    closed: bool,
}

#[derive(Default)]
struct Feeds {
    turns: Mutex<HashMap<String, Weak<watch::Sender<Progress>>>>,
    closed: std::sync::atomic::AtomicBool,
    subscribers: std::sync::atomic::AtomicUsize,
}

impl Feeds {
    fn subscribe(&self, turn: &str) -> Result<Arc<watch::Sender<Progress>>> {
        let mut turns = self
            .turns
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if self.closed.load(std::sync::atomic::Ordering::Acquire) {
            return Err(Error::Closed);
        }
        if self.subscribers.load(std::sync::atomic::Ordering::Relaxed) >= MAX_SUBSCRIPTIONS {
            return Err(Error::SubscriberCapacity);
        }
        let feed = if let Some(feed) = turns.get(turn).and_then(Weak::upgrade) {
            feed
        } else {
            turns.retain(|_, feed| feed.strong_count() > 0);
            let (sender, _) = watch::channel(Progress::default());
            let feed = Arc::new(sender);
            turns.insert(turn.into(), Arc::downgrade(&feed));
            feed
        };
        self.subscribers
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Ok(feed)
    }

    fn committed(&self, turn: &str) {
        let turns = self
            .turns
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(feed) = turns.get(turn).and_then(Weak::upgrade) {
            feed.send_modify(|progress| progress.generation = progress.generation.wrapping_add(1));
        }
    }

    fn close(&self) {
        let turns = self
            .turns
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        self.closed
            .store(true, std::sync::atomic::Ordering::Release);
        for feed in turns.values().filter_map(Weak::upgrade) {
            feed.send_modify(|progress| progress.closed = true);
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Validation(#[from] ValidationError),
    #[error(transparent)]
    Database(#[from] sqlx::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error("assistant writer queue is full; retry admission after capacity is available")]
    Capacity,
    #[error("assistant viewer capacity is full; close an existing subscription before retrying")]
    SubscriberCapacity,
    #[error(
        "assistant journal is closed; inspect durable state before retrying an uncertain write"
    )]
    Closed,
    #[error("idempotency key was reused with different event content")]
    Conflict,
    #[error(
        "expected an assistant laboratory database with schema version 1; refusing to alter this database"
    )]
    IncompatibleDatabase,
    #[error("another writer already owns this assistant laboratory database")]
    AlreadyOwned,
    #[error("writer capacity must be between 1 and 2048, and readers between 1 and 16")]
    InvalidConfiguration,
}

type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, Copy)]
pub struct Config {
    pub writer_capacity: usize,
    pub readers: u32,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            writer_capacity: 128,
            readers: 4,
        }
    }
}

enum Command {
    Append(AppendEvent, oneshot::Sender<Result<AssistantEvent>>),
    Shutdown(oneshot::Sender<Result<()>>),
}

#[derive(Clone)]
pub struct Journal {
    commands: mpsc::Sender<Command>,
    readers: SqlitePool,
    feeds: Arc<Feeds>,
}

impl Journal {
    /// Create a NEW, private laboratory database. Never overwrite an existing file.
    pub async fn create(path: &Path, config: Config) -> Result<Self> {
        Self::validate_config(config)?;
        let mut options = std::fs::OpenOptions::new();
        options.read(true).write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let file = options.open(path)?;
        file.try_lock_exclusive().map_err(|_| Error::AlreadyOwned)?;
        let mut connection = SqliteConnection::connect_with(&Self::options(path)).await?;
        let mut tx = connection.begin().await?;
        sqlx::raw_sql(include_str!("schema.sql"))
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        Self::start(path, config, connection, file).await
    }

    /// Reopen only a journal created by this laboratory, with one writer owner.
    pub async fn open(path: &Path, config: Config) -> Result<Self> {
        Self::validate_config(config)?;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)?;
        file.try_lock_exclusive().map_err(|_| Error::AlreadyOwned)?;
        // Inspect before changing pragmas: even a rejected production database
        // must remain untouched by this development implementation.
        let probe_options = SqliteConnectOptions::new().filename(path).read_only(true);
        let mut probe = SqliteConnection::connect_with(&probe_options).await?;
        let application_id: i64 = sqlx::query_scalar("PRAGMA application_id")
            .fetch_one(&mut probe)
            .await?;
        let version: i64 = sqlx::query_scalar("PRAGMA user_version")
            .fetch_one(&mut probe)
            .await?;
        probe.close().await?;
        if application_id != APPLICATION_ID || version != SCHEMA_VERSION {
            return Err(Error::IncompatibleDatabase);
        }
        let connection = SqliteConnection::connect_with(&Self::options(path)).await?;
        Self::start(path, config, connection, file).await
    }

    fn validate_config(config: Config) -> Result<()> {
        if !(1..=2048).contains(&config.writer_capacity) || !(1..=16).contains(&config.readers) {
            return Err(Error::InvalidConfiguration);
        }
        Ok(())
    }

    fn options(path: &Path) -> SqliteConnectOptions {
        SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(false)
            .journal_mode(SqliteJournalMode::Wal)
            .synchronous(SqliteSynchronous::Normal)
            .foreign_keys(true)
            .busy_timeout(Duration::from_secs(5))
    }

    async fn start(
        path: &Path,
        config: Config,
        connection: SqliteConnection,
        file: File,
    ) -> Result<Self> {
        let readers = SqlitePoolOptions::new()
            .max_connections(config.readers)
            .acquire_timeout(Duration::from_secs(5))
            .connect_with(
                SqliteConnectOptions::new()
                    .filename(path)
                    .read_only(true)
                    .busy_timeout(Duration::from_secs(5)),
            )
            .await?;
        let (commands, receiver) = mpsc::channel(config.writer_capacity);
        let feeds = Arc::new(Feeds::default());
        tokio::spawn(writer(connection, file, receiver, feeds.clone()));
        Ok(Self {
            commands,
            readers,
            feeds,
        })
    }

    /// Capacity rejection means nothing was admitted. Cancellation AFTER send
    /// does not retract a commit: retry with the same idempotency key to resolve it.
    pub async fn append(&self, event: AppendEvent) -> Result<AssistantEvent> {
        event.validate()?;
        let permit = self.commands.try_reserve().map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        permit.send(Command::Append(event, reply));
        result.await.map_err(|_| Error::Closed)?
    }

    pub async fn replay(&self, turn_id: &str, after: i64) -> Result<Vec<AssistantEvent>> {
        bounded(turn_id, 200)?;
        if after < 0 {
            return Err(ValidationError("cursor must be nonnegative").into());
        }
        let mut rows = sqlx::query("SELECT * FROM assistant_events WHERE turn_id = ? AND sequence > ? ORDER BY sequence LIMIT ?")
            .bind(turn_id).bind(after).bind(REPLAY_EVENTS).fetch(&self.readers);
        let mut events = Vec::new();
        let mut bytes = 0;
        while let Some(row) = rows.try_next().await? {
            let payload: String = row.try_get("payload")?;
            if !events.is_empty() && bytes + payload.len() > REPLAY_BYTES {
                break;
            }
            bytes += payload.len();
            events.push(decode(row)?);
        }
        Ok(events)
    }

    /// Subscribe before replay so a concurrent commit cannot be missed.
    pub fn follow(&self, turn_id: &str, after: i64) -> Result<Cursor> {
        bounded(turn_id, 200)?;
        if after < 0 {
            return Err(ValidationError("cursor must be nonnegative").into());
        }
        let feed = self.feeds.subscribe(turn_id)?;
        Ok(Cursor {
            journal: self.clone(),
            notifications: feed.subscribe(),
            feed,
            turn_id: turn_id.into(),
            after,
        })
    }

    /// Stop admission, drain previously accepted messages, close the writer,
    /// then release the read pool. Await this before reopening a journal.
    pub async fn shutdown(self) -> Result<()> {
        let (reply, result) = oneshot::channel();
        self.commands
            .send(Command::Shutdown(reply))
            .await
            .map_err(|_| Error::Closed)?;
        result.await.map_err(|_| Error::Closed)??;
        self.readers.close().await;
        Ok(())
    }
}

pub struct Cursor {
    journal: Journal,
    notifications: watch::Receiver<Progress>,
    feed: Arc<watch::Sender<Progress>>,
    turn_id: String,
    after: i64,
}

impl Cursor {
    pub fn sequence(&self) -> i64 {
        self.after
    }

    pub async fn next_batch(&mut self) -> Result<Vec<AssistantEvent>> {
        loop {
            if self.notifications.borrow_and_update().closed {
                return Err(Error::Closed);
            }
            let events = self.journal.replay(&self.turn_id, self.after).await?;
            if let Some(last) = events.last() {
                self.after = last.sequence;
                return Ok(events);
            }
            self.notifications
                .changed()
                .await
                .map_err(|_| Error::Closed)?;
        }
    }
}

impl Drop for Cursor {
    fn drop(&mut self) {
        let mut turns = self
            .journal
            .feeds
            .turns
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        self.journal
            .feeds
            .subscribers
            .fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
        if Arc::strong_count(&self.feed) == 1 {
            turns.remove(&self.turn_id);
        }
    }
}

async fn writer(
    mut connection: SqliteConnection,
    file: File,
    mut receiver: mpsc::Receiver<Command>,
    feeds: Arc<Feeds>,
) {
    let mut closing = None;
    while let Some(command) = receiver.recv().await {
        match command {
            Command::Append(event, reply) => {
                let result = append_transaction(&mut connection, event).await;
                if let Ok((event, true)) = &result {
                    feeds.committed(&event.turn_id);
                }
                // A disconnected caller cannot undo its admitted durable write.
                let _ = reply.send(result.map(|(event, _)| event));
            }
            Command::Shutdown(reply) => {
                receiver.close();
                if closing.is_none() {
                    closing = Some(reply);
                } else {
                    let _ = reply.send(Err(Error::Closed));
                }
            }
        }
    }
    let result = connection.close().await.map_err(Error::from);
    feeds.close();
    // Explicitly release ownership before acknowledging shutdown.
    drop(file);
    if let Some(reply) = closing {
        let _ = reply.send(result);
    }
}

async fn append_transaction(
    connection: &mut SqliteConnection,
    request: AppendEvent,
) -> Result<(AssistantEvent, bool)> {
    let mut tx = connection.begin().await?;
    if let Some(key) = &request.idempotency_key {
        let existing =
            sqlx::query("SELECT * FROM assistant_events WHERE turn_id = ? AND idempotency_key = ?")
                .bind(&request.turn_id)
                .bind(key)
                .fetch_optional(&mut *tx)
                .await?;
        if let Some(row) = existing {
            let event = decode(row)?;
            if !request.matches(&event) {
                return Err(Error::Conflict);
            }
            tx.commit().await?;
            return Ok((event, false));
        }
    }
    let sequence: i64 = sqlx::query_scalar("INSERT INTO assistant_sequences(turn_id, last_sequence) VALUES (?, 1) ON CONFLICT(turn_id) DO UPDATE SET last_sequence = last_sequence + 1 RETURNING last_sequence")
        .bind(&request.turn_id).fetch_one(&mut *tx).await?;
    let event = AssistantEvent {
        id: Uuid::new_v4().to_string(),
        turn_id: request.turn_id,
        sequence,
        event_type: request.event_type,
        payload: request.payload,
        actor_id: request.actor_id,
        occurred_at: Utc::now(),
        idempotency_key: request.idempotency_key,
    };
    sqlx::query("INSERT INTO assistant_events (id, turn_id, sequence, event_type, payload, actor_id, occurred_at, idempotency_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
        .bind(&event.id).bind(&event.turn_id).bind(sequence).bind(&event.event_type)
        .bind(serde_json::to_string(&event.payload)?).bind(&event.actor_id)
        .bind(event.occurred_at.to_rfc3339()).bind(&event.idempotency_key).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok((event, true))
}

fn decode(row: sqlx::sqlite::SqliteRow) -> Result<AssistantEvent> {
    Ok(AssistantEvent {
        id: row.try_get("id")?,
        turn_id: row.try_get("turn_id")?,
        sequence: row.try_get("sequence")?,
        event_type: row.try_get("event_type")?,
        payload: serde_json::from_str(row.try_get("payload")?)?,
        actor_id: row.try_get("actor_id")?,
        occurred_at: row.try_get("occurred_at")?,
        idempotency_key: row.try_get("idempotency_key")?,
    })
}
