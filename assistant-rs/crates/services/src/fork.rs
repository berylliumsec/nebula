//! Independent retained conversation branches, with the same sequential commit
//! boundaries as Python. This service never launches a provider or vendor turn.
use crate::{AssistantRecords, Error, Result, goal_drafts::constructed};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    model_validation::{CreatedEntityDefaults, Location, Model, TypedModelPath},
    records::{AssistantKind as Kind, MAX_RECORD_BYTES, RecordError, StoredAssistantRecord},
    tool_receipt::exact_integer,
};
use nebula_assistant_storage::entities::{Error as StorageError, ForkHarness, ForkRecord};
use serde::{
    Deserialize, Deserializer, Serialize, Serializer,
    de::{MapAccess, Visitor},
    ser::SerializeMap,
};
use serde_json::{Value, value::RawValue};
use std::{collections::HashMap, io::Write, sync::Arc};

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ForkRequest {
    #[serde(default)]
    pub through_message_id: Option<String>,
    #[serde(default)]
    pub before_message_id: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
}
fn storage(error: StorageError, kind: &'static str, id: &str) -> Error {
    match error {
        StorageError::NotFound => Error::EntityNotFound {
            kind,
            id: id.into(),
        },
        StorageError::WrappedRecord(_) => Error::LegacyStorageUnhandled,
        StorageError::RetainedModelValidation(report) => Error::RetainedModelValidation(report),
        StorageError::Record(RecordError::TooLarge) => StorageError::ReadLimit.into(),
        error => error.into(),
    }
}
fn scalar<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key].as_str().ok_or(Error::LegacyUnhandled)
}
fn truthy_id(value: &Option<String>) -> bool {
    value.as_ref().is_some_and(|s| !s.is_empty())
}
const PRIVATE: &[&str] = &[
    "archived_at",
    "subagent_id",
    "subagent_parent_session_id",
    "subagent_parent_turn_id",
    "temporary_assistant",
];
impl AssistantRecords {
    pub async fn fork_conversation(
        &self,
        session: &str,
        body: ForkRequest,
        make_id: impl FnMut() -> String + Send + 'static,
    ) -> Result<StoredAssistantRecord> {
        if truthy_id(&body.through_message_id) == truthy_id(&body.before_message_id) {
            return Err(Error::Invalid("Choose exactly one branch boundary"));
        }
        let permit = self.store.fork_workflow()?;
        let service = self.clone();
        let session = session.to_owned();
        // Dropping a JoinHandle detaches it. The independent workflow permit
        // bounds abandoned work through the final exception cleanup, just as
        // the source synchronous handler continues after client cancellation.
        tokio::spawn(async move {
            let _permit = permit;
            service.fork_operation(&session, body, make_id).await
        })
        .await
        .map_err(|_| Error::LegacyUnhandled)?
    }
    async fn fork_operation(
        &self,
        session: &str,
        body: ForkRequest,
        mut make_id: impl FnMut() -> String + Send,
    ) -> Result<StoredAssistantRecord> {
        let source = self.fork_source(session).await?;
        let vendor = if source.record.payload()["backend"] == "harness" {
            let id = source.record.payload()["harness_session_id"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or(Error::HarnessState(
                    "harness conversation has no vendor session to branch",
                ))?;
            let old = self
                .store
                .fork_harness(id, Arc::new(self.clock))
                .await
                .map_err(|e| storage(e, DependencyKind::HarnessSession.as_str(), id))?;
            if matches!(
                old.record.payload()["status"].as_str(),
                Some("running" | "waiting_approval")
            ) {
                return Err(Error::HarnessState(
                    "harness session cannot be forked while a turn is active",
                ));
            }
            Some(self.clone_harness(old, &body, &mut make_id).await?)
        } else {
            None
        };
        // Python releases its route-level source reference only after the inner
        // operation. No source relationship is rechecked inside a later commit.
        let result = self
            .fork_chat(session, &body, vendor.as_deref(), &mut make_id)
            .await;
        if result.is_err()
            && let Some(id) = vendor
        {
            match self.store.cleanup_fork_harness(&id).await {
                Ok(()) | Err(StorageError::NotFound) => {}
                // A source database cleanup exception replaces the original
                // failure. Resource/admission errors keep their actionable type.
                Err(StorageError::Database(_)) => return Err(Error::LegacyStorageUnhandled),
                Err(error) => {
                    return Err(storage(error, DependencyKind::HarnessSession.as_str(), &id));
                }
            }
        }
        result
    }
    async fn fork_source(&self, id: &str) -> Result<ForkRecord> {
        self.store
            .fork_session(id)
            .await
            .map_err(|e| storage(e, Kind::Session.as_str(), id))
    }
    fn fork_defaults(&self, id: Option<String>, harness: bool) -> CreatedEntityDefaults {
        CreatedEntityDefaults {
            id,
            created_at: (self.clock)(),
            updated_at: (self.clock)(),
            last_activity_at: harness.then(|| (self.clock)()),
        }
    }
    async fn clone_harness(
        &self,
        old: ForkHarness,
        body: &ForkRequest,
        make_id: &mut impl FnMut() -> String,
    ) -> Result<String> {
        let fields = raw_fields(&old.raw_payload)?;
        let mut metadata =
            Object::from_raw(fields.get("metadata").ok_or(Error::LegacyUnhandled)?.get())?;
        metadata.set("forked_from_session_id", &old.record.payload()["id"])?;
        metadata.set(
            "fork_reason",
            &format!(
                "conversation fork through {}",
                body.through_message_id.as_deref().unwrap_or("None")
            ),
        )?;
        metadata.set("context_management", &"runtime_managed")?;
        let id = make_id();
        let mut args = Object::default();
        args.set("id", &id)?;
        args.copy(&fields, &["engagement_id", "harness_profile_id", "model"])?;
        args.set("status", &"starting")?;
        args.copy(&fields, &["mcp_server_ids", "mcp_snapshot"])?;
        args.set("metadata", &metadata)?;
        let raw = args.encode()?;
        let record = StoredDependency::decode_harness_session_created(
            raw.as_bytes(),
            &self.fork_defaults(None, true),
        )
        .map_err(constructed)?;
        let id = scalar(record.payload(), "id")?.to_owned();
        self.store
            .create_fork_harness(ForkHarness::new(record, &raw)?)
            .await
            .map_err(|e| storage(e, DependencyKind::HarnessSession.as_str(), &id))?;
        Ok(id)
    }
    async fn fork_chat(
        &self,
        session: &str,
        body: &ForkRequest,
        vendor: Option<&str>,
        make_id: &mut impl FnMut() -> String,
    ) -> Result<StoredAssistantRecord> {
        let source = self.fork_source(session).await?;
        if self.has_pending_fork(session).await? {
            return Err(Error::HistoryConflict(
                "conversation cannot be forked while a response is active",
            ));
        }
        let messages = self
            .store
            .fork_messages(session)
            .await
            .map_err(|e| storage(e, Kind::Session.as_str(), session))?;
        let mut keyed = Vec::with_capacity(messages.len());
        for message in messages {
            if message.record.is_replaced_message() {
                continue;
            }
            let p = message.record.payload();
            let sequence = exact_integer(&p["sequence"]).ok_or(Error::LegacyUnhandled)?;
            let created = chrono::DateTime::parse_from_rfc3339(scalar(p, "created_at")?)
                .map_err(|_| Error::LegacyUnhandled)?;
            keyed.push((sequence, created, scalar(p, "id")?.to_owned(), message));
        }
        keyed.sort_by(|a, b| {
            a.0.cmp(&b.0)
                .then_with(|| a.1.cmp(&b.1))
                .then_with(|| a.2.cmp(&b.2))
        });
        let boundary_id = body
            .through_message_id
            .as_deref()
            .filter(|s| !s.is_empty())
            .or(body.before_message_id.as_deref())
            .ok_or(Error::LegacyUnhandled)?;
        let boundary =
            keyed
                .iter()
                .find(|(_, _, id, _)| id == boundary_id)
                .ok_or(Error::HistoryConflict(
                    "fork message does not belong to the selected conversation",
                ))?;
        let sequence = boundary.0.clone();
        let boundary_id = boundary.2.clone();
        let p = source.record.payload();
        let harness = p["backend"] == "harness";
        if harness && vendor.is_none_or(str::is_empty) {
            return Err(Error::ChatConfiguration(
                "harness conversation forks require an independent harness session",
            ));
        }
        let id = make_id();
        let title = body
            .title
            .as_deref()
            .filter(|s| !s.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| format!("{} (fork)", p["title"].as_str().unwrap_or_default()))
            .chars()
            .take(300)
            .collect::<String>();
        let fields = raw_fields(&source.raw_payload)?;
        let mut metadata =
            Object::from_raw(fields.get("metadata").ok_or(Error::LegacyUnhandled)?.get())?;
        metadata
            .entries
            .retain(|(key, _)| !PRIVATE.contains(&key.as_str()));
        metadata.set("forked_from_session_id", &p["id"])?;
        metadata.set("forked_from_message_id", &boundary_id)?;
        metadata.set("workspace_is_shared", &true)?;
        metadata.set("branch_before_message", &truthy_id(&body.before_message_id))?;
        metadata.set("harness_context_handoff_pending", &harness)?;
        let mut args = Object::default();
        args.set("id", &id)?;
        args.copy(&fields, &["engagement_id"])?;
        args.set("title", &title)?;
        args.copy(
            &fields,
            &["backend", "provider_profile_id", "harness_profile_id"],
        )?;
        args.set("harness_session_id", &if harness { vendor } else { None })?;
        args.copy(&fields, &["model"])?;
        args.set("parent_session_id", &p["id"])?;
        args.set("forked_from_message_id", &boundary_id)?;
        args.set("metadata", &metadata)?;
        let fork = self.create_fork(Kind::Session, args, None, &[]).await?;
        for (seq, _, _, message) in keyed {
            if seq > sequence || (truthy_id(&body.before_message_id) && seq == sequence) {
                break;
            }
            let fields = raw_fields(&message.raw_payload)?;
            let mut args = Object::default();
            args.set("id", &make_id())?;
            args.set("engagement_id", &fork.payload()["engagement_id"])?;
            args.set("session_id", &fork.payload()["id"])?;
            args.copy(&fields, &["sequence", "role", "content", "content_blocks"])?;
            args.set("source_message_id", &message.record.payload()["id"])?;
            args.copy(
                &fields,
                &[
                    "provider_profile_id",
                    "model",
                    "usage",
                    "finish_reason",
                    "provider_request_id",
                    "citations",
                ],
            )?;
            let mut metadata =
                Object::from_raw(fields.get("metadata").ok_or(Error::LegacyUnhandled)?.get())?;
            metadata.set("fork_source_message_id", &message.record.payload()["id"])?;
            args.set("metadata", &metadata)?;
            let mut typed = Vec::new();
            for (field, model) in [
                ("content_blocks", Model::ChatContentBlock),
                ("citations", Model::ChatCitation),
            ] {
                for index in 0..message.record.payload()[field]
                    .as_array()
                    .ok_or(Error::LegacyUnhandled)?
                    .len()
                {
                    typed.push(TypedModelPath {
                        path: vec![Location::Field(field.into()), Location::Index(index)],
                        model,
                    });
                }
            }
            if !message.record.payload()["usage"].is_null() {
                typed.push(TypedModelPath {
                    path: vec![Location::Field("usage".into())],
                    model: Model::ChatTokenUsage,
                });
            }
            self.create_fork(Kind::Message, args, None, &typed).await?;
        }
        let decisions = self
            .store
            .fork_decisions(session, scalar(p, "engagement_id")?)
            .await
            .map_err(|e| storage(e, Kind::Session.as_str(), session))?;
        let decision_boundary = sequence - i32::from(truthy_id(&body.before_message_id));
        for decision in decisions {
            let value = decision.record.payload();
            if value["scope"] != "conversation"
                || value["status"] != "active"
                || exact_integer(&value["effective_sequence"]).ok_or(Error::LegacyUnhandled)?
                    > decision_boundary
            {
                continue;
            }
            let fields = raw_fields(&decision.raw_payload)?;
            let mut args = Object::default();
            args.set("engagement_id", &fork.payload()["engagement_id"])?;
            args.set("session_id", &fork.payload()["id"])?;
            args.copy(
                &fields,
                &[
                    "kind",
                    "text",
                    "source_message_id",
                    "source_session_id",
                    "source_selection",
                    "effective_sequence",
                ],
            )?;
            args.set("copied_from_id", &value["id"])?;
            args.set("copied_from_revision", &value["revision"])?;
            self.create_fork(Kind::Decision, args, Some(make_id()), &[])
                .await?;
        }
        if !harness {
            // ChatGoalService.get rereads the Session only at this late stage.
            // Missing Session/Goal is suppressed; malformed or duplicate is not.
            let goals = match self.fork_source(session).await {
                Err(Error::EntityNotFound { .. }) => Vec::new(),
                Err(error) => return Err(error),
                Ok(_) => self
                    .store
                    .fork_goals(session)
                    .await
                    .map_err(|e| storage(e, Kind::Session.as_str(), session))?,
            };
            if goals.len() > 1 {
                return Err(Error::Conflict(
                    "conversation has more than one authoritative goal",
                ));
            }
            if let Some(goal) = goals.into_iter().next() {
                let fields = raw_fields(&goal.raw_payload)?;
                let mut args = Object::default();
                args.set("engagement_id", &fork.payload()["engagement_id"])?;
                args.set("session_id", &fork.payload()["id"])?;
                args.copy(
                    &fields,
                    &[
                        "objective",
                        "completion_criteria",
                        "plan",
                        "token_budget",
                        "time_budget_seconds",
                        "step_budget",
                        "child_budget",
                        "skill_snapshots",
                    ],
                )?;
                let mut metadata = Object::default();
                metadata.set("forked_from_goal_id", &goal.record.payload()["id"])?;
                metadata.set("workspace_is_shared", &true)?;
                args.set("metadata", &metadata)?;
                self.create_fork(Kind::Goal, args, Some(make_id()), &[])
                    .await?;
            }
        }
        Ok(fork)
    }
    async fn create_fork(
        &self,
        kind: Kind,
        args: Object,
        id: Option<String>,
        typed: &[TypedModelPath],
    ) -> Result<StoredAssistantRecord> {
        let raw = args.encode()?;
        let record = StoredAssistantRecord::decode_fork_created(
            kind,
            raw.as_bytes(),
            &self.fork_defaults(id, false),
            typed,
        )
        .map_err(constructed)?;
        let result = record.clone();
        self.store
            .create_fork_record(ForkRecord::new(record, &raw)?)
            .await
            .map_err(|e| {
                storage(
                    e,
                    kind.as_str(),
                    result.payload()["id"].as_str().unwrap_or_default(),
                )
            })?;
        Ok(result)
    }
}
fn raw_fields(raw: &str) -> Result<HashMap<String, &RawValue>> {
    Ok(serde_json::from_str(raw)?)
}
#[derive(Default)]
struct Object {
    entries: Vec<(String, Box<RawValue>)>,
}
impl Serialize for Object {
    fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        let mut map = s.serialize_map(Some(self.entries.len()))?;
        for (key, value) in &self.entries {
            map.serialize_entry(key, value)?;
        }
        map.end()
    }
}
impl Object {
    fn from_raw(raw: &str) -> Result<Self> {
        struct Ordered;
        impl<'de> Visitor<'de> for Ordered {
            type Value = Object;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a retained dictionary")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Object, A::Error> {
                let mut entries = Vec::new();
                let mut positions: HashMap<String, usize> = HashMap::new();
                while let Some((key, value)) = map.next_entry::<String, Box<RawValue>>()? {
                    if let Some(&index) = positions.get(&key) {
                        entries[index] = (key, value);
                    } else {
                        positions.insert(key.clone(), entries.len());
                        entries.push((key, value));
                    }
                }
                Ok(Object { entries })
            }
        }
        struct OrderedObject(Object);
        impl<'de> Deserialize<'de> for OrderedObject {
            fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
                d.deserialize_map(Ordered).map(Self)
            }
        }
        Ok(serde_json::from_str::<OrderedObject>(raw)?.0)
    }
    fn set<T: Serialize>(&mut self, key: &str, value: &T) -> Result<()> {
        let encoded = encode(value)?;
        let value = RawValue::from_string(encoded)?;
        if let Some((_, entry)) = self.entries.iter_mut().find(|(name, _)| name == key) {
            *entry = value;
        } else {
            self.entries.push((key.into(), value));
        }
        Ok(())
    }
    fn copy(&mut self, source: &HashMap<String, &RawValue>, keys: &[&str]) -> Result<()> {
        for key in keys {
            self.set(key, source.get(*key).ok_or(Error::LegacyUnhandled)?)?;
        }
        Ok(())
    }
    fn encode(&self) -> Result<String> {
        encode(self)
    }
}
fn encode<T: Serialize>(value: &T) -> Result<String> {
    struct Bounded(Vec<u8>);
    impl Write for Bounded {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if self.0.len().saturating_add(bytes.len()) > MAX_RECORD_BYTES {
                return Err(std::io::Error::other("fork constructor byte limit"));
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
    String::from_utf8(out.0).map_err(|_| Error::LegacyUnhandled)
}
