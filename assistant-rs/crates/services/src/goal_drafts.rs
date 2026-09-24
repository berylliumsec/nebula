//! Existing-conversation goal configuration only. Saving configuration never
//! reconciles pending work, changes execution ownership, or dispatches a turn.
use crate::{AssistantRecords, Error, Result, context::Revision, plans::active_elapsed_seconds};
use nebula_assistant_domain::{
    records::{AssistantKind as Kind, RecordError, StoredAssistantRecord},
    tool_receipt::exact_integer,
};
use nebula_assistant_storage::entities::{Error as StorageError, Mutation};
use num_bigint::BigInt;
use num_traits::FromPrimitive;
use serde::{Deserialize, Deserializer, Serialize, de::Error as _};
use serde_json::{Map, Number, Value, value::RawValue};
use std::sync::Arc;

/// Transport validates plain BaseModel inputs before calling this service.
/// Preserve raw strings here: entity construction performs its own later trim.
#[derive(Clone, Debug, Serialize)]
pub struct GoalDraft {
    pub objective: String,
    pub completion_criteria: Vec<String>,
    #[serde(default)]
    pub plan: Vec<String>,
    #[serde(default)]
    pub token_budget: Option<Number>,
    #[serde(default)]
    pub time_budget_seconds: Option<Number>,
    #[serde(default)]
    pub step_budget: Option<Number>,
    #[serde(default)]
    pub child_budget: Option<u32>,
}
#[derive(Clone, Debug, Serialize)]
pub struct GoalDraftUpdate {
    #[serde(flatten)]
    pub draft: GoalDraft,
    pub expected_revision: Revision,
}

