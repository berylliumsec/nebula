//! Saved Assistant preferences, goal and schedule configuration. No dispatch.
use super::*;
use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};
use serde::{
    Deserialize, Deserializer,
    de::{IgnoredAny, MapAccess, Visitor},
};
use serde_json::value::RawValue;
use std::{
    collections::{HashMap, HashSet},
    io::Write,
};

pub struct RawSession {
    pub record: StoredAssistantRecord,
    pub raw_payload: String,
}
impl std::fmt::Debug for RawSession {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RawSession")
            .field("kind", &self.record.kind())
            .finish_non_exhaustive()
    }
}
#[derive(Debug)]
pub struct McpProfileRow {
    pub id: String,
    pub record: Result<Option<StoredDependency>>,
}
pub(super) struct SettingsRequest {
    kind: AssistantKind,
    id: String,
    expected_revision: String,
    changes: Map<String, Value>,
    initial_raw: Option<String>,
    clock: Arc<StateClock>,
    _bytes: OwnedSemaphorePermit,
    pub(super) reply: oneshot::Sender<Result<RawSession>>,
}
#[derive(Default)]
struct Budget {
    rows: usize,
    bytes: usize,
}
impl Budget {
    fn add(&mut self, bytes: usize) -> Result<()> {
        self.rows += 1;
        self.bytes = self.bytes.saturating_add(bytes);
        if self.rows > 10_000 || self.bytes > MAX_TRANSACTION_BYTES {
            return Err(Error::ReadLimit);
        }
        Ok(())
    }
    fn row(&mut self, row: &SqliteRow, copies: usize) -> Result<()> {
        let bytes: i64 = row.try_get("payload_bytes")?;
        if bytes < 0 || bytes as u64 > MAX_RECORD_BYTES as u64 {
            return Err(RecordError::TooLarge.into());
        }
        self.add(bytes as usize * copies)
    }
}

impl SqliteAssistantStore {
    pub async fn settings_session(&self, id: &str) -> Result<RawSession> {
        validate_id(id)?;
        let _permit = self.read_permit()?;
        let row = sqlx::query(&format!(
            "{SELECT_RECORD} WHERE kind='chat_sessions' AND id=?"
        ))
        .bind(id)
        .fetch_optional(&self.readers)
        .await?
        .ok_or(Error::NotFound)?;
        Budget::default().row(&row, 2)?;
        Ok(RawSession {
            record: decode_row_ref(&row)?,
            raw_payload: row.try_get("payload")?,
        })
    }

    pub async fn mcp_profiles_snapshot(&self, ids: &[String]) -> Result<Vec<McpProfileRow>> {
        if ids.len() > 64 {
            return Err(Error::InvalidBounds);
        }
        let mut budget = Budget::default();
        for id in ids {
            budget.add(id.len())?;
        }
        let parameters = serde_json::to_string(ids)?;
        budget.add(parameters.len())?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut profiles = Vec::with_capacity(ids.len());
        let statement = sqlx::query("WITH refs AS (SELECT CAST(key AS INTEGER) AS position,value AS requested_id FROM json_each(?)) SELECT refs.position,refs.requested_id,entities.id,entities.kind,entities.engagement_id,entities.revision,entities.chat_session_id,entities.created_at,entities.updated_at,length(CAST(entities.payload AS BLOB)) AS payload_bytes,CASE WHEN length(CAST(entities.payload AS BLOB))<=16777216 THEN entities.payload ELSE NULL END AS payload FROM refs LEFT JOIN entities ON entities.id=refs.requested_id AND entities.kind='mcp_servers'").bind(parameters);
        let mut rows = statement.fetch(&mut *tx);
        while let Some(row) = rows.try_next().await? {
            let position: i64 = row.try_get("position")?;
            let id = row.try_get("requested_id")?;
            let record = if row.try_get::<Option<&str>, _>("id")?.is_none() {
                Ok(None)
            } else {
                budget.row(&row, 1)?;
                let decoded = decode_profile(&row);
                if let Ok(record) = &decoded {
                    // Defaulted capability entries can be larger than their
                    // saved JSON. Count the materialized result as well.
                    budget.add(serde_json::to_vec(record.payload())?.len())?;
                }
                decoded.map(Some)
            };
            profiles.push((position, McpProfileRow { id, record }));
        }
        drop(rows);
        tx.commit().await?;
        profiles.sort_unstable_by_key(|(position, _)| *position);
        Ok(profiles.into_iter().map(|(_, row)| row).collect())
    }

