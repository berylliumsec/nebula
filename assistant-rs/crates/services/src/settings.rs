//! Saved conversation choices and schedule configuration. These writes never
//! start a schedule, provider, MCP client, hook, or subagent.
use crate::{AssistantRecords, Error, Result, context::Revision, timestamp};
use chrono::Duration;
use nebula_assistant_domain::{
    records::{AssistantKind as Kind, RecordError, StoredAssistantRecord},
    tool_receipt::truthy,
};
use nebula_assistant_storage::entities::{Error as StorageError, Mutation, RawSession};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json, value::RawValue};
use std::{collections::HashMap, sync::Arc};

/// Transport has applied ChatSessionUpdateRequest coercion and validators.
/// Presence is retained: null reasoning/limit changes differ from omission.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(transparent)]
pub struct SettingsWrite {
    pub fields: Map<String, Value>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ScheduleCreate {
    pub interval_seconds: i64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ScheduleWrite {
    pub expected_revision: Revision,
    #[serde(default)]
    pub enabled: Option<bool>,
}
const ARCHIVED_SKIP_REASON: &str = "Conversation is archived; unarchive it to resume the schedule.";

fn missing(kind: Kind, id: &str) -> Error {
    Error::EntityNotFound {
        kind: kind.as_str(),
        id: id.into(),
    }
}
fn identity(id: &str) -> Result<()> {
    if id.is_empty() || id.chars().count() > 200 {
        Err(missing(Kind::Session, id))
    } else {
        Ok(())
    }
}
fn storage_error(error: StorageError, kind: Kind, id: &str) -> Error {
    match error {
        StorageError::RetainedModelValidation(report) => Error::RetainedModelValidation(report),
        StorageError::WrappedRecord(_) => Error::LegacyStorageUnhandled,
        StorageError::NotFound => missing(kind, id),
        StorageError::SettingsRevisionConflict { expected, found } => {
            Error::RevisionConflict { expected, found }
        }
        StorageError::Record(RecordError::TooLarge) => StorageError::ReadLimit.into(),
        StorageError::Record(_) | StorageError::CorruptEnvelope => Error::LegacyStorageUnhandled,
        error => error.into(),
    }
}
fn optional<'a>(fields: &'a Map<String, Value>, key: &str) -> Option<&'a Value> {
    fields.get(key).filter(|value| !value.is_null())
}
fn revision(p: &Value) -> Result<String> {
    p["revision"]
        .as_number()
        .map(ToString::to_string)
        .ok_or(Error::LegacyUnhandled)
}
fn changes(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(fields) => fields,
        _ => unreachable!("literal object"),
    }
}
fn absent(error: &Error) -> bool {
    matches!(
        error,
        Error::EntityNotFound { .. } | Error::RetainedNotFound(_)
    )
}
fn saved_get<'a>(saved: &'a Value, key: &str) -> Result<Option<&'a Value>> {
    // Python's `saved = value or {}` permits falsey scalars/containers, while a
    // truthy non-dictionary raises only if this particular get is evaluated.
    if !truthy(saved) {
        return Ok(None);
    }
    Ok(saved.as_object().ok_or(Error::LegacyUnhandled)?.get(key))
}
fn saved_text(raw: &str, saved: &Value, key: &str) -> Result<String> {
    let Some(value) = saved_get(saved, key)?.filter(|v| truthy(v)) else {
        return Ok(String::new());
    };
    if let Some(text) = value.as_str() {
        return Ok(text.into());
    }
    let root: HashMap<String, &RawValue> =
        serde_json::from_str(raw).map_err(|_| Error::LegacyUnhandled)?;
    let metadata: HashMap<String, &RawValue> =
        serde_json::from_str(root.get("metadata").ok_or(Error::LegacyUnhandled)?.get())
            .map_err(|_| Error::LegacyUnhandled)?;
    let subagent: HashMap<String, &RawValue> = serde_json::from_str(
        metadata
            .get("provider_subagent")
            .ok_or(Error::LegacyUnhandled)?
            .get(),
    )
    .map_err(|_| Error::LegacyUnhandled)?;
    crate::subagents::python_json_string(subagent.get(key).ok_or(Error::LegacyUnhandled)?.get())
}
fn one(records: Vec<Option<StoredAssistantRecord>>) -> Result<StoredAssistantRecord> {
    records
        .into_iter()
        .next()
        .flatten()
        .ok_or(Error::LegacyStorageUnhandled)
}

