//! Saved operator context and read cursors. No provider, tool, approval or turn
//! execution is performed; acknowledgments never resolve pending actions.
use crate::{AssistantRecords, Error, Result, guard, history_timestamp};
use chrono::{DateTime, SecondsFormat};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{Error as StorageError, Mutation};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

/// Expected revisions preserve Python's integer range until compared with the
/// persisted i64 revision. Oversized expectations can never match a stored row.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(transparent)]
pub struct Revision(serde_json::Number);
impl Revision {
    pub(crate) fn is_zero(&self) -> bool {
        self.0.as_i64() == Some(0)
    }
    pub(crate) fn is_negative(&self) -> bool {
        self.0.to_string().starts_with('-')
    }
    pub(crate) fn matches(&self, value: &Value) -> bool {
        value.as_number() == Some(&self.0)
    }
    pub(crate) fn as_i64(&self) -> Option<i64> {
        self.0.as_i64()
    }
}
impl From<i64> for Revision {
    fn from(value: i64) -> Self {
        Self(value.into())
    }
}
impl std::fmt::Display for Revision {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(formatter)
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum DecisionAction {
    #[default]
    Save,
    Supersede,
    Remove,
    Promote,
}
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DecisionKind {
    #[default]
    Decision,
    Constraint,
    Assumption,
    Question,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DecisionWrite {
    pub expected_revision: Revision,
    #[serde(default)]
    pub action: DecisionAction,
    #[serde(default)]
    pub kind: DecisionKind,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub source_message_id: Option<String>,
    #[serde(default)]
    pub source_selection: Option<String>,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CursorWrite {
    pub expected_revision: Revision,
    pub through_at: String,
    pub device_id: String,
}

pub fn cursor_id(session_id: &str, device_id: &str) -> String {
    format!(
        "chat-read-{:x}",
        Sha256::digest(format!("{session_id}:{device_id}").as_bytes())
    )
}

fn owner<'a>(supplied: &'a str, authenticated: Option<&'a str>) -> Result<&'a str> {
    if supplied.is_empty() || supplied.chars().count() > 200 {
        return Err(Error::Invalid(
            "Device identity must contain 1 to 200 characters",
        ));
    }
    Ok(authenticated.filter(|s| !s.is_empty()).unwrap_or(supplied))
}
fn patch(record: &StoredAssistantRecord, changes: Value) -> Result<Mutation> {
    let guard = guard(record)?;
    Ok(Mutation::Patch {
        kind: guard.kind,
        id: guard.id,
        expected_revision: guard.revision,
        changes: changes
            .as_object()
            .ok_or(Error::Invalid("Changes must be an object"))?
            .clone(),
    })
}
fn revision_entry(record: &StoredAssistantRecord) -> Result<Value> {
    let p = record.payload();
    Ok(
        json!({"revision":p["revision"],"text":p["text"],"kind":p["kind"],"status":p["status"],"updated_at":history_timestamp(&p["updated_at"])?}),
    )
}

impl AssistantRecords {
    pub async fn decisions(&self, session_id: &str) -> Result<Vec<StoredAssistantRecord>> {
        let session = self.get(Kind::Session, session_id).await?;
        Ok(self
            .store
            .decisions(
                session_id,
                session.payload()["engagement_id"]
                    .as_str()
                    .ok_or(StorageError::CorruptEnvelope)?,
                false,
            )
            .await?)
    }

    /// Capture exactly the active saved context. Question entries remain active
    /// until an operator explicitly removes or supersedes them.
    pub async fn decision_snapshot(
        &self,
        session_id: &str,
        project_id: Option<&str>,
    ) -> Result<Vec<Value>> {
        let Some(project_id) = project_id.filter(|id| !id.is_empty()) else {
            return Ok(vec![]);
        };
        let entries = self.store.decisions(session_id, project_id, true).await?;
        if entries.len() > 100
            || entries
                .iter()
                .map(|p| {
                    p.payload()["text"]
                        .as_str()
                        .map_or(0, |s| s.chars().count())
                })
                .sum::<usize>()
                > 40000
        {
            return Err(Error::Conflict(
                "Active operator context is too large. Supersede or remove decisions in Context before sending",
            ));
        }
        Ok(entries.iter().map(|entry| {let p=entry.payload(); json!({"id":p["id"],"revision":p["revision"],"kind":p["kind"],"text":p["text"],"scope":p["scope"],"source_message_id":p["source_message_id"],"source_session_id":p["source_session_id"]})}).collect())
    }

    pub async fn write_decision(
        &self,
        session_id: &str,
        decision_id: &str,
        body: DecisionWrite,
    ) -> Result<StoredAssistantRecord> {
        if body.expected_revision.is_negative()
            || body.text.chars().count() > 4000
            || body
                .source_selection
                .as_ref()
                .is_some_and(|s| s.chars().count() > 200000)
        {
            return Err(Error::Invalid("Decision request exceeds its field bounds"));
        }
        let session = self.get(Kind::Session, session_id).await?;
        let project = &session.payload()["engagement_id"];
        if decision_id.chars().count() > 200 {
            return Err(Error::Invalid("Decision identity is too long"));
        }
        let mut guards = vec![guard(&session)?];
        if body.expected_revision.is_zero() {
            if body.action != DecisionAction::Save || body.text.trim().is_empty() {
                return Err(Error::Invalid("Write the decision before saving"));
            }
            let source = match body.source_message_id.as_deref().filter(|s| !s.is_empty()) {
                Some(id) => Some(self.get(Kind::Message, id).await?),
                None => None,
            };
            if source
                .as_ref()
                .is_some_and(|p| p.payload()["session_id"] != session_id)
            {
                return Err(Error::NotFound(
                    "Source message does not belong to this conversation",
                ));
            }
            if let Some(selection) = body.source_selection.as_deref().filter(|s| !s.is_empty())
                && !source
                    .as_ref()
                    .and_then(|p| p.payload()["content"].as_str())
                    .is_some_and(|s| s.contains(selection))
            {
                return Err(Error::Invalid(
                    "Source selection must be exact text from the saved message",
                ));
            }
            let sequence = if let Some(source) = &source {
                guards.push(guard(source)?);
                source.payload()["sequence"]
                    .as_i64()
                    .ok_or(StorageError::CorruptEnvelope)?
            } else {
                self.store.latest_message_sequence(session_id).await?
            };
            let record=self.create_record(Kind::Decision,json!({"id":decision_id,"engagement_id":project,"session_id":session_id,"kind":body.kind,"text":body.text,
                "source_message_id":source.as_ref().map(|p|p.payload()["id"].clone()),"source_session_id":source.as_ref().map(|_|session_id),"source_selection":body.source_selection,"effective_sequence":sequence}))?;
            return one(self
                .store
                .apply_guarded(guards, vec![Mutation::Create(record)])
                .await?);
        }
        let current = self.get(Kind::Decision, decision_id).await?;
        let p = current.payload();
        if &p["engagement_id"] != project
            || p["scope"] == "conversation" && p["session_id"] != session_id
        {
            return Err(Error::NotFound(
                "Decision does not belong to this project chat",
            ));
        }
        if !body.expected_revision.matches(&p["revision"]) {
            return Err(Error::Conflict(
                "Decision changed on another device. Reload and reapply your edit",
            ));
        }
        let mut history = p["history"]
            .as_array()
            .ok_or(StorageError::CorruptEnvelope)?
            .clone();
        history.push(revision_entry(&current)?);
        if body.action == DecisionAction::Promote {
            if p["scope"] != "conversation" || p["status"] != "active" {
                return Err(Error::Conflict(
                    "Only active conversation entries can be promoted",
                ));
            }
            let promoted=self.create_record(Kind::Decision,json!({"id":format!("project-{decision_id}"),"engagement_id":project,"session_id":null,"scope":"project","kind":p["kind"],"text":p["text"],
                "source_message_id":p["source_message_id"],"source_session_id":p["source_session_id"],"source_selection":p["source_selection"],"effective_sequence":p["effective_sequence"],"copied_from_id":decision_id,"copied_from_revision":p["revision"]}))?;
            let mut changed = self
                .store
                .apply_guarded(
                    guards,
                    vec![
                        patch(&current, json!({"status":"superseded","history":history}))?,
                        Mutation::Create(promoted),
                    ],
                )
                .await?;
            return changed
                .pop()
                .flatten()
                .ok_or(Error::Storage(StorageError::CorruptEnvelope));
        }
        if p["status"] != "active" {
            return Err(Error::Conflict(
                "This entry is no longer active. Create a new entry to restore it explicitly",
            ));
        }
        let changes = if body.action == DecisionAction::Save {
            if body.text.trim().is_empty() {
                return Err(Error::Invalid("Decision text cannot be empty"));
            }
            json!({"history":history,"text":body.text,"kind":body.kind})
        } else {
            json!({"history":history,"status":if body.action==DecisionAction::Remove {"removed"} else {"superseded"}})
        };
        one(self
            .store
            .apply_guarded(guards, vec![patch(&current, changes)?])
            .await?)
    }

    pub async fn read_cursor(
        &self,
        session_id: &str,
        supplied_device: &str,
        authenticated_device: Option<&str>,
    ) -> Result<Option<StoredAssistantRecord>> {
        self.get(Kind::Session, session_id).await?;
        let owner = owner(supplied_device, authenticated_device)?;
        match self
            .get(Kind::ReadCursor, &cursor_id(session_id, owner))
            .await
        {
            Ok(record) => Ok(Some(record)),
            Err(Error::EntityNotFound { .. }) => Ok(None),
            Err(error) => Err(error),
        }
    }

    pub async fn advance_cursor(
        &self,
        session_id: &str,
        body: CursorWrite,
        authenticated_device: Option<&str>,
    ) -> Result<StoredAssistantRecord> {
        self.advance_cursor_at(session_id, body, authenticated_device, (self.clock)())
            .await
    }

    /// The host supplies its observation clock; request data never controls it.
    /// Record creation keeps its own factory clock, matching the legacy service.
    pub async fn advance_cursor_at(
        &self,
        session_id: &str,
        body: CursorWrite,
        authenticated_device: Option<&str>,
        observed_at: chrono::DateTime<chrono::Utc>,
    ) -> Result<StoredAssistantRecord> {
        if body.expected_revision.is_negative() {
            return Err(Error::Invalid("Expected revision must be non-negative"));
        }
        let session = self.get(Kind::Session, session_id).await?;
        let owner = owner(&body.device_id, authenticated_device)?;
        let identity = cursor_id(session_id, owner);
        let through = DateTime::parse_from_rfc3339(&body.through_at)
            .map_err(|_| Error::Invalid("Read cursor must use a recorded server timestamp"))?;
        if through > observed_at {
            return Err(Error::Invalid(
                "Read cursor must use a recorded server timestamp",
            ));
        }
        let guards = vec![guard(&session)?];
        let through_text = through.to_rfc3339_opts(
            if through.timestamp_subsec_micros() == 0 {
                SecondsFormat::Secs
            } else {
                SecondsFormat::Micros
            },
            true,
        );
        if body.expected_revision.is_zero() {
            let record=self.create_record(Kind::ReadCursor,json!({"id":identity,"engagement_id":session.payload()["engagement_id"],"session_id":session_id,"device_id":owner,"through_at":through_text}))?;
            return one(self
                .store
                .apply_guarded(guards, vec![Mutation::Create(record)])
                .await?);
        }
        let current = self.get(Kind::ReadCursor, &identity).await?;
        let old = DateTime::parse_from_rfc3339(
            current.payload()["through_at"]
                .as_str()
                .ok_or(StorageError::CorruptEnvelope)?,
        )
        .map_err(|_| StorageError::CorruptEnvelope)?;
        if through < old {
            return Err(Error::Conflict(
                "This device has already read newer activity. Reload its cursor",
            ));
        }
        let mut change = patch(&current, json!({"through_at":through_text}))?;
        if let Mutation::Patch {
            expected_revision, ..
        } = &mut change
        {
            *expected_revision =
                body.expected_revision
                    .as_i64()
                    .ok_or_else(|| Error::RevisionConflict {
                        expected: body.expected_revision.to_string(),
                        found: current.payload()["revision"].as_i64().unwrap_or(0),
                    })?;
        }
        one(self.store.apply_guarded(guards, vec![change]).await?)
    }
}
fn one(mut changed: Vec<Option<StoredAssistantRecord>>) -> Result<StoredAssistantRecord> {
    if changed.len() != 1 {
        return Err(Error::Storage(StorageError::CorruptEnvelope));
    }
    changed
        .pop()
        .flatten()
        .ok_or(Error::Storage(StorageError::CorruptEnvelope))
}
