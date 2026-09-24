//! Atomic creation of an empty provider conversation and its goal configuration.
//! Reads validate saved choices only; saving never executes a provider or tool.
use crate::{
    AssistantRecords, Error, Result,
    goal_drafts::{GoalConstructor, GoalDraft, constructed, constructor_bytes},
    subagents::python_string_repr,
};
use nebula_assistant_domain::{
    dependencies::DependencyKind,
    records::{AssistantKind, MAX_RECORD_BYTES, StoredAssistantRecord},
};
use nebula_assistant_storage::entities::{
    ConversationDependencies, DependencyReadBudget, Error as StorageError, Mutation,
};
use serde::{Deserialize, Deserializer, Serialize, de::Error as _};
use serde_json::{Number, Value, value::RawValue};
use std::sync::Arc;

#[derive(Clone, Debug, Serialize)]
pub struct GoalConversationCreate {
    #[serde(flatten)]
    pub draft: GoalDraft,
    pub engagement_id: String,
    pub provider_id: String,
    pub model: String,
    pub tools_enabled: bool,
    pub mcp_server_ids: Vec<String>,
    pub hook_ids: Vec<String>,
    pub reasoning_effort: Option<String>,
    pub allow_subagents: bool,
    pub allow_agent_messaging: bool,
    pub max_active_subagents: Option<u32>,
}
// As in GoalDraftUpdate, keep a lexical, flat wire boundary: Serde's flattened
// Content and Value number paths can round arbitrary precision integer budgets.
#[derive(Deserialize)]
struct Wire {
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
    engagement_id: String,
    provider_id: String,
    model: String,
    #[serde(default)]
    tools_enabled: bool,
    #[serde(default)]
    mcp_server_ids: Vec<String>,
    #[serde(default)]
    hook_ids: Vec<String>,
    #[serde(default)]
    reasoning_effort: Option<String>,
    #[serde(default)]
    allow_subagents: bool,
    #[serde(default)]
    allow_agent_messaging: bool,
    #[serde(default)]
    max_active_subagents: Option<u32>,
}
impl<'de> Deserialize<'de> for GoalConversationCreate {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        let raw = Box::<RawValue>::deserialize(deserializer)?;
        let wire: Wire = serde_json::from_str(raw.get()).map_err(D::Error::custom)?;
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
            engagement_id: wire.engagement_id,
            provider_id: wire.provider_id,
            model: wire.model,
            tools_enabled: wire.tools_enabled,
            mcp_server_ids: wire.mcp_server_ids,
            hook_ids: wire.hook_ids,
            reasoning_effort: wire.reasoning_effort,
            allow_subagents: wire.allow_subagents,
            allow_agent_messaging: wire.allow_agent_messaging,
            max_active_subagents: wire.max_active_subagents,
        })
    }
}
pub struct GoalConversationCreated {
    pub session: StoredAssistantRecord,
    pub goal: StoredAssistantRecord,
}
impl GoalConversationCreated {
    pub fn into_payload(self) -> Value {
        Value::Object(
            [
                ("session".into(), self.session.into_payload()),
                ("goal".into(), self.goal.into_payload()),
            ]
            .into_iter()
            .collect(),
        )
    }
}
#[derive(Serialize)]
struct Choices<'a> {
    tools_enabled: bool,
    mcp_server_ids: &'a [String],
    hook_ids: &'a [String],
    reasoning_effort: &'a Option<String>,
    allow_subagents: bool,
    allow_agent_messaging: bool,
    max_active_subagents: Option<u32>,
    message_count: u8,
    last_sequence: u8,
    initial_title_state: &'static str,
}
#[derive(Serialize)]
struct SessionConstructor<'a> {
    id: &'a str,
    engagement_id: &'a str,
    title: &'a str,
    provider_profile_id: &'a str,
    model: &'a str,
    metadata: Choices<'a>,
}
struct Count(usize);
impl std::io::Write for Count {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > MAX_RECORD_BYTES {
            return Err(std::io::Error::other(
                "conversation response exceeds byte bound",
            ));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
struct Bounded(Vec<u8>);
impl std::io::Write for Bounded {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("conversation exceeds byte bound"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn title(objective: &str) -> String {
    let words = objective.split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c));
    let mut title = String::new();
    let mut count = 0;
    for word in words.filter(|word| !word.is_empty()) {
        if count == 300 {
            break;
        }
        if !title.is_empty() {
            title.push(' ');
            count += 1;
        }
        for ch in word.chars().take(300 - count) {
            title.push(ch);
            count += 1;
        }
    }
    if title.is_empty() {
        "Goal conversation".into()
    } else {
        title
    }
}
fn dependency_error(error: StorageError, kind: DependencyKind, id: &str) -> Error {
    match error {
        StorageError::NotFound => Error::EntityNotFound {
            kind: kind.as_str(),
            id: id.into(),
        },
        StorageError::WrappedRecord(_) => Error::LegacyStorageUnhandled,
        StorageError::DependencyUnavailable => Error::Unavailable(
            "Assistant dependency hydration is busy; retry after capacity becomes available",
        ),
        StorageError::DependencyTimeout => Error::Timeout(
            "Assistant dependency hydration timed out; retry after the host account service responds",
        ),
        error => error.into(),
    }
}
impl AssistantRecords {
    pub async fn create_goal_conversation<F: FnMut() -> String>(
        &self,
        body: GoalConversationCreate,
        environment: &ConversationDependencies,
        mut make_id: F,
    ) -> Result<GoalConversationCreated> {
        let mut budget = DependencyReadBudget::default();
        self.store
            .conversation_dependency(
                DependencyKind::Engagement,
                &body.engagement_id,
                environment,
                Arc::new(self.clock),
                &mut budget,
            )
            .await
            .map_err(|e| dependency_error(e, DependencyKind::Engagement, &body.engagement_id))?;
        let provider = self
            .store
            .conversation_dependency(
                DependencyKind::ProviderProfile,
                &body.provider_id,
                environment,
                Arc::new(self.clock),
                &mut budget,
            )
            .await
            .map_err(|e| dependency_error(e, DependencyKind::ProviderProfile, &body.provider_id))?;
        let p = provider.payload();
        if p["enabled"] == false {
            return Err(Error::Conflict("the selected provider is disabled"));
        }
        let provider_id = p["id"]
            .as_str()
            .ok_or(Error::LegacyStorageUnhandled)?
            .to_owned();
        let models = p["model_allowlist"]
            .as_array()
            .ok_or(Error::LegacyStorageUnhandled)?;
        if !models.is_empty()
            && !models
                .iter()
                .any(|model| model.as_str() == Some(&body.model))
        {
            return Err(Error::DynamicConflict(format!(
                "model {} is not allowed by provider {}",
                python_string_repr(&body.model)?,
                python_string_repr(&provider_id)?
            )));
        }
        drop(provider);
        for id in &body.mcp_server_ids {
            let profile = self
                .store
                .conversation_dependency(
                    DependencyKind::McpServerProfile,
                    id,
                    environment,
                    Arc::new(self.clock),
                    &mut budget,
                )
                .await
                .map_err(|e| dependency_error(e, DependencyKind::McpServerProfile, id))?;
            if profile.payload()["enabled"] == false {
                let name = profile.payload()["name"]
                    .as_str()
                    .ok_or(Error::LegacyStorageUnhandled)?;
                return Err(Error::DynamicConflict(format!(
                    "MCP server {} is disabled and cannot be selected",
                    python_string_repr(name)?
                )));
            }
        }
        let session_id = make_id();
        let title = title(&body.draft.objective);
        let created = (self.clock)();
        let updated = (self.clock)();
        let mut input = Bounded(Vec::new());
        serde_json::to_writer(
            &mut input,
            &SessionConstructor {
                id: &session_id,
                engagement_id: &body.engagement_id,
                title: &title,
                provider_profile_id: &provider_id,
                model: &body.model,
                metadata: Choices {
                    tools_enabled: body.tools_enabled,
                    mcp_server_ids: &body.mcp_server_ids,
                    hook_ids: &body.hook_ids,
                    reasoning_effort: &body.reasoning_effort,
                    allow_subagents: body.allow_subagents,
                    allow_agent_messaging: body.allow_agent_messaging,
                    max_active_subagents: body.max_active_subagents,
                    message_count: 0,
                    last_sequence: 0,
                    initial_title_state: "pending",
                },
            },
        )
        .map_err(|_| StorageError::ReadLimit)?;
        let session = StoredAssistantRecord::decode_created_direct(
            AssistantKind::Session,
            &input.0,
            created,
            updated,
        )
        .map_err(constructed)?;
        drop(input);
        let goal_id = make_id();
        let created = (self.clock)();
        let updated = (self.clock)();
        let input = constructor_bytes(&GoalConstructor {
            id: &goal_id,
            engagement_id: &Value::String(body.engagement_id),
            session_id: &Value::String(session_id),
            draft: &body.draft,
        })?;
        let goal = StoredAssistantRecord::decode_created_direct(
            AssistantKind::Goal,
            &input,
            created,
            updated,
        )
        .map_err(constructed)?;
        drop(input);
        // Reserve the full response envelope before changing durable state.
        let mut response = Count(0);
        #[derive(Serialize)]
        struct Response<'a> {
            session: &'a Value,
            goal: &'a Value,
        }
        serde_json::to_writer(
            &mut response,
            &Response {
                session: session.payload(),
                goal: goal.payload(),
            },
        )
        .map_err(|_| StorageError::ReadLimit)?;

        let mut records = self
            .store
            .apply(vec![Mutation::Create(session), Mutation::Create(goal)])
            .await?
            .into_iter();
        Ok(GoalConversationCreated {
            session: records.next().flatten().ok_or(Error::LegacyUnhandled)?,
            goal: records.next().flatten().ok_or(Error::LegacyUnhandled)?,
        })
    }
}