impl AssistantRecords {
    async fn settings_current(&self, session: &str) -> Result<RawSession> {
        identity(session)?;
        self.store
            .settings_session(session)
            .await
            .map_err(|e| storage_error(e, Kind::Session, session))
    }

    pub async fn update_session_settings(
        &self,
        session: &str,
        body: SettingsWrite,
    ) -> Result<StoredAssistantRecord> {
        let current = self.settings_current(session).await?;
        let fields = body.fields;
        let pending = self.has_pending_turn(session).await?;
        if pending
            && ["title", "archived", "mcp_server_ids", "hook_ids"]
                .iter()
                .any(|key| optional(&fields, key).is_some())
        {
            return Err(Error::Conflict(
                "conversation cannot be changed while a response is active",
            ));
        }
        let p = current.record.payload();
        let original = p["metadata"].as_object().ok_or(Error::LegacyUnhandled)?;
        let mut metadata = original.clone();
        let mut patch = Map::new();
        if let Some(title) = optional(&fields, "title") {
            patch.insert("title".into(), title.clone());
            metadata.insert("initial_title_state".into(), json!("operator"));
        }
        let archived = optional(&fields, "archived").and_then(Value::as_bool);
        if archived == Some(true) {
            // setdefault evaluates its timestamp even when a key already exists.
            let stamp = timestamp((self.clock)(), false);
            metadata
                .entry("archived_at")
                .or_insert_with(|| stamp.into());
        } else if archived == Some(false) {
            metadata.remove("archived_at");
        }
        if let Some(value) = optional(&fields, "mcp_server_ids") {
            let ids: Vec<String> = serde_json::from_value(value.clone())
                .map_err(|_| Error::Invalid("MCP selection must contain strings"))?;
            let rows = self
                .store
                .mcp_profiles_snapshot(&ids)
                .await
                .map_err(|e| storage_error(e, Kind::Session, session))?;
            for row in rows {
                let profile = row
                    .record
                    .map_err(|e| storage_error(e, Kind::Session, session))?
                    .ok_or_else(|| Error::EntityNotFound {
                        kind: "mcp_servers",
                        id: row.id,
                    })?;
                if profile.payload()["enabled"] == false {
                    let name = profile.payload()["name"]
                        .as_str()
                        .ok_or(Error::LegacyUnhandled)?;
                    let name = crate::subagents::python_string_repr(name)?;
                    return Err(Error::DynamicConflict(format!(
                        "MCP server {name} is disabled and cannot be selected"
                    )));
                }
            }
            metadata.insert("mcp_server_ids".into(), value.clone());
        }
        if let Some(value) = optional(&fields, "hook_ids") {
            metadata.insert("hook_ids".into(), value.clone());
        }
        if let Some(value) = fields.get("reasoning_effort") {
            metadata.insert("reasoning_effort".into(), value.clone());
        }
        if let Some(value) = optional(&fields, "allow_agent_messaging") {
            if value == true
                && (original.get("subagent_id").is_some_and(Value::is_string)
                    || original.get("temporary_assistant") == Some(&Value::Bool(true))
                    || original.get("archived_at").is_some_and(Value::is_string))
            {
                return Err(Error::Conflict(
                    "agent messaging is available only to saved main conversations",
                ));
            }
            metadata.insert("allow_agent_messaging".into(), value.clone());
        }
        if optional(&fields, "allow_subagents").is_some()
            || fields.contains_key("max_active_subagents")
            || optional(&fields, "subagent_provider_id").is_some()
            || optional(&fields, "subagent_model").is_some()
        {
            let harness = p["backend"] == "harness";
            let enabled = optional(&fields, "allow_subagents")
                .and_then(Value::as_bool)
                .unwrap_or_else(|| {
                    metadata
                        .get(if harness {
                            "provider_subagent"
                        } else {
                            "allow_subagents"
                        })
                        .is_some_and(truthy)
                });
            if harness {
                if enabled {
                    let saved = metadata.get("provider_subagent").unwrap_or(&Value::Null);
                    let provider: String = match optional(&fields, "subagent_provider_id")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                    {
                        Some(value) => value.into(),
                        None => saved_text(&current.raw_payload, saved, "provider_profile_id")?,
                    };
                    let model: String = match optional(&fields, "subagent_model")
                        .and_then(Value::as_str)
                        .filter(|s| !s.is_empty())
                    {
                        Some(value) => value.into(),
                        None => saved_text(&current.raw_payload, saved, "model")?,
                    };
                    let limit = match fields.get("max_active_subagents") {
                        Some(value) => Some(value),
                        None => saved_get(saved, "max_active")?,
                    };
                    let mut saved = changes(json!({"provider_profile_id":provider,"model":model}));
                    if let Some(limit) = limit.filter(|v| !v.is_null()) {
                        saved.insert("max_active".into(), limit.clone());
                    }
                    metadata.insert("provider_subagent".into(), saved.into());
                } else {
                    metadata.remove("provider_subagent");
                }
            } else {
                metadata.insert("allow_subagents".into(), enabled.into());
                if let Some(value) = fields.get("max_active_subagents") {
                    metadata.insert("max_active_subagents".into(), value.clone());
                }
            }
        }
        patch.insert("metadata".into(), metadata.into());
        let expected = optional(&fields, "expected_revision").map_or_else(
            || revision(p),
            |v| {
                v.as_number()
                    .map(ToString::to_string)
                    .ok_or(Error::Invalid("Expected revision must be an integer"))
            },
        )?;
        let updated = self
            .store
            .patch_session_settings(
                session,
                expected,
                patch,
                current.raw_payload,
                Arc::new(self.clock),
            )
            .await
            .map_err(|e| storage_error(e, Kind::Session, session))?
            .record;
        if archived == Some(true) {
            self.pause_schedule_for_archive(session).await?;
        } else if archived == Some(false) {
            self.resume_schedule_after_unarchive(session).await?;
        }
        Ok(updated)
    }

