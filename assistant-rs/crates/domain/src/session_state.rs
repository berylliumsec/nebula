//! Pure retained session display state and Python-compatible display digests.
//! Input acquisition and revision assignment belong to the storage transaction;
//! this module cannot run a provider, observe a process, or mutate a record.
use crate::{
    dependencies::{DependencyKind, StoredDependency},
    records::{AssistantKind, StoredAssistantRecord},
};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Timelike, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};

const MAX_BYTES: usize = 16 * 1024 * 1024;
const MAX_ROWS: usize = 10_000;
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum StateError {
    #[error("Retained session state cannot be projected")]
    Invalid,
    #[error("Retained session state exceeds its bounded read limit")]
    TooLarge,
}
type Result<T> = std::result::Result<T, StateError>;
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum ConnectionState {
    #[default]
    Unknown,
    Connected,
    Disconnected,
}
impl ConnectionState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Unknown => "unknown",
            Self::Connected => "connected",
            Self::Disconnected => "disconnected",
        }
    }
}
#[derive(Clone, Debug)]
pub struct StateInputs {
    pub session: StoredAssistantRecord,
    /// Complete source collection, newest envelope created_at/id first.
    pub turns: Vec<StoredAssistantRecord>,
    pub approvals: Vec<StoredDependency>,
    pub questions: Vec<StoredDependency>,
    pub harnesses: Vec<StoredDependency>,
    pub profile: Option<StoredDependency>,
    /// First qualifying immutable operation-event sequence per approval.
    pub progress: HashMap<String, Option<i64>>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProgressRequest {
    pub approval_id: String,
    pub harness_turn_id: String,
    pub after_sequence: i64,
}
fn text<'a>(p: &'a Value, field: &str) -> Result<&'a str> {
    p[field].as_str().ok_or(StateError::Invalid)
}
fn terminal(status: &str) -> bool {
    matches!(status, "complete" | "failed" | "cancelled" | "interrupted")
}
fn timestamp(value: &Value, z: bool) -> Result<String> {
    let raw = value.as_str().ok_or(StateError::Invalid)?;
    if let Ok(time) = DateTime::parse_from_rfc3339(raw) {
        Ok(time.to_rfc3339_opts(
            if time.timestamp_subsec_micros() == 0 {
                SecondsFormat::Secs
            } else {
                SecondsFormat::Micros
            },
            z,
        ))
    } else {
        let time = NaiveDateTime::parse_from_str(raw, "%Y-%m-%dT%H:%M:%S%.f")
            .map_err(|_| StateError::Invalid)?;
        Ok(time
            .format(if time.and_utc().timestamp_subsec_micros() == 0 {
                "%Y-%m-%dT%H:%M:%S"
            } else {
                "%Y-%m-%dT%H:%M:%S%.6f"
            })
            .to_string())
    }
}
struct Selection<'a> {
    turn: Option<&'a Value>,
    harness: Option<&'a Value>,
    active: HashSet<&'a str>,
    active_harnesses: HashSet<Option<&'a str>>,
    approval_owners: HashMap<&'a str, &'a str>,
    harness_owners: HashMap<Option<&'a str>, &'a str>,
}
fn select(inputs: &StateInputs) -> Result<Selection<'_>> {
    let count = 1usize
        .saturating_add(inputs.turns.len())
        .saturating_add(inputs.approvals.len())
        .saturating_add(inputs.questions.len())
        .saturating_add(inputs.harnesses.len())
        .saturating_add(usize::from(inputs.profile.is_some()));
    if count > MAX_ROWS {
        return Err(StateError::TooLarge);
    }
    if inputs.session.kind() != AssistantKind::Session
        || inputs.turns.iter().any(|r| r.kind() != AssistantKind::Turn)
        || inputs
            .approvals
            .iter()
            .any(|r| r.kind() != DependencyKind::Approval)
        || inputs
            .questions
            .iter()
            .any(|r| r.kind() != DependencyKind::HarnessInteraction)
        || inputs
            .harnesses
            .iter()
            .any(|r| r.kind() != DependencyKind::HarnessTurn)
        || inputs
            .profile
            .as_ref()
            .is_some_and(|r| r.kind() != DependencyKind::HarnessProfile)
    {
        return Err(StateError::Invalid);
    }
    let harnesses: HashMap<_, _> = inputs
        .harnesses
        .iter()
        .map(|r| Ok((text(r.payload(), "id")?, r.payload())))
        .collect::<Result<_>>()?;
    let mut selected = Selection {
        turn: None,
        harness: None,
        active: HashSet::new(),
        active_harnesses: HashSet::new(),
        approval_owners: HashMap::new(),
        harness_owners: HashMap::new(),
    };
    for turn in &inputs.turns {
        let p = turn.payload();
        let id = text(p, "id")?;
        let harness_id = p["harness_turn_id"].as_str();
        if let Some(approval_id) = p["approval_id"].as_str() {
            selected.approval_owners.entry(approval_id).or_insert(id);
        }
        selected.harness_owners.entry(harness_id).or_insert(id);
        let harness_terminal = harness_id
            .and_then(|id| harnesses.get(id))
            .map(|p| text(p, "status").map(terminal))
            .transpose()?
            .unwrap_or(false);
        if !terminal(text(p, "status")?) && !harness_terminal {
            selected.active.insert(id);
            selected.active_harnesses.insert(harness_id);
            // Descending source order: the last active record is the oldest.
            selected.turn = Some(p);
        }
    }
    selected.turn = selected
        .turn
        .or_else(|| inputs.turns.first().map(StoredAssistantRecord::payload));
    selected.harness = selected
        .turn
        .and_then(|p| p["harness_turn_id"].as_str())
        .filter(|id| !id.is_empty())
        .and_then(|id| harnesses.get(id).copied());
    Ok(selected)
}
fn approval_owner<'a>(p: &'a Value, selected: &Selection<'a>) -> Option<&'a str> {
    p["chat_turn_id"]
        .as_str()
        .filter(|id| !id.is_empty())
        .or_else(|| {
            p["id"]
                .as_str()
                .and_then(|id| selected.approval_owners.get(id).copied())
        })
}
fn approval_pending(p: &Value, selected: &Selection<'_>, now: DateTime<Utc>) -> Result<bool> {
    if !approval_owner(p, selected).is_some_and(|id| selected.active.contains(id))
        || p["status"] != "pending"
    {
        return Ok(false);
    }
    if p["expires_at"].is_null() {
        return Ok(true);
    }
    let expiry = DateTime::parse_from_rfc3339(p["expires_at"].as_str().ok_or(StateError::Invalid)?)
        .map_err(|_| StateError::Invalid)?;
    // Pydantic hydrates datetime at Python's microsecond resolution, including
    // legacy payloads that retained a finer textual fraction.
    let expiry = expiry
        .with_nanosecond(expiry.nanosecond() / 1000 * 1000)
        .ok_or(StateError::Invalid)?;
    Ok(expiry > now)
}
fn decision(p: &Value, selected: &Selection<'_>) -> bool {
    p["status"] != "pending"
        && selected
            .turn
            .is_some_and(|turn| p["chat_turn_id"] == turn["id"] || p["id"] == turn["approval_id"])
}
fn eligible(continuation: &Value) -> bool {
    !continuation.is_null()
        && !continuation["progress_after_sequence"].is_null()
        && continuation["status"] == "delivered"
        && matches!(
            continuation["adapter_status"].as_str(),
            None | Some("not_required" | "sent")
        )
}
/// The storage layer calls this before its ledger/profile reads. Pending expiry
/// errors occur first, as in Python. Non-querying continuations never bind their
/// potentially arbitrary-precision sequence to SQLite's signed64-bit integer.
pub fn progress_requests(inputs: &StateInputs, now: DateTime<Utc>) -> Result<Vec<ProgressRequest>> {
    let selected = select(inputs)?;
    let now = now
        .with_nanosecond(now.nanosecond() / 1000 * 1000)
        .ok_or(StateError::Invalid)?;
    for approval in &inputs.approvals {
        approval_pending(approval.payload(), &selected, now)?;
    }
    let mut requests = Vec::new();
    for approval in &inputs.approvals {
        let p = approval.payload();
        let c = &p["continuation"];
        if decision(p, &selected) && eligible(c) {
            requests.push(ProgressRequest {
                approval_id: text(p, "id")?.into(),
                harness_turn_id: text(c, "harness_turn_id")?.into(),
                after_sequence: c["progress_after_sequence"]
                    .as_i64()
                    .ok_or(StateError::Invalid)?,
            });
        }
    }
    requests.sort_by(|a, b| a.approval_id.cmp(&b.approval_id));
    Ok(requests)
}
struct ByteBudget(usize);
impl std::io::Write for ByteBudget {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(b.len());
        if self.0 > MAX_BYTES {
            Err(std::io::Error::other(
                "Session state exceeds its byte bound",
            ))
        } else {
            Ok(b.len())
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl ByteBudget {
    fn charge(&mut self, value: &Value) -> Result<()> {
        serde_json::to_writer(self, value).map_err(|_| StateError::TooLarge)
    }
    fn push(&mut self, items: &mut Vec<Value>, value: Value) -> Result<()> {
        if items.len() >= MAX_ROWS {
            return Err(StateError::TooLarge);
        }
        self.0 += usize::from(!items.is_empty());
        self.charge(&value)?;
        items.push(value);
        Ok(())
    }
}
pub fn project(
    inputs: &StateInputs,
    now: DateTime<Utc>,
    connection: ConnectionState,
) -> Result<Value> {
    let selected = select(inputs)?;
    let now = now
        .with_nanosecond(now.nanosecond() / 1000 * 1000)
        .ok_or(StateError::Invalid)?;
    let mut budget = ByteBudget(4);
    let mut pending = Vec::new();
    for approval in &inputs.approvals {
        let p = approval.payload();
        if approval_pending(p, &selected, now)? {
            budget.push(&mut pending,json!({"id":p["id"],"turn_id":approval_owner(p,&selected),"kind":"approval","text":"Review the requested action","at":timestamp(&p["updated_at"],false)?}))?;
        }
    }
    for question in &inputs.questions {
        let p = question.payload();
        let harness_id = p["harness_turn_id"].as_str();
        if p["status"] == "pending" && selected.active_harnesses.contains(&harness_id) {
            budget.push(&mut pending,json!({"id":p["id"],"turn_id":selected.harness_owners.get(&harness_id),"kind":"input","text":if p["contains_secret"]==true {Value::from("A secret answer is required")} else {p["prompt"].clone()},"at":timestamp(&p["updated_at"],false)?}))?;
        }
    }
    pending.sort_by(|a, b| {
        (a["at"].as_str(), a["id"].as_str()).cmp(&(b["at"].as_str(), b["id"].as_str()))
    });
    let mut approvals: Vec<_> = inputs
        .approvals
        .iter()
        .filter(|r| decision(r.payload(), &selected))
        .collect();
    approvals.sort_by(|a, b| a.payload()["id"].as_str().cmp(&b.payload()["id"].as_str()));
    let mut decisions = Vec::new();
    for approval in approvals {
        let p = approval.payload();
        let mut continuation = p["continuation"].clone();
        if !continuation.is_null() {
            continuation["updated_at"] = timestamp(&continuation["updated_at"], true)?.into();
        }
        let sequence = if eligible(&continuation) {
            continuation["progress_after_sequence"]
                .as_i64()
                .ok_or(StateError::Invalid)?;
            inputs.progress.get(text(p, "id")?).copied().flatten()
        } else {
            None
        };
        budget.push(&mut decisions,json!({"approval_id":p["id"],"status":p["status"],"continuation":continuation,"progress":if sequence.is_some(){"observed"}else{"not_observed"},"progress_sequence":sequence}))?;
    }
    let mut execution = selected
        .turn
        .map(|p| text(p, "status"))
        .transpose()?
        .unwrap_or("idle");
    if let Some(harness) = selected.harness
        && !terminal(execution)
    {
        execution = text(harness, "status")?;
    }
    if execution == "routing" {
        execution = "running";
    }
    if !terminal(execution) && !pending.is_empty() {
        execution = "waiting_approval";
    } else if execution == "waiting_approval" {
        execution = if decisions.is_empty() {
            "status_unavailable"
        } else {
            "continuing"
        };
    } else if execution == "running" && decisions.iter().any(|d| d["progress"] == "not_observed") {
        execution = "continuing";
    }
    if !terminal(execution)
        && decisions.iter().any(|d| {
            !d["continuation"].is_null()
                && (d["continuation"]["status"] == "failed"
                    || matches!(
                        d["continuation"]["adapter_status"].as_str(),
                        Some("failed" | "unknown")
                    ))
        })
    {
        execution = "interrupted";
    }
    let detail = match execution {
        "idle" => "Ready for your next message.",
        "running" => "Working.",
        "queued" => "Waiting to start.",
        "waiting_approval" => "Review the pending action to continue.",
        "continuing" => "Decision recorded; waiting for execution progress.",
        "status_unavailable" => {
            "The response is paused, but no actionable request is available. Check status or stop waiting."
        }
        "complete" => "Response complete.",
        "cancelled" => "Response stopped.",
        "interrupted" => "Response interrupted. No work was replayed; start a new response.",
        "failed" => "Response failed. Review the saved error before retrying.",
        _ => "Checking response status.",
    };
    let busy = !terminal(execution) && execution != "idle";
    let session = inputs.session.payload();
    let can_stop = busy
        && (session["harness_profile_id"]
            .as_str()
            .is_none_or(|s| s.is_empty())
            || inputs
                .profile
                .as_ref()
                .is_some_and(|p| p.payload()["capabilities"]["interruption"] == true));
    let mut actions = vec!["check_status"];
    if !pending.is_empty() {
        actions.push("review");
    }
    if can_stop {
        actions.push("stop");
    }
    let connection = if session["harness_session_id"]
        .as_str()
        .is_some_and(|s| !s.is_empty())
    {
        connection
    } else {
        ConnectionState::Unknown
    };
    let value = json!({"schema":"nebula.session-state/v1","session_id":session["id"],"turn_id":selected.turn.map(|p|&p["id"]),"harness_turn_id":selected.harness.map(|p|&p["id"]),"execution":execution,"pending":pending,"decisions":decisions,"busy":busy,"detail":detail,"connection":connection.as_str(),"connection_scope":"harness_transport","actions":actions});
    ByteBudget(0).charge(&value)?;
    Ok(value)
}

struct AsciiHash {
    hash: Sha256,
    bytes: usize,
}
impl AsciiHash {
    fn write(&mut self, b: &[u8]) -> Result<()> {
        self.bytes = self.bytes.saturating_add(b.len());
        if self.bytes > MAX_BYTES {
            return Err(StateError::TooLarge);
        }
        self.hash.update(b);
        Ok(())
    }
    fn string(&mut self, text: &str) -> Result<()> {
        self.write(b"\"")?;
        let mut start = 0;
        for (at, c) in text.char_indices() {
            if ('\u{20}'..='\u{7e}').contains(&c) && !matches!(c, '"' | '\\') {
                continue;
            }
            self.write(&text.as_bytes()[start..at])?;
            match c {
                '"' => self.write(b"\\\"")?,
                '\\' => self.write(b"\\\\")?,
                '\u{8}' => self.write(b"\\b")?,
                '\u{c}' => self.write(b"\\f")?,
                '\n' => self.write(b"\\n")?,
                '\r' => self.write(b"\\r")?,
                '\t' => self.write(b"\\t")?,
                c => {
                    const HEX: &[u8; 16] = b"0123456789abcdef";
                    for unit in c.encode_utf16(&mut [0; 2]) {
                        let unit = *unit as usize;
                        self.write(&[
                            b'\\',
                            b'u',
                            HEX[unit >> 12],
                            HEX[(unit >> 8) & 15],
                            HEX[(unit >> 4) & 15],
                            HEX[unit & 15],
                        ])?;
                    }
                }
            }
            start = at + c.len_utf8();
        }
        self.write(&text.as_bytes()[start..])?;
        self.write(b"\"")
    }
    fn value(&mut self, value: &Value, depth: usize) -> Result<()> {
        if depth > 128 {
            return Err(StateError::Invalid);
        }
        match value {
            Value::Null => self.write(b"null")?,
            Value::Bool(v) => self.write(if *v { b"true" } else { b"false" })?,
            Value::Number(v) => {
                let text = v.to_string();
                if text.contains(['.', 'e', 'E']) {
                    let number: f64 = text.parse().map_err(|_| StateError::Invalid)?;
                    if !number.is_finite() {
                        return Err(StateError::Invalid);
                    }
                    let shortest = format!("{number:?}");
                    if let Some((mantissa, exponent)) = shortest.split_once('e') {
                        let exponent: i32 = exponent.parse().map_err(|_| StateError::Invalid)?;
                        self.write(
                            format!(
                                "{mantissa}e{}{:02}",
                                if exponent < 0 { '-' } else { '+' },
                                exponent.unsigned_abs()
                            )
                            .as_bytes(),
                        )?;
                    } else {
                        self.write(shortest.as_bytes())?;
                    }
                } else {
                    self.write(if text == "-0" { b"0" } else { text.as_bytes() })?;
                }
            }
            Value::String(text) => self.string(text)?,
            Value::Array(values) => {
                self.write(b"[")?;
                for (i, v) in values.iter().enumerate() {
                    if i != 0 {
                        self.write(b",")?;
                    }
                    self.value(v, depth + 1)?;
                }
                self.write(b"]")?;
            }
            Value::Object(values) => {
                self.write(b"{")?;
                // Explicit sorting keeps the digest stable even if a future
                // caller enables serde_json's insertion-order map feature.
                let mut keys: Vec<_> = values.keys().collect();
                keys.sort_unstable();
                for (i, key) in keys.into_iter().enumerate() {
                    if i != 0 {
                        self.write(b",")?;
                    }
                    self.string(key)?;
                    self.write(b":")?;
                    self.value(&values[key], depth + 1)?;
                }
                self.write(b"}")?;
            }
        }
        Ok(())
    }
}
/// Match hashlib.sha256(json.dumps(projected, sort_keys=True,
/// separators=(',', ':')).encode()). The watermark revision is supplied by the
/// storage layer after this digest; callers pass the pure projected value.
pub fn digest(value: &Value) -> Result<String> {
    let mut encoder = AsciiHash {
        hash: Sha256::new(),
        bytes: 0,
    };
    encoder.value(value, 0)?;
    Ok(format!("{:x}", encoder.hash.finalize()))
}
