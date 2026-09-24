//! Supervisor-owned durable admission. This module never dispatches providers.
//!
//! A submitted waiter is not an acknowledgement. Only `Committed` means the
//! store acknowledged its transaction. FairQueue owns every pending entry before
//! the writer can see it, including entries whose outcome is uncertain.

use crate::{FairQueue, Reservation, State, Work};
use nebula_assistant_domain::records::{AssistantKind, RecordError};
use nebula_assistant_storage::entities::{
    Error as StorageError, SqliteAssistantStore, StateClock,
    execution::{AdmissionCommit, AdmissionSession, ExecutionRecord, TextAdmission},
};
use serde::Serialize;
use std::{
    collections::{HashMap, HashSet},
    fmt,
    io::{self, Write},
    sync::{Arc, Mutex, MutexGuard},
};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot},
    task::{Id, JoinHandle, JoinSet},
};

const MAX_INPUT_BYTES: usize = 16 * 1024 * 1024;
const MAX_INPUT_MESSAGES: usize = 200;
/// Storage bounds report input/metadata at 16 MiB. Reserve its full possible
/// logical ownership before a job, plus fixed enum overhead; this is not RSS.
pub const ERROR_WORK_BYTES: usize = 16 * 1024 * 1024 + 1024;

#[derive(Clone, Copy, Debug)]
pub struct AdmissionLimits {
    pub running: usize,
    pub pending: usize,
    pub intake: usize,
    pub jobs: usize,
    pub context_bytes: usize,
    pub reply_slots: usize,
    pub reply_bytes: usize,
    /// Separate from caller-held error replies, so drain never waits for those
    /// callers to release an earlier report.
    pub error_work_bytes: usize,
    /// Maximum error retained in a reply. A job first reserves ERROR_WORK_BYTES,
    /// then shrinks that credit to its actual retained error. Larger replies are
    /// replaced by ReplyCapacity with the independent transaction disposition.
    pub error_allowance: usize,
}
impl Default for AdmissionLimits {
    fn default() -> Self {
        Self {
            running: 128,
            pending: 2048,
            intake: 128,
            jobs: 128,
            context_bytes: 256 * 1024 * 1024,
            reply_slots: 256,
            reply_bytes: 64 * 1024 * 1024,
            error_work_bytes: 64 * 1024 * 1024,
            error_allowance: 256 * 1024,
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SubmitError {
    #[error("assistant admission is closing")]
    Closed,
    #[error("assistant admission capacity is full")]
    Capacity,
    #[error("assistant admission limits are invalid")]
    InvalidConfiguration,
    #[error("assistant work does not match the retained admission identity")]
    Identity,
    #[error("assistant admission input exceeds its ownership byte allowance")]
    InputBytes,
    #[error("assistant input lease belongs to another controller")]
    ForeignLease,
    #[error(transparent)]
    Queue(#[from] crate::Error),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Disposition {
    Committed,
    RolledBack,
    Unknown,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailureClass {
    Capacity,
    Conflict,
    Missing,
    Validation,
    Closing,
    Database,
    Other,
    Supervisor,
}

/// Only immutable identities survive a successful admission. No Session,
/// Message, prompt or store result-byte lease is retained here.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AcceptedIdentity {
    pub turn_id: String,
    pub session_id: String,
    pub project_id: String,
}

/// The complete typed error remains borrowed behind its lifetime byte credit.
/// There is deliberately no into_inner or Clone that could detach that credit.
pub struct StorageFailure {
    error: Box<StorageError>,
    _credit: OwnedSemaphorePermit,
}
impl StorageFailure {
    pub fn error(&self) -> &StorageError {
        &self.error
    }
}
impl fmt::Debug for StorageFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("StorageFailure")
            .field("class", &classify(&self.error))
            .finish_non_exhaustive()
    }
}

#[derive(Debug)]
pub enum AdmissionFailure {
    Storage(StorageFailure),
    ReplyCapacity { class: FailureClass },
    Supervisor,
    Scheduling,
}
#[derive(Debug)]
pub struct AdmissionOutcome {
    pub disposition: Disposition,
    pub result: Result<AcceptedIdentity, AdmissionFailure>,
}

pub struct AdmissionWaiter {
    result: oneshot::Receiver<AdmissionOutcome>,
    _slot: OwnedSemaphorePermit,
}
impl AdmissionWaiter {
    /// Dropping this future or its waiter never cancels the supervised job.
    pub async fn wait(self) -> AdmissionOutcome {
        self.result.await.unwrap_or(AdmissionOutcome {
            disposition: Disposition::Unknown,
            result: Err(AdmissionFailure::Supervisor),
        })
    }
}
impl fmt::Debug for AdmissionWaiter {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AdmissionWaiter").finish_non_exhaustive()
    }
}

/// Acquire before materializing request/context copies. This accounts encoded
/// hydrated bytes plus original JSON and bounded metadata, not allocator RSS.
pub struct AdmissionLease {
    identity: Arc<()>,
    permit: OwnedSemaphorePermit,
}
impl fmt::Debug for AdmissionLease {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("AdmissionLease")
            .field("bytes", &self.permit.num_permits())
            .finish_non_exhaustive()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionState {
    Reserved,
    Ready,
    Uncertain,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct AdmissionSnapshot {
    pub closing: bool,
    pub outstanding: usize,
    pub uncertain: usize,
    pub available_context_bytes: usize,
    pub available_reply_bytes: usize,
    pub available_error_work_bytes: usize,
    pub available_reply_slots: usize,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ShutdownReport {
    pub ready: usize,
    pub uncertain: usize,
}

struct QueueOwner {
    queue: FairQueue,
    uncertain: HashSet<String>,
    closing: bool,
}
struct Shared {
    owner: Mutex<QueueOwner>,
    identity: Arc<()>,
    contexts: Arc<Semaphore>,
    reply_bytes: Arc<Semaphore>,
    error_work_bytes: Arc<Semaphore>,
    reply_slots: Arc<Semaphore>,
    error_allowance: usize,
}
impl Shared {
    fn owner(&self) -> MutexGuard<'_, QueueOwner> {
        self.owner.lock().unwrap_or_else(|error| error.into_inner())
    }
    fn close(&self) {
        self.owner().closing = true;
    }
}
struct Pending {
    ticket: Reservation,
    identity: AcceptedIdentity,
    reply: oneshot::Sender<AdmissionOutcome>,
}
struct Request {
    input: TextAdmission,
    context: AdmissionLease,
    error_credit: OwnedSemaphorePermit,
    pending: Pending,
}

#[derive(Clone)]
pub struct AdmissionHandle {
    shared: Arc<Shared>,
    requests: mpsc::Sender<Request>,
}
pub struct AdmissionSupervisor {
    shared: Arc<Shared>,
    stop: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<ShutdownReport>>,
}
pub struct AdmissionController;
impl AdmissionController {
    /// Requires an active Tokio runtime. The supplied store remains separately
    /// owned and must be shut down only after this supervisor has drained.
    pub fn start(
        store: SqliteAssistantStore,
        clock: Arc<StateClock>,
        limits: AdmissionLimits,
    ) -> Result<(AdmissionHandle, AdmissionSupervisor), SubmitError> {
        if !(1..=2048).contains(&limits.intake)
            || !(1..=2048).contains(&limits.jobs)
            || !(1..=65536).contains(&limits.reply_slots)
            || !(1..=256 * 1024 * 1024).contains(&limits.context_bytes)
            || !(256..=256 * 1024 * 1024).contains(&limits.reply_bytes)
            || !(ERROR_WORK_BYTES..=256 * 1024 * 1024).contains(&limits.error_work_bytes)
            || !(256..=limits.reply_bytes).contains(&limits.error_allowance)
        {
            return Err(SubmitError::InvalidConfiguration);
        }
        let shared = Arc::new(Shared {
            owner: Mutex::new(QueueOwner {
                queue: FairQueue::new(limits.running, limits.pending)?,
                uncertain: HashSet::new(),
                closing: false,
            }),
            identity: Arc::new(()),
            contexts: Arc::new(Semaphore::new(limits.context_bytes)),
            reply_bytes: Arc::new(Semaphore::new(limits.reply_bytes)),
            error_work_bytes: Arc::new(Semaphore::new(limits.error_work_bytes)),
            reply_slots: Arc::new(Semaphore::new(limits.reply_slots)),
            error_allowance: limits.error_allowance,
        });
        let (requests, receiver) = mpsc::channel(limits.intake);
        let (stop, stopped) = oneshot::channel();
        let task = tokio::spawn(supervise(
            shared.clone(),
            receiver,
            stopped,
            store,
            clock,
            limits.jobs,
        ));
        Ok((
            AdmissionHandle {
                shared: shared.clone(),
                requests,
            },
            AdmissionSupervisor {
                shared,
                stop: Some(stop),
                task: Some(task),
            },
        ))
    }
}
impl AdmissionHandle {
    pub fn try_reserve_bytes(&self, bytes: usize) -> Result<AdmissionLease, SubmitError> {
        if self.shared.owner().closing {
            return Err(SubmitError::Closed);
        }
        if bytes == 0 || bytes > MAX_INPUT_BYTES {
            return Err(SubmitError::InputBytes);
        }
        Ok(AdmissionLease {
            identity: self.shared.identity.clone(),
            permit: self
                .shared
                .contexts
                .clone()
                .try_acquire_many_owned(bytes as u32)
                .map_err(|_| SubmitError::Capacity)?,
        })
    }

    /// A returned waiter means ownership transferred, not that durable admission
    /// succeeded. Parent grouping is trusted preparation input, bounded by Work.
    pub fn try_submit(
        &self,
        input: TextAdmission,
        work: Work,
        context: AdmissionLease,
    ) -> Result<AdmissionWaiter, SubmitError> {
        if self.shared.owner().closing {
            return Err(SubmitError::Closed);
        }
        if !Arc::ptr_eq(&context.identity, &self.shared.identity) {
            return Err(SubmitError::ForeignLease);
        }
        validate_identity(&input, &work)?;
        input_bytes(&input, &work, context.permit.num_permits())?;
        let slot = self
            .shared
            .reply_slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| SubmitError::Capacity)?;
        let error_credit = self
            .shared
            .reply_bytes
            .clone()
            .try_acquire_many_owned(self.shared.error_allowance as u32)
            .map_err(|_| SubmitError::Capacity)?;
        let identity = AcceptedIdentity {
            turn_id: work.id.clone(),
            session_id: work.session_id.clone(),
            project_id: work.project_id.clone(),
        };
        let (reply, result) = oneshot::channel();
        // This lock protects only policy transitions; there is no awaited I/O.
        let mut owner = self.shared.owner();
        if owner.closing {
            return Err(SubmitError::Closed);
        }
        let ticket = owner.queue.reserve(work)?;
        let request = Request {
            input,
            context,
            error_credit,
            pending: Pending {
                ticket: ticket.clone(),
                identity,
                reply,
            },
        };
        if let Err(error) = self.requests.try_send(request) {
            // No writer has seen a failed enqueue. Only this exact generation
            // may be aborted, even if the work ID will later be reused.
            owner.queue.abort(ticket)?;
            drop(owner);
            return Err(match error {
                mpsc::error::TrySendError::Full(_) => SubmitError::Capacity,
                mpsc::error::TrySendError::Closed(_) => SubmitError::Closed,
            });
        }
        Ok(AdmissionWaiter {
            result,
            _slot: slot,
        })
    }

    pub fn state(&self, turn_id: &str) -> Option<AdmissionState> {
        let owner = self.shared.owner();
        if owner.uncertain.contains(turn_id) {
            return Some(AdmissionState::Uncertain);
        }
        match owner.queue.state(turn_id)? {
            State::Reserved => Some(AdmissionState::Reserved),
            State::Ready => Some(AdmissionState::Ready),
            State::Running | State::Waiting => None, // No dispatch in this owner.
        }
    }
    /// Diagnostic only; available credits cannot authorize later admission.
    pub fn snapshot(&self) -> AdmissionSnapshot {
        let owner = self.shared.owner();
        AdmissionSnapshot {
            closing: owner.closing,
            outstanding: owner.queue.outstanding(),
            uncertain: owner.uncertain.len(),
            available_context_bytes: self.shared.contexts.available_permits(),
            available_reply_bytes: self.shared.reply_bytes.available_permits(),
            available_error_work_bytes: self.shared.error_work_bytes.available_permits(),
            available_reply_slots: self.shared.reply_slots.available_permits(),
        }
    }
}
impl AdmissionSupervisor {
    pub async fn shutdown(mut self) -> Result<ShutdownReport, AdmissionFailure> {
        self.shared.close();
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        self.task
            .take()
            .ok_or(AdmissionFailure::Supervisor)?
            .await
            .map_err(|_| AdmissionFailure::Supervisor)
    }
}
impl Drop for AdmissionSupervisor {
    fn drop(&mut self) {
        self.shared.close();
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        // Dropping JoinHandle detaches; it must not abort accepted jobs. Owners
        // should call shutdown() to retain positive drain evidence.
    }
}

async fn supervise(
    shared: Arc<Shared>,
    mut requests: mpsc::Receiver<Request>,
    mut stopped: oneshot::Receiver<()>,
    store: SqliteAssistantStore,
    clock: Arc<StateClock>,
    job_limit: usize,
) -> ShutdownReport {
    let mut jobs = JoinSet::new();
    let mut pending: HashMap<Id, Pending> = HashMap::new();
    let mut closing = false;
    let mut empty = false;
    // One bounded staging request may wait for error-work credit. It retains
    // its existing reservation; FairQueue remains the only work scheduler.
    let mut waiting: Option<Request> = None;
    while !empty || waiting.is_some() || !jobs.is_empty() {
        tokio::select! {
            biased;
            _ = &mut stopped, if !closing => {
                closing = true;
                requests.close();
                shared.close();
            }
            result = jobs.join_next_with_id(), if !jobs.is_empty() => {
                if let Some(result) = result {
                    let (id, outcome) = match result {
                        Ok((id, outcome)) => (id, outcome),
                        Err(error) => (error.id(), AdmissionOutcome {
                            disposition: Disposition::Unknown,
                            result: Err(AdmissionFailure::Supervisor),
                        }),
                    };
                    if let Some(entry) = pending.remove(&id) {
                        finish(&shared, entry, outcome);
                    }
                }
            }
            request = requests.recv(), if !empty && waiting.is_none() && jobs.len() < job_limit => {
                match request {
                    Some(request) => waiting = Some(request),
                    None => { empty = true; shared.close(); }
                }
            }
            credit = shared.error_work_bytes.clone().acquire_many_owned(ERROR_WORK_BYTES as u32), if waiting.is_some() && jobs.len() < job_limit => {
                if let Some(request) = waiting.take() {
                    let Request {input, context, error_credit, pending: entry} = request;
                    if let Ok(materialization_credit) = credit {
                        let store = store.clone();
                        let clock = clock.clone();
                        let identity = entry.identity.clone();
                        let error_allowance = shared.error_allowance;
                        let job = jobs.spawn(async move {
                            let result = store.admit_text(input, clock).await;
                            // Convert while the job's byte credits still own all
                            // results. Never return a complete AdmissionCommit.
                            let outcome = result_outcome(result, identity, error_credit, error_allowance);
                            drop(context);
                            drop(materialization_credit);
                            outcome
                        });
                        pending.insert(job.id(), entry);
                    } else {
                        // This private semaphore is never externally closed;
                        // if it is, no writer has seen this staged request.
                        finish(&shared, entry, AdmissionOutcome {
                            disposition: Disposition::RolledBack,
                            result: Err(AdmissionFailure::ReplyCapacity {class: FailureClass::Capacity}),
                        });
                    }
                }
            }
        }
    }
    let owner = shared.owner();
    ShutdownReport {
        ready: owner
            .queue
            .entries
            .values()
            .filter(|entry| entry.state == State::Ready)
            .count(),
        uncertain: owner.uncertain.len(),
    }
}

fn finish(shared: &Shared, entry: Pending, mut outcome: AdmissionOutcome) {
    let mut owner = shared.owner();
    if matches!(outcome.result, Err(AdmissionFailure::Scheduling)) {
        owner.uncertain.insert(entry.identity.turn_id.clone());
        drop(owner);
        let _ = entry.reply.send(outcome);
        return;
    }
    let transition = match outcome.disposition {
        Disposition::Committed => owner.queue.commit(entry.ticket),
        Disposition::RolledBack => owner.queue.abort(entry.ticket).map(|_| ()),
        Disposition::Unknown => {
            owner.uncertain.insert(entry.identity.turn_id.clone());
            Ok(())
        }
    };
    if transition.is_err() {
        owner.uncertain.insert(entry.identity.turn_id);
        outcome.result = Err(AdmissionFailure::Scheduling);
    }
    drop(owner);
    let _ = entry.reply.send(outcome);
}

fn result_outcome(
    result: Result<AdmissionCommit, StorageError>,
    identity: AcceptedIdentity,
    mut credit: OwnedSemaphorePermit,
    allowance: usize,
) -> AdmissionOutcome {
    match result {
        Ok(commit) => {
            let matches = commit.turn.payload()["id"] == identity.turn_id
                && commit.turn.payload()["session_id"] == identity.session_id
                && commit.turn.payload()["engagement_id"] == identity.project_id
                && commit.session.payload()["id"] == identity.session_id;
            drop(commit); // Release store-owned record credits immediately.
            drop(credit);
            AdmissionOutcome {
                disposition: Disposition::Committed,
                result: if matches {
                    Ok(identity)
                } else {
                    Err(AdmissionFailure::Scheduling)
                },
            }
        }
        Err(error) => {
            let disposition = match error {
                StorageError::Closed | StorageError::Database(_) | StorageError::Io(_) => {
                    Disposition::Unknown
                }
                _ => Disposition::RolledBack,
            };
            let failure = if let Some(bytes) = error_bytes(&error)
                .filter(|bytes| *bytes <= allowance && *bytes <= credit.num_permits())
            {
                if let Some(excess) = credit.split(credit.num_permits() - bytes) {
                    drop(excess);
                }
                AdmissionFailure::Storage(StorageFailure {
                    error: Box::new(error),
                    _credit: credit,
                })
            } else {
                AdmissionFailure::ReplyCapacity {
                    class: classify(&error),
                }
            };
            AdmissionOutcome {
                disposition,
                result: Err(failure),
            }
        }
    }
}

fn classify(error: &StorageError) -> FailureClass {
    match error {
        StorageError::Capacity
        | StorageError::ExecutionResultCapacity
        | StorageError::ReadLimit
        | StorageError::DependencyUnavailable
        | StorageError::TransactionLimit => FailureClass::Capacity,
        StorageError::Conflict
        | StorageError::AlreadyExists(_)
        | StorageError::RevisionConflict { .. }
        | StorageError::SettingsRevisionConflict { .. }
        | StorageError::ExecutionConflict(_) => FailureClass::Conflict,
        StorageError::NotFound | StorageError::RecoveryHookNotFound(_) => FailureClass::Missing,
        StorageError::Record(_)
        | StorageError::WrappedRecord(_)
        | StorageError::RetainedModelValidation(_)
        | StorageError::CorruptEnvelope
        | StorageError::ExecutionInvalidState => FailureClass::Validation,
        StorageError::Closed => FailureClass::Closing,
        StorageError::Database(_) | StorageError::Io(_) => FailureClass::Database,
        _ => FailureClass::Other,
    }
}
fn error_bytes(error: &StorageError) -> Option<usize> {
    let owned = match error {
        StorageError::RetainedModelValidation(report)
        | StorageError::Record(RecordError::ModelValidation(report))
        | StorageError::WrappedRecord(RecordError::ModelValidation(report)) => {
            report.retained_bytes()
        }
        StorageError::RecoveryHookNotFound(text)
        | StorageError::AlreadyExists(text)
        | StorageError::ExecutionConflict(text) => text.capacity(),
        StorageError::SettingsRevisionConflict { expected, .. } => expected.capacity(),
        // These opaque third-party error owners have no trustworthy retained-byte
        // API. Never pretend measuring Debug/Display accounts private contents.
        StorageError::Database(_) | StorageError::Io(_) | StorageError::Json(_) => return None,
        _ => 0,
    };
    owned.checked_add(std::mem::size_of::<StorageError>())
}

fn validate_identity(input: &TextAdmission, work: &Work) -> Result<(), SubmitError> {
    FairQueue::validate_work(work)?;
    if input.messages.is_empty() || input.messages.len() > MAX_INPUT_MESSAGES {
        return Err(SubmitError::InputBytes);
    }
    let turn = input.turn.payload();
    if input.turn.record().kind() != AssistantKind::Turn
        || turn["id"] != work.id
        || turn["session_id"] != work.session_id
        || turn["engagement_id"] != work.project_id
    {
        return Err(SubmitError::Identity);
    }
    match &input.session {
        AdmissionSession::New(session)
            if session.record().kind() != AssistantKind::Session
                || session.payload()["id"] != work.session_id
                || session.payload()["engagement_id"] != work.project_id =>
        {
            return Err(SubmitError::Identity);
        }
        AdmissionSession::Existing { id, .. } if id != &work.session_id => {
            return Err(SubmitError::Identity);
        }
        _ => {}
    }
    if input.messages.iter().any(|message| {
        message.record().kind() != AssistantKind::Message
            || message.payload()["session_id"] != work.session_id
            || message.payload()["engagement_id"] != work.project_id
    }) {
        return Err(SubmitError::Identity);
    }
    Ok(())
}

/// Complete bounded measurement without allocating another encoded payload.
pub fn admission_bytes(input: &TextAdmission, work: &Work) -> Result<usize, SubmitError> {
    input_bytes(input, work, MAX_INPUT_BYTES)
}
fn input_bytes(input: &TextAdmission, work: &Work, allowance: usize) -> Result<usize, SubmitError> {
    if input.messages.is_empty() || input.messages.len() > MAX_INPUT_MESSAGES {
        return Err(SubmitError::InputBytes);
    }
    let mut count = Counter {
        bytes: 256,
        limit: allowance,
    };
    match &input.session {
        AdmissionSession::New(session) => count.record(session)?,
        AdmissionSession::Existing { id, .. } => count.add(id.len() + 16)?,
    }
    count.record(&input.turn)?;
    for message in &input.messages {
        count.record(message)?;
    }
    count.json(&input.settings)?;
    count.json(work)?;
    count.add(input.provider_profile_id.len())?;
    count.add(input.resolved_model.len())?;
    Ok(count.bytes)
}
struct Counter {
    bytes: usize,
    limit: usize,
}
impl Counter {
    fn add(&mut self, bytes: usize) -> Result<(), SubmitError> {
        self.bytes = self
            .bytes
            .checked_add(bytes)
            .ok_or(SubmitError::InputBytes)?;
        if self.bytes > self.limit {
            return Err(SubmitError::InputBytes);
        }
        Ok(())
    }
    fn json(&mut self, value: &impl Serialize) -> Result<(), SubmitError> {
        serde_json::to_writer(self, value).map_err(|_| SubmitError::InputBytes)
    }
    fn record(&mut self, record: &ExecutionRecord) -> Result<(), SubmitError> {
        self.add(record.raw_payload().len())?;
        self.json(record.payload())
    }
}
impl Write for Counter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.add(bytes.len())
            .map_err(|_| io::Error::other("admission byte limit"))?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
