//! Immutable shared records consumed by Assistant retained read projections.
//! These contracts grant no mutation, approval, dispatch, or execution capability.
use crate::records::{MAX_RECORD_BYTES, RecordError, fill_defaults, require_canonical_fields};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Utc};
use jsonschema::Validator;
use serde_json::Value;
use std::{collections::HashMap, sync::LazyLock};
mod profiles;

/// Trusted passive context. Implementations must not resolve credentials or
/// contact providers; expansion returns the lexical home for one `~` component.
pub trait DependencyEnvironment {
    fn now(&mut self) -> DateTime<Utc>;
    fn expand_user(&mut self, first_component: &str) -> Result<String, RecordError>;
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Hash)]
pub enum DependencyKind {
    Approval,
    HarnessInteraction,
    HarnessTurn,
    ToolCall,
    Artifact,
    NativeHookExecution,
    HarnessProfile,
    McpServerProfile,
    Engagement,
    ProviderProfile,
    HarnessSession,
}
impl DependencyKind {
    pub const ALL: [Self; 11] = [
        Self::Approval,
        Self::HarnessInteraction,
        Self::HarnessTurn,
        Self::ToolCall,
        Self::Artifact,
        Self::NativeHookExecution,
        Self::HarnessProfile,
        Self::McpServerProfile,
        Self::Engagement,
        Self::ProviderProfile,
        Self::HarnessSession,
    ];
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Approval => "approvals",
            Self::HarnessInteraction => "harness_interactions",
            Self::HarnessTurn => "harness_turns",
            Self::ToolCall => "tool_calls",
            Self::Artifact => "artifacts",
            Self::NativeHookExecution => "native_hook_executions",
            Self::HarnessProfile => "harnesses",
            Self::McpServerProfile => "mcp_servers",
            Self::Engagement => "engagements",
            Self::ProviderProfile => "providers",
            Self::HarnessSession => "harness_sessions",
        }
    }
}
impl TryFrom<&str> for DependencyKind {
    type Error = RecordError;
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        Self::ALL
            .into_iter()
            .find(|kind| kind.as_str() == value)
            .ok_or(RecordError::UnknownKind)
    }
}
static SCHEMAS: LazyLock<Result<Value, RecordError>> = LazyLock::new(|| {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-catchup.json"))
            .map_err(|_| RecordError::Schema)?;
    let results: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-results.json"))
            .map_err(|_| RecordError::Schema)?;
    let mut schemas = fixture["dependency_schemas"].clone();
    for kind in [DependencyKind::ToolCall, DependencyKind::Artifact] {
        schemas[kind.as_str()] = results["dependency_schemas"][kind.as_str()].clone();
    }
    let status: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-status.json"))
            .map_err(|_| RecordError::Schema)?;
    schemas[DependencyKind::NativeHookExecution.as_str()] =
        status["dependency_schemas"][DependencyKind::NativeHookExecution.as_str()].clone();
    let state: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-state.json"))
            .map_err(|_| RecordError::Schema)?;
    schemas[DependencyKind::HarnessProfile.as_str()] =
        state["dependency_schemas"][DependencyKind::HarnessProfile.as_str()].clone();
    let settings: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-settings.json"))
            .map_err(|_| RecordError::Schema)?;
    schemas[DependencyKind::McpServerProfile.as_str()] =
        settings["dependency_schemas"][DependencyKind::McpServerProfile.as_str()].clone();
    let conversations: Value = serde_json::from_str(include_str!(
        "../../../compatibility/python-goal-conversations.json"
    ))
    .map_err(|_| RecordError::Schema)?;
    for kind in [DependencyKind::Engagement, DependencyKind::ProviderProfile] {
        schemas[kind.as_str()] = conversations["dependency_schemas"][kind.as_str()].clone();
    }
    let forks: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-forks.json"))
            .map_err(|_| RecordError::Schema)?;
    schemas[DependencyKind::HarnessSession.as_str()] =
        forks["dependency_schemas"][DependencyKind::HarnessSession.as_str()].clone();
    Ok(schemas)
});
static VALIDATORS: LazyLock<Result<HashMap<DependencyKind, Validator>, RecordError>> =
    LazyLock::new(|| {
        DependencyKind::ALL
            .into_iter()
            .map(|kind| {
                let mut schema =
                    SCHEMAS.as_ref().map_err(|_| RecordError::Schema)?[kind.as_str()].clone();
                require_canonical_fields(&mut schema);
                if kind == DependencyKind::Engagement {
                    // Pydantic applies this bound before Path.expanduser; a
                    // trusted expanded home may make the saved path longer.
                    if let Some(branches) =
                        schema["properties"]["workspace_path"]["anyOf"].as_array_mut()
                    {
                        for branch in branches {
                            if let Some(object) = branch.as_object_mut() {
                                object.remove("maxLength");
                            }
                        }
                    }
                }
                jsonschema::draft202012::options()
                    .with_format("date-time", recorded_datetime)
                    .should_validate_formats(true)
                    .build(&schema)
                    .map(|validator| (kind, validator))
                    .map_err(|_| RecordError::Schema)
            })
            .collect()
    });