    pub async fn patch_session_settings(
        &self,
        id: &str,
        expected_revision: String,
        changes: Map<String, Value>,
        initial_raw: String,
        clock: Arc<StateClock>,
    ) -> Result<RawSession> {
        self.patch_settings(
            AssistantKind::Session,
            id,
            expected_revision,
            changes,
            Some(initial_raw),
            clock,
        )
        .await
    }
    pub async fn patch_schedule(
        &self,
        id: &str,
        expected_revision: String,
        changes: Map<String, Value>,
        clock: Arc<StateClock>,
    ) -> Result<StoredAssistantRecord> {
        Ok(self
            .patch_settings(
                AssistantKind::Schedule,
                id,
                expected_revision,
                changes,
                None,
                clock,
            )
            .await?
            .record)
    }
    /// Replace only goal configuration. Lifecycle, usage and execution claims
    /// remain owned by their separate state transitions.
    pub async fn patch_goal_config(
        &self,
        id: &str,
        expected_revision: String,
        changes: Map<String, Value>,
        clock: Arc<StateClock>,
    ) -> Result<StoredAssistantRecord> {
        Ok(self
            .patch_settings(
                AssistantKind::Goal,
                id,
                expected_revision,
                changes,
                None,
                clock,
            )
            .await?
            .record)
    }
    async fn patch_settings(
        &self,
        kind: AssistantKind,
        id: &str,
        expected_revision: String,
        changes: Map<String, Value>,
        initial_raw: Option<String>,
        clock: Arc<StateClock>,
    ) -> Result<RawSession> {
        validate_id(id)?;
        if expected_revision.is_empty()
            || expected_revision.len() > 4300
            || expected_revision.starts_with('0')
            || !expected_revision.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(Error::InvalidBounds);
        }
        let allowed = match kind {
            AssistantKind::Session => &["title", "metadata"][..],
            AssistantKind::Schedule => &["enabled", "paused_by", "skip_reason", "next_run_at"][..],
            AssistantKind::Goal => &[
                "objective",
                "completion_criteria",
                "plan",
                "token_budget",
                "time_budget_seconds",
                "step_budget",
                "child_budget",
            ][..],
            _ => return Err(Error::ProtectedField),
        };
        if changes.keys().any(|key| !allowed.contains(&key.as_str()))
            || kind == AssistantKind::Goal && changes.len() != allowed.len()
            || kind == AssistantKind::Session
                && !changes.get("metadata").is_some_and(Value::is_object)
        {
            return Err(Error::ProtectedField);
        }
        let size = id
            .len()
            .saturating_add(expected_revision.len())
            .saturating_add(serde_json::to_vec(&changes)?.len())
            .saturating_add(initial_raw.as_ref().map_or(0, String::len))
            .saturating_add(128);
        if size > MAX_TRANSACTION_BYTES {
            return Err(Error::TransactionLimit);
        }
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        let bytes = self
            .bytes
            .clone()
            .try_acquire_many_owned(size as u32)
            .map_err(|_| Error::Capacity)?;
        let slot = self.commands.try_reserve().map_err(|error| match error {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        slot.send(Command::Settings(SettingsRequest {
            kind,
            id: id.into(),
            expected_revision,
            changes,
            initial_raw,
            clock,
            _bytes: bytes,
            reply,
        }));
        result.await.map_err(|_| Error::Closed)?
    }
}

fn decode_profile(row: &SqliteRow) -> Result<StoredDependency> {
    let record = StoredDependency::decode(
        DependencyKind::McpServerProfile,
        row.try_get::<&str, _>("payload")?.as_bytes(),
    )?;
    let p = record.payload();
    if p["id"].as_str() != Some(row.try_get("id")?)
        || p["revision"].as_i64() != Some(row.try_get("revision")?)
        || row.try_get::<Option<&str>, _>("engagement_id")?.is_some()
        || row.try_get::<Option<&str>, _>("chat_session_id")?.is_some()
        || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
        || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
    {
        return Err(Error::CorruptEnvelope);
    }
    Ok(record)
}

struct Output(Vec<u8>);
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("settings record exceeds byte bound"));
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
}

