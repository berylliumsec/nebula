//! Durable project-scoped provider text operations. All I/O here is bounded
//! SQLite work; no provider, helper, workspace or inference retry is authorized.
use super::*;
use nebula_assistant_domain::{
    model_validation::{InputOrigin, Model, hydrate},
    retained_json::repair_turn_json,
};
use serde::{Deserialize, Serialize, Serializer, ser::SerializeMap};
use serde_json::{Number, value::RawValue};
use std::{
    collections::{HashMap, HashSet},
    io::Write,
};

const MAX_INPUT_MESSAGES: usize = 200;

/// Validated values and raw spelling are immutable shared allocations. Records
/// returned by a store retain a shared aggregate byte lease until their last
/// clone is dropped. This charges encoded canonical values plus raw JSON; it is
/// not an allocator/RSS bound or a budget for caller-created payload copies.
#[derive(Clone)]
pub struct ExecutionRecord {
    data: Arc<ExecutionData>,
    lease: Option<Arc<ResultLease>>,
}
struct ExecutionData {
    record: StoredAssistantRecord,
    raw_payload: String,
    retained_bytes: usize,
}
struct ResultLease {
    budget: Arc<Semaphore>,
    _permit: OwnedSemaphorePermit,
}
impl std::fmt::Debug for ExecutionRecord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ExecutionRecord")
            .field("kind", &self.record().kind())
            .finish_non_exhaustive()
    }
}
impl ExecutionRecord {
    pub fn new(record: StoredAssistantRecord, original: &str) -> Result<Self> {
        let (record, raw_payload) = match record.kind() {
            AssistantKind::Session | AssistantKind::Message => {
                let stored = ForkRecord::new(record, original)?;
                (stored.record, stored.raw_payload)
            }
            AssistantKind::Turn => {
                let raw = repair_turn_json(original, record.payload()).map_err(direct_error)?;
                (record, raw)
            }
            _ => return Err(Error::InvalidBounds),
        };
        let retained_bytes = encoded_size(record.payload())?.saturating_add(raw_payload.len());
        Ok(Self {
            data: Arc::new(ExecutionData {
                record,
                raw_payload,
                retained_bytes,
            }),
            lease: None,
        })
    }
    pub fn from_record(record: StoredAssistantRecord) -> Result<Self> {
        let raw = bounded_json(record.payload())?;
        Self::new(record, &raw)
    }
    /// Borrow the validated record. Any independent payload copies made by a
    /// caller require that caller's own context/memory admission.
    pub fn record(&self) -> &StoredAssistantRecord {
        &self.data.record
    }
    pub fn payload(&self) -> &Value {
        self.data.record.payload()
    }
    pub fn raw_payload(&self) -> &str {
        &self.data.raw_payload
    }
    fn id(&self) -> Result<&str> {
        text(self.payload(), "id")
    }
    fn revision(&self) -> Result<i64> {
        record_revision(self.record())
    }
}
/// Acquire the whole returned group without waiting. Shared clones retain the
/// group lease, conservatively, until its last record is dropped. Existing
/// records charged to this same store reuse their lease; foreign-store records
/// receive this store's own accounting before they can be returned.
fn lease_records<'a>(
    records: impl IntoIterator<Item = &'a mut ExecutionRecord>,
    budget: &Arc<Semaphore>,
) -> Result<()> {
    let mut records: Vec<_> = records.into_iter().collect();
    let mut charged = HashMap::new();
    for record in &records {
        if let Some(lease) = &record.lease
            && Arc::ptr_eq(&lease.budget, budget)
        {
            charged.insert(Arc::as_ptr(&record.data) as usize, lease.clone());
        }
    }
    let mut pending = HashSet::new();
    let mut bytes = 0usize;
    for record in &mut records {
        let key = Arc::as_ptr(&record.data) as usize;
        if let Some(lease) = charged.get(&key) {
            record.lease = Some(lease.clone());
        } else if pending.insert(key) {
            bytes = bytes
                .checked_add(record.data.retained_bytes)
                .ok_or(Error::ExecutionResultCapacity)?;
        }
    }
    if bytes == 0 {
        return Ok(());
    }
    let permit = budget
        .clone()
        .try_acquire_many_owned(u32::try_from(bytes).map_err(|_| Error::ExecutionResultCapacity)?)
        .map_err(|_| Error::ExecutionResultCapacity)?;
    let lease = Arc::new(ResultLease {
        budget: budget.clone(),
        _permit: permit,
    });
    for record in records {
        if pending.contains(&(Arc::as_ptr(&record.data) as usize)) {
            record.lease = Some(lease.clone());
        }
    }
    Ok(())
}
#[derive(Clone, Debug)]
pub enum AdmissionSession {
    New(ExecutionRecord),
    Existing { id: String, expected_revision: i64 },
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct TextSettings {
    pub reasoning_effort: Option<String>,
    pub max_active_subagents: Option<Number>,
}
#[derive(Clone, Debug)]
pub struct TextAdmission {
    pub session: AdmissionSession,
    pub turn: ExecutionRecord,
    pub messages: Vec<ExecutionRecord>,
    pub settings: TextSettings,
    pub provider_profile_id: String,
    pub resolved_model: String,
}
#[derive(Debug)]
pub struct AdmissionCommit {
    pub session: ExecutionRecord,
    pub turn: ExecutionRecord,
    pub messages: Vec<ExecutionRecord>,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExecutionFence {
    pub turn_id: String,
    pub worker_id: String,
    pub claim_id: String,
}
#[derive(Clone, Debug)]
pub struct Claim {
    pub worker_id: String,
    pub claim_id: String,
    pub claimed_at: DateTime<Utc>,
}
#[derive(Debug)]
pub struct AnswerCommit {
    pub session: ExecutionRecord,
    pub turn: ExecutionRecord,
    pub message: ExecutionRecord,
}
#[derive(Debug)]
pub enum ReleaseOutcome {
    Released(ExecutionRecord),
    NoLongerOwner(ExecutionRecord),
}
#[derive(Clone, Copy, Debug)]
pub enum InterruptCause {
    Restart,
    Shutdown,
}
#[derive(Clone, Debug)]
pub enum TerminalAction {
    Stop,
    Fail {
        fence: ExecutionFence,
        error: String,
    },
    /// A missing fence is startup classification, which still requires CAS.
    Interrupt {
        fence: Option<ExecutionFence>,
        cause: InterruptCause,
        observed_at: DateTime<Utc>,
    },
}
#[derive(Debug)]
pub enum OutcomeCommit {
    AlreadyRecorded,
    Written {
        session: ExecutionRecord,
        message: ExecutionRecord,
    },
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RecoveryState {
    Complete {
        message_id: String,
        claim_retained: bool,
    },
    SavedAnswer {
        message_id: String,
    },
    Interrupted,
    /// No current claim; absence alone does not prove no prior dispatch.
    Unclaimed,
    /// A claim was durable but provider completion has no durable evidence.
    ClaimedUncertain,
    Terminal,
    /// Preserve evidence and let the caller present uncertainty. Never dispatch
    /// from this result or select an arbitrary candidate.
    Uncertain(&'static str),
}
#[derive(Debug)]
pub struct TextRecovery {
    pub turn: ExecutionRecord,
    pub session: ExecutionRecord,
    pub state: RecoveryState,
    pub answer: Option<ExecutionRecord>,
}
#[derive(Debug)]
pub struct TextSnapshot {
    pub session: ExecutionRecord,
    pub messages: Vec<ExecutionRecord>,
    pub unfinished_turns: Vec<ExecutionRecord>,
}

pub(super) struct Request {
    pub operation: Operation,
    pub clock: Arc<StateClock>,
    pub results: Arc<Semaphore>,
    pub _bytes: OwnedSemaphorePermit,
    pub reply: oneshot::Sender<Result<Reply>>,
}
pub(super) enum Operation {
    Admit(TextAdmission),
    Claim {
        turn_id: String,
        expected_revision: i64,
        claim: Claim,
    },
    Answer {
        fence: ExecutionFence,
        turn_revision: i64,
        session_revision: i64,
        message: ExecutionRecord,
    },
    Complete {
        fence: ExecutionFence,
        expected_revision: i64,
        message_id: String,
        usage: Value,
    },
    Release {
        fence: ExecutionFence,
        expected_revision: i64,
    },
    Terminal {
        turn_id: String,
        expected_revision: i64,
        action: TerminalAction,
    },
    Outcome {
        turn_id: String,
        turn_revision: i64,
        session_revision: i64,
        message: ExecutionRecord,
    },
}
pub(super) enum Reply {
    Admission(AdmissionCommit),
    Record(ExecutionRecord),
    Answer(AnswerCommit),
    Release(ReleaseOutcome),
    Outcome(OutcomeCommit),
}

impl SqliteAssistantStore {
    pub async fn admit_text(
        &self,
        input: TextAdmission,
        clock: Arc<StateClock>,
    ) -> Result<AdmissionCommit> {
        match self
            .execution_command(Operation::Admit(input), clock)
            .await?
        {
            Reply::Admission(v) => Ok(v),
            _ => Err(Error::CorruptEnvelope),
        }
    }
    pub async fn claim_text(
        &self,
        turn_id: &str,
        expected_revision: i64,
        claim: Claim,
        clock: Arc<StateClock>,
    ) -> Result<ExecutionRecord> {
        match self
            .execution_command(
                Operation::Claim {
                    turn_id: turn_id.into(),
                    expected_revision,
                    claim,
                },
                clock,
            )
            .await?
        {
            Reply::Record(v) => Ok(v),
            _ => Err(Error::CorruptEnvelope),
        }
    }
    pub async fn append_text_answer(
        &self,
        fence: ExecutionFence,
        expected_turn_revision: i64,
        expected_session_revision: i64,
        answer: ExecutionRecord,
        clock: Arc<StateClock>,
    ) -> Result<AnswerCommit> {
        match self
            .execution_command(
                Operation::Answer {
                    fence,
                    turn_revision: expected_turn_revision,
                    session_revision: expected_session_revision,
                    message: answer,
                },
                clock,
            )
            .await?
        {
            Reply::Answer(v) => Ok(v),
            _ => Err(Error::CorruptEnvelope),
        }
    }
    pub async fn complete_text(
        &self,
        fence: ExecutionFence,
        expected_revision: i64,
        message_id: &str,
        usage: Value,
        clock: Arc<StateClock>,
    ) -> Result<ExecutionRecord> {
        match self
            .execution_command(
                Operation::Complete {
                    fence,
                    expected_revision,
                    message_id: message_id.into(),
                    usage,
                },
                clock,
            )
            .await?
        {
            Reply::Record(v) => Ok(v),
            _ => Err(Error::CorruptEnvelope),
        }
    }
    pub async fn release_text(
        &self,
        fence: ExecutionFence,
        expected_revision: i64,
        clock: Arc<StateClock>,
    ) -> Result<ReleaseOutcome> {
        match self
            .execution_command(
                Operation::Release {
                    fence,
                    expected_revision,
                },
                clock,
            )
            .await?
        {
            Reply::Release(v) => Ok(v),
            _ => Err(Error::CorruptEnvelope),
        }
    }
    pub async fn settle_text(
        &self,
        turn_id: &str,
        expected_revision: i64,
        action: TerminalAction,
        clock: Arc<StateClock>,
    ) -> Result<ExecutionRecord> {
        match self
            .execution_command(
                Operation::Terminal {
                    turn_id: turn_id.into(),
                    expected_revision,
                    action,
                },
                clock,
            )
            .await?
        {
            Reply::Record(v) => Ok(v),
            _ => Err(Error::CorruptEnvelope),
        }
    }
    pub async fn append_text_outcome(
        &self,
        turn_id: &str,
        expected_turn_revision: i64,
        expected_session_revision: i64,
        note: ExecutionRecord,
        clock: Arc<StateClock>,
    ) -> Result<OutcomeCommit> {
        match self
            .execution_command(
                Operation::Outcome {
                    turn_id: turn_id.into(),
                    turn_revision: expected_turn_revision,
                    session_revision: expected_session_revision,
                    message: note,
                },
                clock,
            )
            .await?
        {
            Reply::Outcome(v) => Ok(v),
            _ => Err(Error::CorruptEnvelope),
        }
    }
    async fn execution_command(
        &self,
        operation: Operation,
        clock: Arc<StateClock>,
    ) -> Result<Reply> {
        if self.closing.load(Ordering::Acquire) {
            return Err(Error::Closed);
        }
        let bytes = operation_size(&operation)?;
        let byte_permit = self
            .bytes
            .clone()
            .try_acquire_many_owned(bytes as u32)
            .map_err(|_| Error::Capacity)?;
        let slot = self.commands.try_reserve().map_err(|e| match e {
            mpsc::error::TrySendError::Full(_) => Error::Capacity,
            mpsc::error::TrySendError::Closed(_) => Error::Closed,
        })?;
        let (reply, result) = oneshot::channel();
        slot.send(Command::Execution(Request {
            operation,
            clock,
            results: self.execution_results.clone(),
            _bytes: byte_permit,
            reply,
        }));
        result.await.map_err(|_| Error::Closed)?
    }
    pub async fn execution_record(&self, kind: AssistantKind, id: &str) -> Result<ExecutionRecord> {
        supported_kind(kind)?;
        validate_id(id)?;
        let _permit = self.read_permit()?;
        let mut connection = self.readers.acquire().await?;
        {
            let mut record = read(&mut connection, kind, id, &mut Budget::default(), true).await?;
            lease_records([&mut record], &self.execution_results)?;
            Ok(record)
        }
    }
    pub async fn text_snapshot(&self, session_id: &str) -> Result<TextSnapshot> {
        validate_id(session_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let session = read(
            &mut tx,
            AssistantKind::Session,
            session_id,
            &mut budget,
            true,
        )
        .await?;
        let unfinished_turns =
            collection(&mut tx, AssistantKind::Turn, session_id, true, &mut budget).await?;
        let messages = collection(
            &mut tx,
            AssistantKind::Message,
            session_id,
            false,
            &mut budget,
        )
        .await?;
        let mut messages = messages;
        messages.sort_by(|a, b| {
            compare_decimal(
                &decimal(&a.payload()["sequence"]).expect("validated positive sequence"),
                &decimal(&b.payload()["sequence"]).expect("validated positive sequence"),
            )
        });
        tx.commit().await?;
        let mut result = TextSnapshot {
            session,
            messages,
            unfinished_turns,
        };
        lease_records(
            std::iter::once(&mut result.session)
                .chain(result.messages.iter_mut())
                .chain(result.unfinished_turns.iter_mut()),
            &self.execution_results,
        )?;
        Ok(result)
    }
    pub async fn text_recovery(&self, turn_id: &str) -> Result<TextRecovery> {
        validate_id(turn_id)?;
        let _permit = self.read_permit()?;
        let mut tx = self.readers.begin().await?;
        let mut budget = Budget::default();
        let turn = read(&mut tx, AssistantKind::Turn, turn_id, &mut budget, true).await?;
        text_turn(&turn)?;
        let session = read(
            &mut tx,
            AssistantKind::Session,
            text(turn.payload(), "session_id")?,
            &mut budget,
            true,
        )
        .await?;
        let messages = collection(
            &mut tx,
            AssistantKind::Message,
            session.id()?,
            false,
            &mut budget,
        )
        .await?;
        let (state, answer) = classify(&turn, &session, messages)?;
        tx.commit().await?;
        let mut result = TextRecovery {
            turn,
            session,
            state,
            answer,
        };
        lease_records(
            [&mut result.turn, &mut result.session]
                .into_iter()
                .chain(result.answer.iter_mut()),
            &self.execution_results,
        )?;
        Ok(result)
    }
}

struct Count(usize);
impl Write for Count {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(b.len());
        if self.0 > MAX_TRANSACTION_BYTES {
            return Err(std::io::Error::other("execution byte limit"));
        }
        Ok(b.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn encoded_size<T: Serialize>(value: &T) -> Result<usize> {
    let mut out = Count(0);
    serde_json::to_writer(&mut out, value).map_err(|_| Error::ReadLimit)?;
    Ok(out.0)
}
fn record_size(record: &ExecutionRecord) -> Result<usize> {
    Ok(record.data.retained_bytes)
}
fn supported_kind(kind: AssistantKind) -> Result<()> {
    if matches!(
        kind,
        AssistantKind::Session | AssistantKind::Message | AssistantKind::Turn
    ) {
        Ok(())
    } else {
        Err(Error::InvalidBounds)
    }
}
fn operation_size(operation: &Operation) -> Result<usize> {
    let mut bytes = 256usize;
    match operation {
        Operation::Admit(a) => {
            if a.messages.is_empty() || a.messages.len() > MAX_INPUT_MESSAGES {
                return Err(Error::TransactionLimit);
            }
            bytes += match &a.session {
                AdmissionSession::New(s) => record_size(s)?,
                AdmissionSession::Existing {
                    id,
                    expected_revision,
                } => {
                    valid_revision(*expected_revision)?;
                    validate_id(id)?;
                    id.len() + 16
                }
            };
            bytes = bytes
                .saturating_add(record_size(&a.turn)?)
                .saturating_add(encoded_size(&a.settings)?)
                .saturating_add(a.provider_profile_id.len())
                .saturating_add(a.resolved_model.len());
            for m in &a.messages {
                bytes = bytes.saturating_add(record_size(m)?);
                if bytes > MAX_TRANSACTION_BYTES {
                    return Err(Error::TransactionLimit);
                }
            }
        }
        Operation::Claim {
            turn_id,
            expected_revision,
            claim,
        } => {
            validate_id(turn_id)?;
            validate_id(&claim.worker_id)?;
            validate_id(&claim.claim_id)?;
            valid_revision(*expected_revision)?;
            bytes += turn_id.len() + claim.worker_id.len() + claim.claim_id.len();
        }
        Operation::Answer {
            fence,
            turn_revision,
            session_revision,
            message,
        } => {
            validate_fence(fence)?;
            valid_revision(*turn_revision)?;
            valid_revision(*session_revision)?;
            bytes += fence_size(fence) + record_size(message)?;
        }
        Operation::Complete {
            fence,
            expected_revision,
            message_id,
            usage,
        } => {
            validate_fence(fence)?;
            valid_revision(*expected_revision)?;
            validate_id(message_id)?;
            bytes += fence_size(fence) + message_id.len() + encoded_size(usage)?;
        }
        Operation::Release {
            fence,
            expected_revision,
        } => {
            validate_fence(fence)?;
            valid_revision(*expected_revision)?;
            bytes += fence_size(fence);
        }
        Operation::Terminal {
            turn_id,
            expected_revision,
            action,
        } => {
            validate_id(turn_id)?;
            valid_revision(*expected_revision)?;
            bytes += turn_id.len();
            match action {
                TerminalAction::Stop => {}
                TerminalAction::Fail { fence, error } => {
                    validate_fence(fence)?;
                    bytes += fence_size(fence) + error.len();
                }
                TerminalAction::Interrupt { fence: Some(f), .. } => {
                    validate_fence(f)?;
                    bytes += fence_size(f);
                }
                _ => {}
            }
        }
        Operation::Outcome {
            turn_id,
            turn_revision,
            session_revision,
            message,
        } => {
            validate_id(turn_id)?;
            valid_revision(*turn_revision)?;
            valid_revision(*session_revision)?;
            bytes += turn_id.len() + record_size(message)?;
        }
    }
    if bytes > MAX_TRANSACTION_BYTES {
        Err(Error::TransactionLimit)
    } else {
        Ok(bytes)
    }
}
fn fence_size(f: &ExecutionFence) -> usize {
    f.turn_id.len() + f.worker_id.len() + f.claim_id.len() + 32
}
fn validate_fence(f: &ExecutionFence) -> Result<()> {
    validate_id(&f.turn_id)?;
    validate_id(&f.worker_id)?;
    validate_id(&f.claim_id)
}
fn valid_revision(revision: i64) -> Result<()> {
    if revision >= 1 {
        Ok(())
    } else {
        Err(Error::InvalidBounds)
    }
}
fn text<'a>(p: &'a Value, key: &str) -> Result<&'a str> {
    p[key].as_str().ok_or(Error::CorruptEnvelope)
}
fn conflict(detail: &str) -> Error {
    Error::ExecutionConflict(detail.to_owned())
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64().is_none_or(|n| n != 0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
fn direct_error(error: RecordError) -> Error {
    match error {
        RecordError::TooLarge => Error::ReadLimit,
        RecordError::ModelValidation(r) => Error::RetainedModelValidation(r),
        e => Error::Record(e),
    }
}
fn wrapped_error(error: RecordError) -> Error {
    match error {
        RecordError::TooLarge => Error::ReadLimit,
        e @ (RecordError::ModelValidation(_)
        | RecordError::Shape(_)
        | RecordError::Invariant(_)) => Error::WrappedRecord(e),
        e => Error::Record(e),
    }
}
#[derive(Default)]
struct Budget {
    bytes: usize,
    rows: usize,
}
impl Budget {
    fn add(&mut self, bytes: usize) -> Result<()> {
        self.bytes = self.bytes.saturating_add(bytes);
        if self.bytes > MAX_TRANSACTION_BYTES {
            Err(Error::ReadLimit)
        } else {
            Ok(())
        }
    }
    fn row(&mut self, row: &SqliteRow) -> Result<()> {
        self.rows += 1;
        if self.rows > 10_000 {
            return Err(Error::ReadLimit);
        }
        let size: i64 = row.try_get("payload_bytes")?;
        if size < 0 || size as u64 > MAX_RECORD_BYTES as u64 {
            return Err(Error::ReadLimit);
        }
        self.add(size as usize)
    }
    fn error(&mut self, error: RecordError, wrapped: bool) -> Error {
        if let RecordError::ModelValidation(report) = &error
            && self.add(report.retained_bytes()).is_err()
        {
            return Error::ReadLimit;
        }
        if wrapped {
            wrapped_error(error)
        } else {
            direct_error(error)
        }
    }
}
fn decode(row: &SqliteRow, budget: &mut Budget, wrapped: bool) -> Result<ExecutionRecord> {
    budget.row(row)?;
    let raw: &str = row.try_get("payload")?;
    let kind = AssistantKind::try_from(row.try_get::<&str, _>("kind")?)?;
    supported_kind(kind)?;
    let record = if kind == AssistantKind::Turn {
        StoredAssistantRecord::decode_execution_turn_direct(
            raw.as_bytes(),
            InputOrigin::RetainedJson,
        )
    } else {
        StoredAssistantRecord::decode_fork_persisted_direct(kind, raw.as_bytes())
    }
    .map_err(|e| budget.error(e, wrapped))?;
    let p = record.payload();
    if p["id"].as_str() != Some(row.try_get("id")?)
        || p["revision"].as_i64() != Some(row.try_get("revision")?)
        || p["engagement_id"].as_str() != row.try_get::<Option<&str>, _>("engagement_id")?
        || session_projection(&record) != row.try_get::<Option<&str>, _>("chat_session_id")?
        || sql_time(&p["created_at"])? != row.try_get::<&str, _>("created_at")?
        || sql_time(&p["updated_at"])? != row.try_get::<&str, _>("updated_at")?
    {
        return Err(Error::CorruptEnvelope);
    }
    let result = ExecutionRecord::new(record, raw)?;
    budget.add(record_size(&result)?)?;
    Ok(result)
}
async fn read(
    connection: &mut SqliteConnection,
    kind: AssistantKind,
    id: &str,
    budget: &mut Budget,
    wrapped: bool,
) -> Result<ExecutionRecord> {
    let row = sqlx::query(&format!("{SELECT_RECORD} WHERE kind=? AND id=?"))
        .bind(kind.as_str())
        .bind(id)
        .fetch_optional(connection)
        .await?
        .ok_or(Error::NotFound)?;
    decode(&row, budget, wrapped)
}
async fn collection(
    connection: &mut SqliteConnection,
    kind: AssistantKind,
    session: &str,
    active: bool,
    budget: &mut Budget,
) -> Result<Vec<ExecutionRecord>> {
    let mut q = QueryBuilder::new(SELECT_RECORD);
    q.push(" WHERE kind=")
        .push_bind(kind.as_str())
        .push(" AND chat_session_id=")
        .push_bind(session);
    if active {
        q.push(" AND json_extract(payload,'$.status') IN ('routing','waiting_approval','waiting_callback','finalizing','interrupted')");
    }
    q.push(" ORDER BY created_at,id LIMIT 10001");
    let query = q.build();
    let mut rows = query.fetch(connection);
    let mut result = Vec::new();
    while let Some(row) = rows.try_next().await? {
        result.push(decode(&row, budget, false)?);
    }
    Ok(result)
}
fn expected(record: &ExecutionRecord, revision: i64) -> Result<()> {
    let found = record.revision()?;
    if revision == found {
        Ok(())
    } else {
        Err(Error::RevisionConflict {
            expected: revision,
            found,
        })
    }
}
fn has_owner(turn: &ExecutionRecord, fence: &ExecutionFence) -> bool {
    let p = turn.payload();
    p["id"] == fence.turn_id
        && p["execution_owner_id"] == fence.worker_id
        && p["execution_claim_id"] == fence.claim_id
}
fn owner(turn: &ExecutionRecord, fence: &ExecutionFence) -> Result<()> {
    if has_owner(turn, fence) {
        Ok(())
    } else {
        Err(conflict(
            "chat response ownership changed; stale worker output was discarded",
        ))
    }
}
fn text_turn(turn: &ExecutionRecord) -> Result<()> {
    let p = turn.payload();
    if turn.record().kind() != AssistantKind::Turn
        || p["backend"] != "provider"
        || p["tools_enabled"] != false
        || truthy(&p["goal_id"])
        || truthy(&p["harness_turn_id"])
        || truthy(&p["approval_id"])
        || truthy(&p["tool_call_ids"])
        || truthy(&p["tool_history"])
    {
        return Err(Error::ExecutionUnsupported(
            "This execution writer supports provider text turns without goals, tools or approvals",
        ));
    }
    for key in [
        "hook_snapshots",
        "skill_snapshots",
        "mcp_server_ids",
        "mcp_snapshot",
        "ssh_environment_snapshot",
        "browser_session_id",
        "allow_subagents",
        "subagent_child",
        "allow_agent_messaging",
        "include_oci_tools",
    ] {
        if truthy(&p["request_snapshot"][key]) {
            return Err(Error::ExecutionUnsupported(
                "This text turn contains an unsupported execution capability",
            ));
        }
    }
    if truthy(&p["request_snapshot"]["model_request"]["tools"]) {
        return Err(Error::ExecutionUnsupported(
            "This text turn advertises tools",
        ));
    }
    Ok(())
}
fn assert_available(turns: &[ExecutionRecord]) -> Result<()> {
    let mut active = false;
    for turn in turns {
        let p = turn.payload();
        if p["status"] != "interrupted" {
            active = true;
            continue;
        }
        match p["request_snapshot"].get("recovery") {
            None => {}
            Some(Value::Object(v)) => active |= v.get("required").is_some_and(truthy),
            Some(_) => return Err(Error::ExecutionInvalidState),
        }
    }
    if active {
        Err(conflict("chat session already has an active response"))
    } else {
        Ok(())
    }
}
fn decimal(value: &Value) -> Option<String> {
    let Value::Number(n) = value else { return None };
    let text = n.to_string();
    (!text.is_empty() && text.bytes().all(|b| b.is_ascii_digit())).then_some(text)
}
fn compare_decimal(left: &str, right: &str) -> std::cmp::Ordering {
    left.len().cmp(&right.len()).then_with(|| left.cmp(right))
}
fn increment(value: &str) -> String {
    let mut bytes = value.as_bytes().to_vec();
    for byte in bytes.iter_mut().rev() {
        if *byte < b'9' {
            *byte += 1;
            return String::from_utf8(bytes).expect("ASCII integer");
        }
        *byte = b'0';
    }
    bytes.insert(0, b'1');
    String::from_utf8(bytes).expect("ASCII integer")
}

fn next_sequence(session: &ExecutionRecord, messages: &[ExecutionRecord]) -> Result<String> {
    let recorded = &session.payload()["metadata"]["last_sequence"];
    let mut max = match recorded {
        Value::Bool(true) => "1".into(),
        _ => decimal(recorded).unwrap_or_else(|| "0".into()),
    };
    for message in messages {
        let seq = decimal(&message.payload()["sequence"]).ok_or(Error::ExecutionInvalidState)?;
        if compare_decimal(&seq, &max).is_gt() {
            max = seq;
        }
    }
    Ok(increment(&max))
}
fn check_sequence(message: &ExecutionRecord, sequence: &str) -> Result<()> {
    if decimal(&message.payload()["sequence"]).as_deref() == Some(sequence) {
        Ok(())
    } else {
        Err(conflict(
            "conversation transcript changed while its next sequence was being reserved",
        ))
    }
}
fn message_scope(message: &ExecutionRecord, session: &ExecutionRecord) -> Result<()> {
    if message.record().kind() != AssistantKind::Message
        || message.payload()["session_id"] != session.payload()["id"]
        || message.payload()["engagement_id"] != session.payload()["engagement_id"]
    {
        Err(Error::InvalidBounds)
    } else {
        Ok(())
    }
}
fn turn_scope(turn: &ExecutionRecord, session: &ExecutionRecord) -> Result<()> {
    if turn.payload()["session_id"] != session.payload()["id"]
        || turn.payload()["engagement_id"] != session.payload()["engagement_id"]
        || text(session.payload(), "engagement_id")?.is_empty()
    {
        Err(Error::InvalidBounds)
    } else {
        Ok(())
    }
}
fn new_record(record: &ExecutionRecord, kind: AssistantKind) -> Result<()> {
    if record.record().kind() == kind && record.revision()? == 1 {
        Ok(())
    } else {
        Err(Error::InvalidBounds)
    }
}
fn clear_claim() -> Map<String, Value> {
    [
        ("execution_owner_id".into(), Value::Null),
        ("execution_claim_id".into(), Value::Null),
        ("execution_claimed_at".into(), Value::Null),
    ]
    .into_iter()
    .collect()
}
fn claim_changes(claim: &Claim) -> Map<String, Value> {
    [
        ("execution_owner_id".into(), claim.worker_id.clone().into()),
        ("execution_claim_id".into(), claim.claim_id.clone().into()),
        (
            "execution_claimed_at".into(),
            timestamp(claim.claimed_at).into(),
        ),
    ]
    .into_iter()
    .collect()
}
fn timestamp(now: DateTime<Utc>) -> String {
    now.to_rfc3339_opts(
        if now.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        true,
    )
}
fn metadata_with_cursor(session: &ExecutionRecord, sequence: &Value) -> Result<Map<String, Value>> {
    let mut metadata = session.payload()["metadata"]
        .as_object()
        .ok_or(Error::CorruptEnvelope)?
        .clone();
    metadata.insert("message_count".into(), sequence.clone());
    metadata.insert("last_sequence".into(), sequence.clone());
    Ok(metadata)
}

struct Output(Vec<u8>);
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("execution record byte limit"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn bounded_json<T: Serialize>(value: &T) -> Result<String> {
    let mut output = Output(Vec::new());
    serde_json::to_writer(&mut output, value).map_err(|_| Error::ReadLimit)?;
    String::from_utf8(output.0).map_err(|_| Error::CorruptEnvelope)
}
/// Only recurse through declared record containers. Changed opaque values are
/// encoded wholesale, avoiding depth-times-payload retained allocations.
struct Overlay<'a> {
    before: &'a Value,
    next: &'a Value,
    raw: &'a str,
    depth: u8,
}
impl Serialize for Overlay<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let fields = self
            .next
            .as_object()
            .ok_or_else(|| serde::ser::Error::custom("expected object"))?;
        let raw: HashMap<String, &RawValue> =
            serde_json::from_str(self.raw).map_err(serde::ser::Error::custom)?;
        let mut output = serializer.serialize_map(Some(fields.len()))?;
        for (key, next) in fields {
            let previous = self.before.get(key);
            if previous == Some(next)
                && let Some(fragment) = raw.get(key)
            {
                output.serialize_entry(key, *fragment)?;
            } else if self.depth == 0
                && matches!(key.as_str(), "metadata" | "request_snapshot")
                && next.is_object()
                && previous.is_some_and(Value::is_object)
                && let Some(fragment) = raw.get(key)
            {
                output.serialize_entry(
                    key,
                    &Overlay {
                        before: previous.unwrap(),
                        next,
                        raw: fragment.get(),
                        depth: 1,
                    },
                )?;
            } else {
                output.serialize_entry(key, next)?;
            }
        }
        output.end()
    }
}
fn changed(
    current: &ExecutionRecord,
    changes: Map<String, Value>,
    now: DateTime<Utc>,
    budget: &mut Budget,
) -> Result<ExecutionRecord> {
    let mut payload = current.payload().clone();
    let fields = payload.as_object_mut().ok_or(Error::CorruptEnvelope)?;
    fields.extend(changes);
    fields.insert(
        "revision".into(),
        current
            .revision()?
            .checked_add(1)
            .ok_or(Error::CorruptEnvelope)?
            .into(),
    );
    fields.insert("updated_at".into(), timestamp(now).into());
    let raw = bounded_json(&Overlay {
        before: current.payload(),
        next: &payload,
        raw: current.raw_payload(),
        depth: 0,
    })?;
    let record = if current.record().kind() == AssistantKind::Session {
        let payload = hydrate(
            Model::ChatSession,
            InputOrigin::WriterModelDump,
            raw.as_bytes(),
        )
        .map_err(|e| budget.error(e, false))?;
        StoredAssistantRecord::decode(
            AssistantKind::Session,
            &bounded_json(&payload)?.into_bytes(),
        )
        .map_err(|e| budget.error(e, false))?
    } else if current.record().kind() == AssistantKind::Turn {
        StoredAssistantRecord::decode_execution_turn_direct(
            raw.as_bytes(),
            InputOrigin::WriterModelDump,
        )
        .map_err(|e| budget.error(e, false))?
    } else {
        StoredAssistantRecord::decode_updated_direct(current.record().kind(), raw.as_bytes())
            .map_err(|e| budget.error(e, false))?
    };
    let result = ExecutionRecord::new(record, &raw)?;
    budget.add(record_size(&result)?)?;
    Ok(result)
}
async fn insert(connection: &mut SqliteConnection, record: &ExecutionRecord) -> Result<()> {
    let p = record.payload();
    sqlx::query("INSERT INTO entities (id,kind,engagement_id,revision,payload,chat_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)")
        .bind(record.id()?).bind(record.record().kind().as_str()).bind(p["engagement_id"].as_str()).bind(record.revision()?).bind(record.raw_payload()).bind(session_projection(record.record())).bind(sql_time(&p["created_at"])?).bind(sql_time(&p["updated_at"])?).execute(&mut *connection).await.map_err(|error|match Error::from(error){Error::Conflict=>Error::AlreadyExists(record.id().unwrap_or_default().into()),e=>e})?;
    update_search(connection, record.record()).await
}
async fn update(
    connection: &mut SqliteConnection,
    current: &ExecutionRecord,
    changes: Map<String, Value>,
    clock: &Arc<StateClock>,
    budget: &mut Budget,
) -> Result<ExecutionRecord> {
    let next = changed(current, changes, clock(), budget)?;
    let p = next.payload();
    let result=sqlx::query("UPDATE entities SET engagement_id=?,revision=?,payload=?,chat_session_id=?,updated_at=? WHERE id=? AND kind=? AND revision=?")
        .bind(p["engagement_id"].as_str()).bind(next.revision()?).bind(next.raw_payload()).bind(session_projection(next.record())).bind(sql_time(&p["updated_at"])?).bind(current.id()?).bind(current.record().kind().as_str()).bind(current.revision()?).execute(&mut *connection).await?;
    if result.rows_affected() != 1 {
        return Err(Error::Conflict);
    }
    update_search(connection, next.record()).await?;
    Ok(next)
}
fn settings(settings: &TextSettings) -> Value {
    serde_json::json!({"mcp_server_ids":[],"hook_ids":[],"reasoning_effort":settings.reasoning_effort,"allow_subagents":false,"max_active_subagents":settings.max_active_subagents,"allow_agent_messaging":false})
}
async fn admit(
    connection: &mut SqliteConnection,
    input: &TextAdmission,
    clock: &Arc<StateClock>,
    budget: &mut Budget,
) -> Result<AdmissionCommit> {
    new_record(&input.turn, AssistantKind::Turn)?;
    text_turn(&input.turn)?;
    if input.turn.payload()["status"] != "routing"
        || !input.turn.payload()["execution_claim_id"].is_null()
        || !input.turn.payload()["final_message_id"].is_null()
    {
        return Err(Error::InvalidBounds);
    }
    let session = match &input.session {
        AdmissionSession::New(s) => {
            new_record(s, AssistantKind::Session)?;
            s.clone()
        }
        AdmissionSession::Existing { id, .. } => {
            read(connection, AssistantKind::Session, id, budget, true).await?
        }
    };
    turn_scope(&input.turn, &session)?;
    if input.turn.payload()["provider_profile_id"] != input.provider_profile_id {
        return Err(Error::InvalidBounds);
    }
    let unfinished =
        collection(connection, AssistantKind::Turn, session.id()?, true, budget).await?;
    assert_available(&unfinished)?;
    let prior = collection(
        connection,
        AssistantKind::Message,
        session.id()?,
        false,
        budget,
    )
    .await?;
    let mut sequence = next_sequence(&session, &prior)?;
    let mut ids = HashSet::new();
    for message in &input.messages {
        new_record(message, AssistantKind::Message)?;
        message_scope(message, &session)?;
        check_sequence(message, &sequence)?;
        if !matches!(
            message.payload()["role"].as_str(),
            Some("user" | "assistant")
        ) || !ids.insert(message.id()?)
        {
            return Err(Error::InvalidBounds);
        }
        sequence = increment(&sequence);
    }
    if matches!(input.session, AdmissionSession::Existing { .. })
        && (input.messages.len() != 1 || input.messages[0].payload()["role"] != "user")
    {
        return Err(Error::InvalidBounds);
    }
    if input
        .messages
        .last()
        .is_none_or(|m| m.payload()["role"] != "user")
    {
        return Err(Error::InvalidBounds);
    }
    let last = &input.messages.last().ok_or(Error::InvalidBounds)?.payload()["sequence"];
    let mut metadata = metadata_with_cursor(&session, last)?;
    metadata.insert("tools_enabled".into(), false.into());
    metadata.extend(
        settings(&input.settings)
            .as_object()
            .ok_or(Error::CorruptEnvelope)?
            .clone(),
    );
    let session = match &input.session {
        AdmissionSession::New(_) => {
            if session.payload()["backend"] != "provider"
                || session.payload()["provider_profile_id"] != input.provider_profile_id
            {
                return Err(Error::InvalidBounds);
            }
            let mut p = session.payload().clone();
            p["metadata"] = metadata.into();
            let raw = bounded_json(&Overlay {
                before: session.payload(),
                next: &p,
                raw: session.raw_payload(),
                depth: 0,
            })?;
            let record = StoredAssistantRecord::decode_fork_persisted_direct(
                AssistantKind::Session,
                raw.as_bytes(),
            )
            .map_err(|e| budget.error(e, false))?;
            let next = ExecutionRecord::new(record, &raw)?;
            budget.add(record_size(&next)?)?;
            insert(connection, &next).await?;
            next
        }
        AdmissionSession::Existing {
            expected_revision, ..
        } => {
            expected(&session, *expected_revision)?;
            update(
                connection,
                &session,
                [
                    ("backend".into(), "provider".into()),
                    (
                        "provider_profile_id".into(),
                        input.provider_profile_id.clone().into(),
                    ),
                    ("harness_profile_id".into(), Value::Null),
                    ("harness_session_id".into(), Value::Null),
                    ("model".into(), input.resolved_model.clone().into()),
                    ("metadata".into(), metadata.into()),
                ]
                .into_iter()
                .collect(),
                clock,
                budget,
            )
            .await?
        }
    };
    for message in &input.messages {
        insert(connection, message).await?;
    }
    insert(connection, &input.turn).await?;
    Ok(AdmissionCommit {
        session,
        turn: input.turn.clone(),
        messages: input.messages.clone(),
    })
}
fn active(turn: &ExecutionRecord) -> Result<()> {
    if matches!(
        turn.payload()["status"].as_str(),
        Some("routing" | "finalizing")
    ) {
        Ok(())
    } else {
        Err(conflict(&format!(
            "chat turn cannot start from {}",
            text(turn.payload(), "status")?
        )))
    }
}
fn answer_scope(
    message: &ExecutionRecord,
    turn: &ExecutionRecord,
    session: &ExecutionRecord,
) -> Result<()> {
    message_scope(message, session)?;
    turn_scope(turn, session)?;
    if message.payload()["role"] != "assistant"
        || message.payload()["metadata"]["chat_turn_id"] != turn.payload()["id"]
        || message.payload()["provider_profile_id"] != turn.payload()["provider_profile_id"]
    {
        return Err(Error::InvalidBounds);
    }
    Ok(())
}
async fn answer(
    connection: &mut SqliteConnection,
    fence: &ExecutionFence,
    turn_revision: i64,
    session_revision: i64,
    message: &ExecutionRecord,
    clock: &Arc<StateClock>,
    budget: &mut Budget,
) -> Result<AnswerCommit> {
    let turn = read(
        connection,
        AssistantKind::Turn,
        &fence.turn_id,
        budget,
        true,
    )
    .await?;
    text_turn(&turn)?;
    owner(&turn, fence)?;
    active(&turn)?;
    expected(&turn, turn_revision)?;
    let session = read(
        connection,
        AssistantKind::Session,
        text(turn.payload(), "session_id")?,
        budget,
        true,
    )
    .await?;
    expected(&session, session_revision)?;
    new_record(message, AssistantKind::Message)?;
    answer_scope(message, &turn, &session)?;
    if message.payload()["metadata"]["kind"] == "turn_outcome" {
        return Err(Error::InvalidBounds);
    }
    let messages = collection(
        connection,
        AssistantKind::Message,
        session.id()?,
        false,
        budget,
    )
    .await?;
    if messages
        .iter()
        .any(|m| m.payload()["metadata"]["chat_turn_id"] == turn.payload()["id"])
    {
        return Err(Error::ExecutionUncertain(
            "A durable message already represents this turn; inspect recovery before settling it again",
        ));
    }
    check_sequence(message, &next_sequence(&session, &messages)?)?;
    let claim = [
        "execution_owner_id",
        "execution_claim_id",
        "execution_claimed_at",
    ]
    .into_iter()
    .map(|key| (key.to_string(), turn.payload()[key].clone()))
    .collect();
    let turn = update(connection, &turn, claim, clock, budget).await?;
    let mut metadata = metadata_with_cursor(&session, &message.payload()["sequence"])?;
    if metadata.contains_key("tools_enabled") {
        metadata.insert("tools_enabled".into(), false.into());
    }
    let session = update(
        connection,
        &session,
        [
            ("title".into(), session.payload()["title"].clone()),
            ("metadata".into(), metadata.into()),
        ]
        .into_iter()
        .collect(),
        clock,
        budget,
    )
    .await?;
    insert(connection, message).await?;
    Ok(AnswerCommit {
        session,
        turn,
        message: message.clone(),
    })
}

async fn complete(
    connection: &mut SqliteConnection,
    fence: &ExecutionFence,
    revision: i64,
    message_id: &str,
    usage: &Value,
    clock: &Arc<StateClock>,
    budget: &mut Budget,
) -> Result<ExecutionRecord> {
    let turn = read(
        connection,
        AssistantKind::Turn,
        &fence.turn_id,
        budget,
        true,
    )
    .await?;
    text_turn(&turn)?;
    owner(&turn, fence)?;
    if turn.payload()["status"] == "complete" {
        return Ok(turn);
    }
    active(&turn)?;
    expected(&turn, revision)?;
    let session = read(
        connection,
        AssistantKind::Session,
        text(turn.payload(), "session_id")?,
        budget,
        true,
    )
    .await?;
    let message = read(connection, AssistantKind::Message, message_id, budget, true).await?;
    answer_scope(&message, &turn, &session)?;
    if message.payload()["metadata"]["kind"] == "turn_outcome"
        || truthy(&message.payload()["metadata"]["retracted_at"])
        || &message.payload()["usage"] != usage
    {
        return Err(Error::ExecutionUncertain(
            "The saved answer does not match this completion",
        ));
    }
    update(
        connection,
        &turn,
        [
            ("status".into(), "complete".into()),
            ("final_message_id".into(), message_id.into()),
            ("usage".into(), usage.clone()),
            ("error".into(), Value::Null),
        ]
        .into_iter()
        .collect(),
        clock,
        budget,
    )
    .await
}
async fn terminal(
    connection: &mut SqliteConnection,
    id: &str,
    revision: i64,
    action: &TerminalAction,
    clock: &Arc<StateClock>,
    budget: &mut Budget,
) -> Result<ExecutionRecord> {
    let turn = read(connection, AssistantKind::Turn, id, budget, true).await?;
    text_turn(&turn)?;
    let status = text(turn.payload(), "status")?;
    let changes = match action {
        TerminalAction::Stop => {
            if matches!(status, "complete" | "cancelled") {
                return Ok(turn);
            }
            expected(&turn, revision)?;
            let mut changes = clear_claim();
            changes.insert("status".into(), "cancelled".into());
            changes.insert("error".into(), "response stopped".into());
            changes
        }
        TerminalAction::Fail { fence, error } => {
            owner(&turn, fence)?;
            active(&turn)?;
            expected(&turn, revision)?;
            // Failure and release are separate source commits. Retain the claim
            // until the matching worker explicitly releases it.
            [
                ("status".into(), "failed".into()),
                ("error".into(), error.clone().into()),
            ]
            .into_iter()
            .collect()
        }
        TerminalAction::Interrupt {
            fence,
            cause,
            observed_at,
        } => {
            if let Some(fence) = fence {
                owner(&turn, fence)?;
            }
            active(&turn)?;
            expected(&turn, revision)?;
            let label = match cause {
                InterruptCause::Restart => "Core restarted",
                InterruptCause::Shutdown => "Core stopped",
            };
            let mut snapshot = turn.payload()["request_snapshot"]
                .as_object()
                .ok_or(Error::ExecutionInvalidState)?
                .clone();
            snapshot.insert("recovery".into(),serde_json::json!({"required":true,"cause":match cause{InterruptCause::Restart=>"core_restart",InterruptCause::Shutdown=>"core_shutdown"},"unknown_tool_call_ids":[],"unknown_hook_execution_ids":[],"rerunnable_hook_execution_ids":[],"interrupted_at":observed_at.to_rfc3339_opts(if observed_at.timestamp_subsec_micros()==0{SecondsFormat::Secs}else{SecondsFormat::Micros},false)}));
            let mut changes = clear_claim();
            changes.insert("status".into(), "interrupted".into());
            changes.insert("request_snapshot".into(), snapshot.into());
            changes.insert(
                "error".into(),
                format!(
                    "{label} before this response completed. Core will resume it automatically."
                )
                .into(),
            );
            changes
        }
    };
    update(connection, &turn, changes, clock, budget).await
}
async fn outcome(
    connection: &mut SqliteConnection,
    id: &str,
    turn_revision: i64,
    session_revision: i64,
    message: &ExecutionRecord,
    clock: &Arc<StateClock>,
    budget: &mut Budget,
) -> Result<OutcomeCommit> {
    let turn = read(connection, AssistantKind::Turn, id, budget, true).await?;
    text_turn(&turn)?;
    if !matches!(
        turn.payload()["status"].as_str(),
        Some("failed" | "cancelled")
    ) || !turn.payload()["final_message_id"].is_null()
    {
        return Ok(OutcomeCommit::AlreadyRecorded);
    }
    expected(&turn, turn_revision)?;
    let session = read(
        connection,
        AssistantKind::Session,
        text(turn.payload(), "session_id")?,
        budget,
        true,
    )
    .await?;
    let messages = collection(
        connection,
        AssistantKind::Message,
        session.id()?,
        false,
        budget,
    )
    .await?;
    if messages
        .iter()
        .any(|m| m.payload()["metadata"]["chat_turn_id"] == turn.payload()["id"])
    {
        return Ok(OutcomeCommit::AlreadyRecorded);
    }
    expected(&session, session_revision)?;
    new_record(message, AssistantKind::Message)?;
    answer_scope(message, &turn, &session)?;
    if message.payload()["finish_reason"] != "interrupted"
        || message.payload()["metadata"]["kind"] != "turn_outcome"
        || message.payload()["metadata"]["turn_status"] != turn.payload()["status"]
        || message.payload()["metadata"]["interrupted"] != true
    {
        return Err(Error::InvalidBounds);
    }
    check_sequence(message, &next_sequence(&session, &messages)?)?;
    let metadata = metadata_with_cursor(&session, &message.payload()["sequence"])?;
    let session = update(
        connection,
        &session,
        [("metadata".into(), metadata.into())].into_iter().collect(),
        clock,
        budget,
    )
    .await?;
    insert(connection, message).await?;
    Ok(OutcomeCommit::Written {
        session,
        message: message.clone(),
    })
}
fn classify(
    turn: &ExecutionRecord,
    session: &ExecutionRecord,
    messages: Vec<ExecutionRecord>,
) -> Result<(RecoveryState, Option<ExecutionRecord>)> {
    turn_scope(turn, session)?;
    let mut found = None;
    for message in messages {
        if message.payload()["metadata"]["chat_turn_id"] != turn.payload()["id"] {
            continue;
        }
        if message.payload()["metadata"]["kind"] == "turn_outcome" {
            continue;
        }
        if answer_scope(&message, turn, session).is_err()
            || truthy(&message.payload()["metadata"]["retracted_at"])
        {
            return Ok((
                RecoveryState::Uncertain(
                    "A retained answer has mismatched identity or was replaced",
                ),
                None,
            ));
        }
        if found.is_some() {
            return Ok((
                RecoveryState::Uncertain("Multiple retained answers refer to this turn"),
                None,
            ));
        }
        found = Some(message);
    }
    if turn.payload()["status"] == "complete" {
        if let Some(message) = found
            && message.payload()["id"] == turn.payload()["final_message_id"]
            && message.payload()["usage"] == turn.payload()["usage"]
        {
            return Ok((
                RecoveryState::Complete {
                    message_id: message.id()?.into(),
                    claim_retained: !turn.payload()["execution_claim_id"].is_null(),
                },
                Some(message),
            ));
        }
        return Ok((
            RecoveryState::Uncertain("The completion has no matching trustworthy saved answer"),
            None,
        ));
    }
    if let Some(message) = found {
        if !turn.payload()["final_message_id"].is_null() {
            return Ok((
                RecoveryState::Uncertain("The turn has an inconsistent final answer pointer"),
                None,
            ));
        }
        return Ok((
            RecoveryState::SavedAnswer {
                message_id: message.id()?.into(),
            },
            Some(message),
        ));
    }
    if !turn.payload()["final_message_id"].is_null() {
        return Ok((
            RecoveryState::Uncertain(
                "The turn has a final answer pointer without a matching saved answer",
            ),
            None,
        ));
    }
    let state = match turn.payload()["status"].as_str() {
        Some("interrupted") => RecoveryState::Interrupted,
        Some("routing" | "finalizing") if turn.payload()["execution_claim_id"].is_null() => {
            RecoveryState::Unclaimed
        }
        Some("routing" | "finalizing") => RecoveryState::ClaimedUncertain,
        _ => RecoveryState::Terminal,
    };
    Ok((state, None))
}

pub(super) async fn write(
    connection: &mut SqliteConnection,
    operation: &Operation,
    clock: &Arc<StateClock>,
    results: &Arc<Semaphore>,
) -> Result<Reply> {
    let mut budget = Budget::default();
    budget.add(operation_size(operation)?)?;
    let mut tx = connection.begin_with("BEGIN IMMEDIATE").await?;
    let mut reply = match operation {
        Operation::Admit(input) => {
            Reply::Admission(admit(&mut tx, input, clock, &mut budget).await?)
        }
        Operation::Claim {
            turn_id,
            expected_revision,
            claim,
        } => {
            let turn = read(&mut tx, AssistantKind::Turn, turn_id, &mut budget, true).await?;
            text_turn(&turn)?;
            if !turn.payload()["execution_claim_id"].is_null() {
                return Err(conflict(
                    "chat response is already owned by another Core worker",
                ));
            }
            active(&turn)?;
            expected(&turn, *expected_revision)?;
            Reply::Record(update(&mut tx, &turn, claim_changes(claim), clock, &mut budget).await?)
        }
        Operation::Answer {
            fence,
            turn_revision,
            session_revision,
            message,
        } => Reply::Answer(
            answer(
                &mut tx,
                fence,
                *turn_revision,
                *session_revision,
                message,
                clock,
                &mut budget,
            )
            .await?,
        ),
        Operation::Complete {
            fence,
            expected_revision,
            message_id,
            usage,
        } => Reply::Record(
            complete(
                &mut tx,
                fence,
                *expected_revision,
                message_id,
                usage,
                clock,
                &mut budget,
            )
            .await?,
        ),
        Operation::Release {
            fence,
            expected_revision,
        } => {
            let turn = read(
                &mut tx,
                AssistantKind::Turn,
                &fence.turn_id,
                &mut budget,
                true,
            )
            .await?;
            text_turn(&turn)?;
            Reply::Release(if !has_owner(&turn, fence) {
                ReleaseOutcome::NoLongerOwner(turn)
            } else {
                expected(&turn, *expected_revision)?;
                ReleaseOutcome::Released(
                    update(&mut tx, &turn, clear_claim(), clock, &mut budget).await?,
                )
            })
        }
        Operation::Terminal {
            turn_id,
            expected_revision,
            action,
        } => Reply::Record(
            terminal(
                &mut tx,
                turn_id,
                *expected_revision,
                action,
                clock,
                &mut budget,
            )
            .await?,
        ),
        Operation::Outcome {
            turn_id,
            turn_revision,
            session_revision,
            message,
        } => Reply::Outcome(
            outcome(
                &mut tx,
                turn_id,
                *turn_revision,
                *session_revision,
                message,
                clock,
                &mut budget,
            )
            .await?,
        ),
    };
    let reply_bytes = match &reply {
        Reply::Admission(value) => {
            let mut bytes = record_size(&value.session)?.saturating_add(record_size(&value.turn)?);
            for message in &value.messages {
                bytes = bytes.saturating_add(record_size(message)?);
            }
            bytes
        }
        Reply::Record(value) => record_size(value)?,
        Reply::Answer(value) => record_size(&value.session)?
            .saturating_add(record_size(&value.turn)?)
            .saturating_add(record_size(&value.message)?),
        Reply::Release(ReleaseOutcome::Released(value) | ReleaseOutcome::NoLongerOwner(value)) => {
            record_size(value)?
        }
        Reply::Outcome(OutcomeCommit::AlreadyRecorded) => 0,
        Reply::Outcome(OutcomeCommit::Written { session, message }) => {
            record_size(session)?.saturating_add(record_size(message)?)
        }
    };
    budget.add(reply_bytes)?;
    match &mut reply {
        Reply::Admission(value) => lease_records(
            [&mut value.session, &mut value.turn]
                .into_iter()
                .chain(value.messages.iter_mut()),
            results,
        )?,
        Reply::Record(value) => lease_records([value], results)?,
        Reply::Answer(value) => lease_records(
            [&mut value.session, &mut value.turn, &mut value.message],
            results,
        )?,
        Reply::Release(ReleaseOutcome::Released(value) | ReleaseOutcome::NoLongerOwner(value)) => {
            lease_records([value], results)?
        }
        Reply::Outcome(OutcomeCommit::AlreadyRecorded) => {}
        Reply::Outcome(OutcomeCommit::Written { session, message }) => {
            lease_records([session, message], results)?
        }
    }
    tx.commit().await?;
    Ok(reply)
}