#[derive(Clone, PartialEq)]
pub struct StoredDependency {
    kind: DependencyKind,
    payload: Value,
}
impl std::fmt::Debug for StoredDependency {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StoredDependency")
            .field("kind", &self.kind)
            .finish_non_exhaustive()
    }
}
impl StoredDependency {
    /// Narrow trusted constructor for a retained harness fork; no adapter or
    /// provider capability is created by this immutable record value.
    pub fn decode_harness_session_created(
        bytes: &[u8],
        defaults: &crate::model_validation::CreatedEntityDefaults,
    ) -> Result<Self, RecordError> {
        let payload = crate::model_validation::hydrate_fork_created(
            crate::model_validation::Model::HarnessSession,
            bytes,
            defaults,
            &[],
        )?;
        Self::decode(
            DependencyKind::HarnessSession,
            &serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?,
        )
    }
    pub fn decode(kind: DependencyKind, bytes: &[u8]) -> Result<Self, RecordError> {
        Self::decode_context(kind, bytes, None)
    }
    pub fn decode_with_environment(
        kind: DependencyKind,
        bytes: &[u8],
        environment: &mut dyn DependencyEnvironment,
    ) -> Result<Self, RecordError> {
        Self::decode_context(kind, bytes, Some(environment))
    }
    fn decode_context(
        kind: DependencyKind,
        bytes: &[u8],
        environment: Option<&mut dyn DependencyEnvironment>,
    ) -> Result<Self, RecordError> {
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        let mut p: Value = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
        for field in ["id", "revision", "created_at", "updated_at"] {
            if p.get(field).is_none() {
                return Err(RecordError::Shape(kind.as_str()));
            }
        }
        if kind == DependencyKind::Approval
            && (p.get("requested_at").is_none()
                || p.get("continuation")
                    .is_some_and(|v| !v.is_null() && v.get("updated_at").is_none()))
        {
            return Err(RecordError::Shape(kind.as_str()));
        }
        if kind == DependencyKind::NativeHookExecution
            && p.get("late_outcome")
                .is_some_and(|v| !v.is_null() && v.get("observed_at").is_none())
        {
            // An observed process result must keep its recorded observation
            // time; reading history must not invent one from the current clock.
            return Err(RecordError::Shape(kind.as_str()));
        }
        let schema = &SCHEMAS.as_ref().map_err(|_| RecordError::Schema)?[kind.as_str()];
        if kind == DependencyKind::McpServerProfile {
            p = crate::mcp_profile::hydrate(schema, bytes)?;
        } else if matches!(
            kind,
            DependencyKind::Engagement | DependencyKind::ProviderProfile
        ) {
            p = profiles::hydrate(schema, bytes, environment)?;
        } else if kind == DependencyKind::HarnessSession {
            let hydrated = match environment {
                Some(environment) => {
                    crate::model_validation::hydrate_harness_session(bytes, environment)
                }
                None => crate::model_validation::hydrate(
                    crate::model_validation::Model::HarnessSession,
                    crate::model_validation::InputOrigin::RetainedJson,
                    bytes,
                ),
            };
            p = hydrated.map_err(|error| match error {
                RecordError::ModelValidation(_) => RecordError::Shape("harness_sessions"),
                error => error,
            })?;
        }
        if !matches!(
            kind,
            DependencyKind::Engagement
                | DependencyKind::ProviderProfile
                | DependencyKind::HarnessSession
        ) {
            // Contextual codecs already fill and validate every model field.
            // Re-normalizing an expanded workspace path would strip characters
            // introduced by trusted home expansion after the source validator.
            fill_defaults(schema, schema, &mut p);
        }
        if !VALIDATORS.as_ref().map_err(|_| RecordError::Schema)?[&kind].is_valid(&p)
            || (!matches!(
                kind,
                DependencyKind::Engagement
                    | DependencyKind::ProviderProfile
                    | DependencyKind::HarnessSession
            ) && p["revision"].as_i64().is_none_or(|r| r < 1))
        {
            return Err(RecordError::Shape(kind.as_str()));
        }
        let created = DateTime::parse_from_rfc3339(p["created_at"].as_str().unwrap_or_default())
            .map_err(|_| RecordError::Invariant("dependency creation timestamp must be aware"))?;
        let updated = DateTime::parse_from_rfc3339(p["updated_at"].as_str().unwrap_or_default())
            .map_err(|_| RecordError::Invariant("dependency update timestamp must be aware"))?;
        if updated < created {
            return Err(RecordError::Invariant(
                "dependency update predates creation",
            ));
        }
        // Python Entity normalizes only these base timestamps to UTC. Optional
        // dependency timestamps retain their own offsets and semantics.
        for (field, time) in [("created_at", created), ("updated_at", updated)] {
            p[field] = time
                .with_timezone(&Utc)
                .to_rfc3339_opts(
                    if time.timestamp_subsec_micros() == 0 {
                        SecondsFormat::Secs
                    } else {
                        SecondsFormat::Micros
                    },
                    true,
                )
                .into();
        }
        for field in [
            "requested_at",
            "decided_at",
            "expires_at",
            "started_at",
            "completed_at",
            "resolved_at",
        ] {
            if let Some(value) = p.get(field).filter(|v| !v.is_null()) {
                timestamp(value)?;
            }
        }
        if !p["continuation"].is_null() {
            timestamp(&p["continuation"]["updated_at"])?;
        }
        let truthy = |field: &str| p[field].as_str().is_some_and(|v| !v.is_empty());
        match kind {
            DependencyKind::HarnessProfile => crate::harness_profile::validate(&mut p)?,
            DependencyKind::McpServerProfile => crate::mcp_profile::validate(&p)?,
            DependencyKind::NativeHookExecution => {
                if (p["status"] != "running") == p["completed_at"].is_null() {
                    return Err(RecordError::Invariant(
                        "hook completion timestamp must match terminal status",
                    ));
                }
                if !p["late_outcome"].is_null() {
                    timestamp(&p["late_outcome"]["observed_at"])?;
                }
            }
            DependencyKind::HarnessTurn => {
                let coherent = match p["origin"].as_str() {
                    Some("chat") => {
                        truthy("chat_session_id") && truthy("chat_turn_id") && !truthy("run_id")
                    }
                    Some("mission") => truthy("run_id") && !truthy("chat_turn_id"),
                    Some("analysis") => {
                        !truthy("chat_session_id") && !truthy("chat_turn_id") && !truthy("run_id")
                    }
                    _ => false,
                };
                if !coherent {
                    return Err(RecordError::Invariant(
                        "harness turn origin bindings disagree",
                    ));
                }
            }
            DependencyKind::HarnessInteraction => {
                let coherent = if p["origin"] == "chat" {
                    truthy("chat_session_id") && p["run_id"].is_null()
                } else {
                    truthy("run_id") && p["chat_session_id"].is_null()
                };
                if !coherent
                    || (p["status"] != "pending") == p["resolved_at"].is_null()
                    || p["contains_secret"] == true && !p["response"].is_null()
                {
                    return Err(RecordError::Invariant(
                        "harness interaction ownership or resolution disagrees",
                    ));
                }
            }
            DependencyKind::Approval
            | DependencyKind::ToolCall
            | DependencyKind::Artifact
            | DependencyKind::Engagement
            | DependencyKind::ProviderProfile => {}
            DependencyKind::HarnessSession => {}
        }
        Ok(Self { kind, payload: p })
    }
    pub fn kind(&self) -> DependencyKind {
        self.kind
    }
    pub fn payload(&self) -> &Value {
        &self.payload
    }
}
fn timestamp(value: &Value) -> Result<(), RecordError> {
    let text = value.as_str().unwrap_or_default();
    if recorded_datetime(text) {
        Ok(())
    } else {
        Err(RecordError::Invariant("dependency timestamp is invalid"))
    }
}
fn recorded_datetime(text: &str) -> bool {
    DateTime::parse_from_rfc3339(text).is_ok()
        || NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f").is_ok()
}