fn write_value(output: &mut Output, value: &Value) -> Result<()> {
    serde_json::to_writer(output, value).map_err(|_| Error::ReadLimit)
}
#[derive(Clone, Copy)]
enum Preserve {
    Plain,
    Provider,
    Goal,
}
struct Keys(Vec<String>);
impl<'de> Deserialize<'de> for Keys {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct KeyVisitor;
        impl<'de> Visitor<'de> for KeyVisitor {
            type Value = Keys;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a saved dictionary")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Keys, A::Error> {
                let mut keys = Vec::new();
                let mut seen = HashSet::new();
                while let Some((key, _)) = map.next_entry::<String, IgnoredAny>()? {
                    if seen.insert(key.clone()) {
                        keys.push(key);
                    }
                }
                Ok(Keys(keys))
            }
        }
        deserializer.deserialize_map(KeyVisitor)
    }
}
/// Typed dictionary keys are stripped by the Goal model. Keep their first
/// normalized insertion position and last colliding value, as Python does;
/// values themselves are opaque Any data and retain their raw representation.
fn typed_dictionary(output: &mut Output, source: &str, next: &Value) -> Result<()> {
    let raw: HashMap<String, &RawValue> = serde_json::from_str(source)?;
    let Keys(keys) = serde_json::from_str(source)?;
    let mut positions = HashMap::new();
    let mut entries = Vec::new();
    for key in keys {
        let normalized = key.trim().to_owned();
        let value = *raw.get(&key).ok_or(Error::CorruptEnvelope)?;
        if let Some(&position) = positions.get(&normalized) {
            entries[position] = (normalized, value);
        } else {
            positions.insert(normalized.clone(), entries.len());
            entries.push((normalized, value));
        }
    }
    let next = next.as_object().ok_or(Error::CorruptEnvelope)?;
    if next.len() != entries.len() {
        return Err(Error::CorruptEnvelope);
    }
    output.bytes(b"{")?;
    for (index, (key, raw)) in entries.iter().enumerate() {
        if index != 0 {
            output.bytes(b",")?;
        }
        if next.get(key) != Some(&serde_json::from_str::<Value>(raw.get())?) {
            return Err(Error::CorruptEnvelope);
        }
        write_value(output, &Value::String(key.clone()))?;
        output.bytes(b":")?;
        output.bytes(raw.get().as_bytes())?;
    }
    output.bytes(b"}")
}
fn goal_dictionaries(output: &mut Output, source: &str, next: &Value) -> Result<()> {
    let raw: Vec<&RawValue> = serde_json::from_str(source)?;
    let next = next.as_array().ok_or(Error::CorruptEnvelope)?;
    if raw.len() != next.len() {
        return Err(Error::CorruptEnvelope);
    }
    output.bytes(b"[")?;
    for (index, (raw, next)) in raw.iter().zip(next).enumerate() {
        if index != 0 {
            output.bytes(b",")?;
        }
        typed_dictionary(output, raw.get(), next)?;
    }
    output.bytes(b"]")
}
fn preserved_object(
    output: &mut Output,
    source: &str,
    next: &Map<String, Value>,
    preserve: Preserve,
) -> Result<()> {
    let before: Value = serde_json::from_str(source)?;
    let raw: HashMap<String, &RawValue> = serde_json::from_str(source)?;
    output.bytes(b"{")?;
    for (index, (key, value)) in next.iter().enumerate() {
        if index != 0 {
            output.bytes(b",")?;
        }
        write_value(output, &Value::String(key.clone()))?;
        output.bytes(b":")?;
        if before.get(key) == Some(value) {
            output.bytes(raw.get(key).ok_or(Error::CorruptEnvelope)?.get().as_bytes())?;
        } else if matches!(preserve, Preserve::Goal) && key == "metadata" && before[key].is_object()
        {
            typed_dictionary(
                output,
                raw.get(key).ok_or(Error::CorruptEnvelope)?.get(),
                value,
            )?;
        } else if matches!(preserve, Preserve::Goal)
            && ["completion_evidence", "skill_snapshots"].contains(&key.as_str())
            && before[key].is_array()
        {
            goal_dictionaries(
                output,
                raw.get(key).ok_or(Error::CorruptEnvelope)?.get(),
                value,
            )?;
        } else if matches!(preserve, Preserve::Provider)
            && key == "provider_subagent"
            && before[key].is_object()
            && value.is_object()
        {
            preserved_object(
                output,
                raw.get(key).ok_or(Error::CorruptEnvelope)?.get(),
                value.as_object().ok_or(Error::CorruptEnvelope)?,
                Preserve::Plain,
            )?;
        } else {
            write_value(output, value)?;
        }
    }
    output.bytes(b"}")
}
fn session_json(current_raw: &str, initial_raw: &str, next: &Value, id: &str) -> Result<String> {
    let initial: Value = serde_json::from_str(initial_raw)?;
    if initial["id"].as_str().map(str::trim) != Some(id) {
        return Err(Error::CorruptEnvelope);
    }
    let initial_raw: HashMap<String, &RawValue> = serde_json::from_str(initial_raw)?;
    let current: Value = serde_json::from_str(current_raw)?;
    let raw: HashMap<String, &RawValue> = serde_json::from_str(current_raw)?;
    let mut output = Output(Vec::new());
    output.bytes(b"{")?;
    for (index, (key, value)) in next
        .as_object()
        .ok_or(Error::CorruptEnvelope)?
        .iter()
        .enumerate()
    {
        if index != 0 {
            output.bytes(b",")?;
        }
        write_value(&mut output, &Value::String(key.clone()))?;
        output.bytes(b":")?;
        if key == "metadata" {
            preserved_object(
                &mut output,
                initial_raw.get(key).map_or("{}", |raw| raw.get()),
                value.as_object().ok_or(Error::CorruptEnvelope)?,
                Preserve::Provider,
            )?;
        } else if current.get(key) == Some(value) {
            output.bytes(raw.get(key).ok_or(Error::CorruptEnvelope)?.get().as_bytes())?;
        } else {
            write_value(&mut output, value)?;
        }
    }
    output.bytes(b"}")?;
    String::from_utf8(output.0).map_err(|_| Error::CorruptEnvelope)
}