// Deserialize from lexical JSON, including when the caller provides a Value.
// serde_json's Number deserializer can route a >u128 integer through f64 when
// the float's decimal display happens to match its digits (for example 10^100).
// That changes the integer token into exponent notation and can lose precision.
// RawValue preserves the original number spelling across this boundary. Keep
// the update wire flat so Serde never buffers these numbers through Content.
#[derive(Deserialize)]
struct DraftWire {
    objective: String,
    completion_criteria: Vec<String>,
    #[serde(default)]
    plan: Vec<String>,
    #[serde(default)]
    token_budget: Option<Number>,
    #[serde(default)]
    time_budget_seconds: Option<Number>,
    #[serde(default)]
    step_budget: Option<Number>,
    #[serde(default)]
    child_budget: Option<u32>,
}
#[derive(Deserialize)]
struct UpdateWire {
    objective: String,
    completion_criteria: Vec<String>,
    #[serde(default)]
    plan: Vec<String>,
    #[serde(default)]
    token_budget: Option<Number>,
    #[serde(default)]
    time_budget_seconds: Option<Number>,
    #[serde(default)]
    step_budget: Option<Number>,
    #[serde(default)]
    child_budget: Option<u32>,
    expected_revision: Revision,
}
impl<'de> Deserialize<'de> for GoalDraft {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let raw = Box::<RawValue>::deserialize(deserializer)?;
        let wire: DraftWire = serde_json::from_str(raw.get()).map_err(D::Error::custom)?;
        Ok(Self {
            objective: wire.objective,
            completion_criteria: wire.completion_criteria,
            plan: wire.plan,
            token_budget: wire.token_budget,
            time_budget_seconds: wire.time_budget_seconds,
            step_budget: wire.step_budget,
            child_budget: wire.child_budget,
        })
    }
}
impl<'de> Deserialize<'de> for GoalDraftUpdate {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let raw = Box::<RawValue>::deserialize(deserializer)?;
        let wire: UpdateWire = serde_json::from_str(raw.get()).map_err(D::Error::custom)?;
        Ok(Self {
            draft: GoalDraft {
                objective: wire.objective,
                completion_criteria: wire.completion_criteria,
                plan: wire.plan,
                token_budget: wire.token_budget,
                time_budget_seconds: wire.time_budget_seconds,
                step_budget: wire.step_budget,
                child_budget: wire.child_budget,
            },
            expected_revision: wire.expected_revision,
        })
    }
}
impl GoalDraft {
    fn fields(self) -> Result<Map<String, Value>> {
        match serde_json::to_value(self)? {
            Value::Object(fields) => Ok(fields),
            _ => Err(Error::LegacyUnhandled),
        }
    }
}
#[derive(Serialize)]
struct GoalConstructor<'a> {
    id: &'a str,
    engagement_id: &'a Value,
    session_id: &'a Value,
    #[serde(flatten)]
    draft: &'a GoalDraft,
}
fn constructor_bytes(value: &GoalConstructor<'_>) -> Result<Vec<u8>> {
    struct Bounded(Vec<u8>);
    impl std::io::Write for Bounded {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if bytes.len()
                > nebula_assistant_domain::records::MAX_RECORD_BYTES.saturating_sub(self.0.len())
            {
                return Err(std::io::Error::other(
                    "Goal constructor exceeds record limit",
                ));
            }
            self.0.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut out = Bounded(Vec::new());
    serde_json::to_writer(&mut out, value).map_err(|_| StorageError::ReadLimit)?;
    Ok(out.0)
}
fn missing(kind: Kind, id: &str) -> Error {
    Error::EntityNotFound {
        kind: kind.as_str(),
        id: id.into(),
    }
}
fn identity(session: &str) -> Result<()> {
    if session.is_empty() || session.chars().count() > 200 {
        Err(missing(Kind::Session, session))
    } else {
        Ok(())
    }
}
fn storage_error(error: StorageError, kind: Kind, id: &str) -> Error {
    match error {
        StorageError::NotFound => missing(kind, id),
        StorageError::SettingsRevisionConflict { expected, found } => {
            Error::RevisionConflict { expected, found }
        }
        StorageError::RetainedModelValidation(report) => Error::RetainedModelValidation(report),
        StorageError::WrappedRecord(_) => Error::LegacyStorageUnhandled,
        StorageError::Record(RecordError::TooLarge) => StorageError::ReadLimit.into(),
        StorageError::Record(
            RecordError::Shape(_) | RecordError::Invariant(_) | RecordError::ModelValidation(_),
        ) => Error::LegacyStorageUnhandled,
        error => error.into(),
    }
}
fn constructed(error: RecordError) -> Error {
    match error {
        RecordError::ModelValidation(report) => Error::RetainedModelValidation(report),
        RecordError::TooLarge => StorageError::ReadLimit.into(),
        error => error.into(),
    }
}
fn integer(value: &Value) -> Result<BigInt> {
    exact_integer(value).ok_or(Error::LegacyUnhandled)
}
fn budget_integer(number: &Number) -> Result<BigInt> {
    integer(&Value::Number(number.clone()))
}
fn below_integer(budget: &Number, used: &Value) -> Result<bool> {
    Ok(budget_integer(budget)? < integer(used)?)
}
fn below_elapsed(budget: &Number, elapsed: f64) -> Result<bool> {
    // Python compares arbitrary integers with a float without rounding the
    // integer to f64. The float's floor is itself an exact representable integer.
    if elapsed == f64::INFINITY {
        return Ok(true);
    }
    if !elapsed.is_finite() {
        return Err(Error::LegacyUnhandled);
    }
    let whole = BigInt::from_f64(elapsed.floor()).ok_or(Error::LegacyUnhandled)?;
    let budget = budget_integer(budget)?;
    Ok(if elapsed.fract() == 0.0 {
        budget < whole
    } else {
        budget <= whole
    })
}
impl AssistantRecords {
    async fn retained_goal(&self, session: &str) -> Result<StoredAssistantRecord> {
        identity(session)?;
        let snapshot = self
            .store
            .session_plans_snapshot(Kind::Goal, session)
            .await
            .map_err(|error| storage_error(error, Kind::Session, session))?;
        if snapshot.records.len() > 1 {
            return Err(Error::Conflict(
                "conversation has more than one authoritative goal",
            ));
        }
        snapshot.records.into_iter().next().ok_or_else(|| {
            Error::RetainedNotFound(format!("chat goal not found for session: {session}"))
        })
    }

    pub async fn create_goal<F: FnOnce() -> String>(
        &self,
        session: &str,
        draft: GoalDraft,
        make_id: F,
    ) -> Result<StoredAssistantRecord> {
        identity(session)?;
        let first = self
            .store
            .get(Kind::Session, session)
            .await
            .map_err(|error| storage_error(error, Kind::Session, session))?;
        if first.payload()["backend"] != "provider" {
            return Err(Error::Conflict(
                "provider-backed goals require a provider conversation",
            ));
        }
        // Preserve the source second Session read and complete goal validation.
        // Its absence check is separate from insert; no singleton claim is made.
        match self.retained_goal(session).await {
            Ok(_) => return Err(Error::Conflict("conversation already has a goal")),
            Err(Error::EntityNotFound { .. } | Error::RetainedNotFound(_)) => {}
            Err(error) => return Err(error),
        }
        let id = make_id();
        let created_at = (self.clock)();
        let updated_at = (self.clock)();
        // Preserve explicit Python kwargs order and omit factory-backed fields
        // from original-input diagnostics, even when the second clock is stale.
        let input = constructor_bytes(&GoalConstructor {
            id: &id,
            engagement_id: &first.payload()["engagement_id"],
            session_id: &first.payload()["id"],
            draft: &draft,
        })?;
        let goal = StoredAssistantRecord::decode_created_direct(
            Kind::Goal,
            &input,
            created_at,
            updated_at,
        )
        .map_err(constructed)?;
        self.store
            .apply(vec![Mutation::Create(goal)])
            .await?
            .into_iter()
            .next()
            .flatten()
            .ok_or(Error::LegacyUnhandled)
    }

    pub async fn update_goal(
        &self,
        session: &str,
        write: GoalDraftUpdate,
    ) -> Result<StoredAssistantRecord> {
        let goal = self.retained_goal(session).await?;
        let p = goal.payload();
        if !write.expected_revision.matches(&p["revision"]) {
            return Err(Error::Conflict(
                "goal changed on another device; review the latest goal before retrying",
            ));
        }
        if p["status"] == "completed" || p["status"] == "cancelled" {
            return Err(Error::Conflict(
                "completed or cancelled goals cannot be edited",
            ));
        }
        if let Some(budget) = &write.draft.step_budget
            && below_integer(budget, &p["current_step"])?
        {
            return Err(Error::Conflict(
                "step budget cannot be lower than completed steps",
            ));
        }
        if let Some(budget) = write.draft.child_budget
            && BigInt::from(budget) < integer(&p["children_started"])?
        {
            return Err(Error::Conflict(
                "child budget cannot be lower than children already started",
            ));
        }
        if let Some(budget) = &write.draft.time_budget_seconds {
            let elapsed = active_elapsed_seconds(p, (self.clock)())?;
            if below_elapsed(budget, elapsed)? {
                return Err(Error::Conflict(
                    "time budget cannot be lower than time already used",
                ));
            }
        }
        if let Some(budget) = &write.draft.token_budget
            && below_integer(budget, &p["usage"]["total_tokens"])?
        {
            return Err(Error::Conflict(
                "token budget cannot be lower than tokens already used",
            ));
        }
        let id = p["id"].as_str().ok_or(Error::LegacyUnhandled)?;
        let expected = p["revision"]
            .as_number()
            .ok_or(Error::LegacyUnhandled)?
            .to_string();
        self.store
            .patch_goal_config(id, expected, write.draft.fields()?, Arc::new(self.clock))
            .await
            .map_err(|error| storage_error(error, Kind::Goal, id))
    }
}
