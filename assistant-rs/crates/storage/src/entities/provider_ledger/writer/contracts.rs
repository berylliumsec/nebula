use super::{Error, Result, canonical::CanonicalJson};
use nebula_assistant_domain::provider_stream;
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::{fmt, sync::Arc};

pub const MAX_CONTROL_RECEIPTS: i64 = 8;
pub const MAX_CONTROL_RECEIPT_BYTES: usize = 64 * 1024;
pub const CONTROL_FLOOR: i64 =
    (2 * provider_stream::MAX_FRAME_BYTES + 8 * MAX_CONTROL_RECEIPT_BYTES + 64 * 1024) as i64;
pub const ROW_OVERHEAD: usize = 256;
pub const MAX_WATCHES: usize = 256;

#[derive(Clone, Copy, PartialEq, Eq)]
pub struct Digest(pub [u8; 32]);
impl Digest {
    pub const ZERO: Self = Self([0; 32]);
    pub fn hex(self) -> String {
        self.0.iter().map(|byte| format!("{byte:02x}")).collect()
    }
    pub fn parse(value: &str) -> Result<Self> {
        if value.len() != 64
            || !value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(Error::Corrupt);
        }
        let mut bytes = [0; 32];
        for (index, byte) in bytes.iter_mut().enumerate() {
            *byte = u8::from_str_radix(&value[index * 2..index * 2 + 2], 16)
                .map_err(|_| Error::Corrupt)?;
        }
        Ok(Self(bytes))
    }
}
impl fmt::Debug for Digest {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.hex())
    }
}
impl<'de> Deserialize<'de> for Digest {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        let text = String::deserialize(d)?;
        Self::parse(&text).map_err(|_| serde::de::Error::custom("invalid digest"))
    }
}
impl Serialize for Digest {
    fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.serialize_str(&self.hex())
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct StreamFence {
    pub turn_id: String,
    pub stream_id: String,
    pub session_id: String,
    pub project_id: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct AttemptFence {
    pub attempt_id: String,
    pub ordinal: i64,
    pub owner_id: String,
    pub claim_id: String,
    pub request_sha256: Digest,
    pub route_sha256: Digest,
}
impl StreamFence {
    pub(crate) fn validate(&self) -> Result<()> {
        for id in [
            &self.turn_id,
            &self.stream_id,
            &self.session_id,
            &self.project_id,
        ] {
            valid_id(id)?;
        }
        Ok(())
    }
}
impl AttemptFence {
    pub(crate) fn validate(&self) -> Result<()> {
        for id in [&self.attempt_id, &self.owner_id, &self.claim_id] {
            valid_id(id)?;
        }
        if self.ordinal < 1 {
            return Err(Error::Invalid);
        }
        Ok(())
    }
}
pub(crate) fn valid_id(id: &str) -> Result<()> {
    if id.is_empty() || id.chars().count() > 200 || id.contains('\0') {
        Err(Error::Invalid)
    } else {
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecordProof {
    pub id: String,
    pub revision: String,
    pub mutation_epoch: i64,
    pub raw_sha256: Digest,
}
impl RecordProof {
    fn validate(&self) -> Result<()> {
        valid_id(&self.id)?;
        if self.revision.len() > 4300
            || self.revision.is_empty()
            || self.revision.starts_with('0')
            || !self.revision.bytes().all(|b| b.is_ascii_digit())
            || self.mutation_epoch < 0
        {
            return Err(Error::Invalid);
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryClass {
    AdmittedNoDispatch,
    ClaimedUncertain,
    SavedAnswer,
    CompletedClaimRetained,
    Interrupted,
    Terminal,
    UnknownLineage,
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureReason {
    ProviderFailure,
    OutputLimit,
    Stopped,
    Shutdown,
    Restart,
    LineageChanged,
    ConversationDeleted,
    OperatorRetention,
}
/// Control receipts contain identity evidence, never transcript text or an arbitrary JSON map.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ControlReceipt {
    AnswerSaved {
        turn: RecordProof,
        session: RecordProof,
        answer: RecordProof,
    },
    Completed {
        turn: RecordProof,
        answer_id: String,
        answer_sha256: Digest,
    },
    Released {
        turn: RecordProof,
        settlement_receipt: Digest,
    },
    Cancelled {
        turn: RecordProof,
        note: Option<RecordProof>,
        reason: FailureReason,
    },
    Failed {
        turn: RecordProof,
        note: Option<RecordProof>,
        reason: FailureReason,
    },
    RecoveryClassified {
        turn: RecordProof,
        classification: RecoveryClass,
        evidence_receipt: Option<Digest>,
    },
    LineageInvalidated {
        turn_id: String,
        observed_epoch: i64,
        previous_receipt: Digest,
        reason: FailureReason,
    },
    RetentionBoundary {
        turn_id: String,
        first_retained_sequence: u64,
        reason: FailureReason,
    },
}
impl ControlReceipt {
    pub(crate) fn kind(&self) -> &'static str {
        match self {
            Self::AnswerSaved { .. } => "answer_saved",
            Self::Completed { .. } => "completed",
            Self::Released { .. } => "released",
            Self::Cancelled { .. } => "cancelled",
            Self::Failed { .. } => "failed",
            Self::RecoveryClassified { .. } => "recovery_classified",
            Self::LineageInvalidated { .. } => "lineage_invalidated",
            Self::RetentionBoundary { .. } => "retention_boundary",
        }
    }
    pub(crate) fn canonical(
        &self,
        scope: &StreamFence,
        expected_epoch: i64,
    ) -> Result<CanonicalJson> {
        let turn = match self {
            Self::AnswerSaved {
                turn,
                session,
                answer,
            } => {
                session.validate()?;
                answer.validate()?;
                if session.id != scope.session_id {
                    return Err(Error::Conflict);
                }
                Some(turn)
            }
            Self::Completed {
                turn, answer_id, ..
            } => {
                valid_id(answer_id)?;
                Some(turn)
            }
            Self::Released { turn, .. } | Self::RecoveryClassified { turn, .. } => Some(turn),
            Self::Cancelled { turn, note, .. } | Self::Failed { turn, note, .. } => {
                if let Some(note) = note {
                    note.validate()?;
                }
                Some(turn)
            }
            Self::LineageInvalidated {
                turn_id,
                observed_epoch,
                ..
            } => {
                if turn_id != &scope.turn_id || *observed_epoch != expected_epoch {
                    return Err(Error::Conflict);
                }
                None
            }
            Self::RetentionBoundary {
                turn_id,
                first_retained_sequence,
                ..
            } => {
                if turn_id != &scope.turn_id
                    || *first_retained_sequence == 0
                    || *first_retained_sequence > provider_stream::MAX_SEQUENCE + 1
                {
                    return Err(Error::Invalid);
                }
                None
            }
        };
        if let Some(turn) = turn {
            turn.validate()?;
            if turn.id != scope.turn_id || turn.mutation_epoch != expected_epoch {
                return Err(Error::Conflict);
            }
        }
        let json = CanonicalJson::from_value(self)?;
        if json.bytes().len() > MAX_CONTROL_RECEIPT_BYTES {
            return Err(Error::Capacity);
        }
        Ok(json)
    }
}
#[derive(Clone, Copy, Debug)]
pub enum OrdinaryReceiptKind {
    Admitted,
    Claimed,
    DispatchIntent,
}
impl OrdinaryReceiptKind {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::Admitted => "admitted",
            Self::Claimed => "claimed",
            Self::DispatchIntent => "dispatch_intent",
        }
    }
}
pub enum ReceiptPayload {
    Ordinary {
        kind: OrdinaryReceiptKind,
        payload: CanonicalJson,
    },
    Control(Box<ControlReceipt>),
}
pub struct ReceiptCommand {
    pub key: String,
    pub turn_epoch: i64,
    pub command: CanonicalJson,
    pub payload: ReceiptPayload,
}
#[derive(Clone, Copy, Debug)]
pub struct ReceiptReference {
    pub sequence: i64,
    pub hash: Digest,
}
pub struct EventCommand {
    pub key: String,
    pub settlement: Option<ReceiptReference>,
    pub turn_epoch: i64,
    pub draft: provider_stream::Draft,
}

#[derive(Clone, Debug)]
pub struct WatchedEntity {
    pub id: String,
    pub epoch: i64,
    pub rowid: Option<i64>,
}
#[derive(Clone, Debug)]
pub struct StreamState {
    pub scope: StreamFence,
    pub state: String,
    pub last_sequence: u64,
    pub first_retained_sequence: u64,
    pub receipt_sequence: i64,
    pub receipt_hash: Digest,
    pub reserved_bytes: i64,
    pub terminal_sequence: Option<u64>,
}
/// An append result inside an uncommitted transaction. Only the outer owner can establish commit.
#[derive(Clone, Debug)]
pub struct StagedReceipt {
    pub sequence: i64,
    pub receipt_hash: Digest,
    pub duplicate: bool,
}
pub struct RetainedEvent {
    pub sequence: u64,
    pub settlement: Option<ReceiptReference>,
    pub event_type: String,
    pub event_key: String,
    pub attempt_id: Option<String>,
    pub json: Vec<u8>,
    pub sse: Vec<u8>,
    pub content_hash: Digest,
    pub previous_hash: Digest,
    pub event_hash: Digest,
    pub committed_at_us: i64,
}
impl fmt::Debug for RetainedEvent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RetainedProviderEvent")
            .field("sequence", &self.sequence)
            .field("bytes", &self.retained_bytes())
            .finish()
    }
}
impl RetainedEvent {
    pub fn retained_bytes(&self) -> usize {
        ROW_OVERHEAD
            + self.event_type.len()
            + self.event_key.len()
            + self.attempt_id.as_ref().map_or(0, String::len)
            + self.json.len()
            + self.sse.len()
    }
}
#[derive(Clone, Debug)]
pub struct StagedEvent {
    pub event: Arc<RetainedEvent>,
    pub duplicate: bool,
}
#[derive(Clone, Copy, Debug)]
pub enum ReplayCursor {
    After(u64),
    BeyondHead,
}
impl ReplayCursor {
    pub fn decimal(value: &str) -> Result<Self> {
        if value.is_empty() || value.len() > 4300 || !value.bytes().all(|b| b.is_ascii_digit()) {
            return Err(Error::Invalid);
        }
        let significant = value.trim_start_matches('0');
        if significant.len() > 16 {
            return Ok(Self::BeyondHead);
        }
        let value = if significant.is_empty() {
            0
        } else {
            significant.parse().map_err(|_| Error::Invalid)?
        };
        if value > provider_stream::MAX_SEQUENCE {
            Ok(Self::BeyondHead)
        } else {
            Ok(Self::After(value))
        }
    }
}
#[derive(Clone, Copy, Debug)]
pub struct PageLimits {
    pub events: u32,
    pub bytes: usize,
}
impl Default for PageLimits {
    fn default() -> Self {
        Self {
            events: 256,
            bytes: 4 * 1024 * 1024,
        }
    }
}
pub struct ReplayPage {
    pub events: Vec<Arc<RetainedEvent>>,
    pub next: ReplayCursor,
    pub has_more: bool,
    pub stream: StreamState,
    pub retained_bytes: usize,
}