pub(super) async fn write(
    connection: &mut SqliteConnection,
    request: &SettingsRequest,
) -> Result<RawSession> {
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    let row = sqlx::query(&format!("{SELECT_RECORD} WHERE kind=? AND id=?"))
        .bind(request.kind.as_str())
        .bind(&request.id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or(Error::NotFound)?;
    let revision: i64 = row.try_get("revision")?;
    if request.expected_revision != revision.to_string() {
        return Err(Error::SettingsRevisionConflict {
            expected: request.expected_revision.clone(),
            found: revision,
        });
    }
    let mut budget = Budget::default();
    budget.add(request.initial_raw.as_ref().map_or(0, String::len))?;
    budget.row(&row, 2)?;
    let current = decode_row_ref(&row)?;
    let mut payload = current.into_payload();
    payload
        .as_object_mut()
        .ok_or(Error::CorruptEnvelope)?
        .extend(request.changes.clone());
    payload["revision"] = revision
        .checked_add(1)
        .ok_or(Error::RevisionExhausted)?
        .into();
    payload["updated_at"] = (request.clock)()
        .to_rfc3339_opts(SecondsFormat::Micros, true)
        .into();
    let encoded = if request.kind == AssistantKind::Goal {
        // model_dump preserves insertion order inside unchanged opaque values.
        // Keep those fragments both for merged-model diagnostics and the commit.
        let mut output = Output(Vec::new());
        preserved_object(
            &mut output,
            row.try_get("payload")?,
            payload.as_object().ok_or(Error::CorruptEnvelope)?,
            Preserve::Goal,
        )?;
        output.0
    } else {
        serde_json::to_vec(&payload)?
    };
    let record = if matches!(request.kind, AssistantKind::Schedule | AssistantKind::Goal) {
        match StoredAssistantRecord::decode_updated_direct(request.kind, &encoded)
            .map_err(direct_record_error)
        {
            Ok(record) => record,
            Err(Error::RetainedModelValidation(report)) => {
                budget.add(report.retained_bytes())?;
                return Err(Error::RetainedModelValidation(report));
            }
            Err(error) => return Err(error),
        }
    } else {
        // Session field diagnostics are a separate compatibility increment.
        StoredAssistantRecord::decode_persisted(request.kind, &encoded)?
    };
    let raw_payload = if request.kind == AssistantKind::Session {
        session_json(
            row.try_get("payload")?,
            request
                .initial_raw
                .as_deref()
                .ok_or(Error::CorruptEnvelope)?,
            record.payload(),
            &request.id,
        )?
    } else if request.kind == AssistantKind::Goal {
        let mut output = Output(Vec::new());
        preserved_object(
            &mut output,
            row.try_get("payload")?,
            record.payload().as_object().ok_or(Error::CorruptEnvelope)?,
            Preserve::Goal,
        )?;
        String::from_utf8(output.0).map_err(|_| Error::CorruptEnvelope)?
    } else {
        serde_json::to_string(record.payload())?
    };
    budget.add(raw_payload.len().saturating_mul(2))?;
    let p = record.payload();
    let changed=sqlx::query("UPDATE entities SET payload=?,engagement_id=?,chat_session_id=?,revision=?,updated_at=? WHERE kind=? AND id=? AND revision=?")
        .bind(&raw_payload).bind(p["engagement_id"].as_str()).bind(session_projection(&record)).bind(record_revision(&record)?).bind(sql_time(&p["updated_at"])?).bind(request.kind.as_str()).bind(&request.id).bind(revision).execute(&mut *tx).await?;
    if changed.rows_affected() != 1 {
        return Err(Error::Conflict);
    }
    update_search(&mut tx, &record).await?;
    tx.commit().await?;
    Ok(RawSession {
        record,
        raw_payload,
    })
}