    async fn settings_schedule(&self, session: &str) -> Result<StoredAssistantRecord> {
        identity(session)?;
        self.store
            .session_plans_snapshot(Kind::Schedule, session)
            .await
            .map_err(|e| storage_error(e, Kind::Session, session))?
            .records
            .into_iter()
            .next()
            .ok_or_else(|| {
                Error::RetainedNotFound(format!("chat schedule not found for session: {session}"))
            })
    }
    fn next_schedule_time(&self, interval: i64) -> Result<Value> {
        let delta = Duration::try_seconds(interval).ok_or(Error::LegacyUnhandled)?;
        let time = (self.clock)()
            .checked_add_signed(delta)
            .ok_or(Error::LegacyUnhandled)?;
        Ok(timestamp(time, true).into())
    }
    async fn schedule_patch(
        &self,
        schedule: &StoredAssistantRecord,
        patch: Map<String, Value>,
    ) -> Result<StoredAssistantRecord> {
        let p = schedule.payload();
        let id = p["id"].as_str().ok_or(Error::LegacyUnhandled)?;
        self.store
            .patch_schedule(id, revision(p)?, patch, Arc::new(self.clock))
            .await
            .map_err(|e| storage_error(e, Kind::Schedule, id))
    }
    /// The trusted factory is invoked only after the source admission guards;
    /// rejected creation requests never consume a generated schedule identity.
    pub async fn create_schedule<F: FnOnce() -> String>(
        &self,
        session: &str,
        body: ScheduleCreate,
        make_id: F,
    ) -> Result<StoredAssistantRecord> {
        if !(3600..=2592000).contains(&body.interval_seconds) {
            return Err(Error::Invalid(
                "Schedule interval is outside its configured range",
            ));
        }
        identity(session)?;
        let current = self
            .store
            .get(Kind::Session, session)
            .await
            .map_err(|error| storage_error(error, Kind::Session, session))?;
        let p = current.payload();
        if p["backend"] != "provider" || !truthy(&p["provider_profile_id"]) {
            return Err(Error::Conflict("schedules require a provider conversation"));
        }
        match self.settings_schedule(session).await {
            Ok(_) => return Err(Error::Conflict("conversation already has a schedule")),
            Err(error) if absent(&error) => {}
            Err(error) => return Err(error),
        }
        let id = make_id();
        let next_run = self.next_schedule_time(body.interval_seconds)?;
        // Python evaluates next-run first, then each entity timestamp factory.
        let created_at = timestamp((self.clock)(), true);
        let updated_at = timestamp((self.clock)(), true);
        let record = StoredAssistantRecord::decode_persisted(
            Kind::Schedule,
            &serde_json::to_vec(
                &json!({"id":id,"engagement_id":p["engagement_id"],"session_id":p["id"],"provider_profile_id":p["provider_profile_id"],"model":p["model"],"interval_seconds":body.interval_seconds,"next_run_at":next_run,"revision":1,"created_at":created_at,"updated_at":updated_at}),
            )?,
        )?;
        one(self.store.apply(vec![Mutation::Create(record)]).await?)
    }
    pub async fn write_schedule(
        &self,
        session: &str,
        body: ScheduleWrite,
    ) -> Result<StoredAssistantRecord> {
        let schedule = self.settings_schedule(session).await?;
        if !body
            .expected_revision
            .matches(&schedule.payload()["revision"])
        {
            return Err(Error::Conflict(
                "schedule changed on another device; reload before retrying",
            ));
        }
        let mut patch = Map::new();
        if let Some(enabled) = body.enabled {
            patch.insert("enabled".into(), enabled.into());
            patch.insert("paused_by".into(), Value::Null);
            if enabled {
                patch.insert("skip_reason".into(), Value::Null);
                patch.insert(
                    "next_run_at".into(),
                    self.next_schedule_time(
                        schedule.payload()["interval_seconds"]
                            .as_i64()
                            .ok_or(Error::LegacyUnhandled)?,
                    )?,
                );
            }
        }
        let updated = self.schedule_patch(&schedule, patch).await?;
        if body.enabled == Some(true) {
            self.unarchive_session(session).await?;
        }
        Ok(updated)
    }
    async fn pause_schedule_for_archive(&self, session: &str) -> Result<()> {
        let schedule = match self.settings_schedule(session).await {
            Ok(v) => v,
            Err(e) if absent(&e) => return Ok(()),
            Err(e) => return Err(e),
        };
        if schedule.payload()["enabled"] == false {
            return Ok(());
        }
        self.schedule_patch(
            &schedule,
            changes(
                json!({"enabled":false,"paused_by":"archive","skip_reason":ARCHIVED_SKIP_REASON}),
            ),
        )
        .await?;
        Ok(())
    }
    async fn resume_schedule_after_unarchive(&self, session: &str) -> Result<()> {
        let schedule = match self.settings_schedule(session).await {
            Ok(v) => v,
            Err(e) if absent(&e) => return Ok(()),
            Err(e) => return Err(e),
        };
        let p = schedule.payload();
        if p["enabled"] == true || p["paused_by"] != "archive" {
            return Ok(());
        }
        let next = self.next_schedule_time(
            p["interval_seconds"]
                .as_i64()
                .ok_or(Error::LegacyUnhandled)?,
        )?;
        self.schedule_patch(
            &schedule,
            changes(json!({"enabled":true,"paused_by":null,"skip_reason":null,"next_run_at":next})),
        )
        .await?;
        Ok(())
    }
    pub(crate) async fn unarchive_session(&self, session: &str) -> Result<()> {
        for _ in 0..3 {
            let current = self.settings_current(session).await?;
            let p = current.record.payload();
            let mut metadata = p["metadata"]
                .as_object()
                .ok_or(Error::LegacyUnhandled)?
                .clone();
            if metadata.remove("archived_at").is_none() {
                return Ok(());
            }
            match self
                .store
                .patch_session_settings(
                    session,
                    revision(p)?,
                    changes(json!({"metadata":metadata})),
                    current.raw_payload,
                    Arc::new(self.clock),
                )
                .await
            {
                Ok(_) => return Ok(()),
                Err(
                    StorageError::Conflict
                    | StorageError::RevisionConflict { .. }
                    | StorageError::SettingsRevisionConflict { .. },
                ) => continue,
                Err(e) => return Err(storage_error(e, Kind::Session, session)),
            }
        }
        // The source intentionally gives up without an error after three races.
        Ok(())
    }
}
