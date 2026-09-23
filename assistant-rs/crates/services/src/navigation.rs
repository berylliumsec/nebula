//! Transcript navigation over existing durable records. Reading or bookmarking a
//! message never dispatches work or changes the current transcript's history.
use crate::{AssistantRecords, Error, Result, context::Revision, guard, history_timestamp};
use nebula_assistant_domain::records::{AssistantKind as Kind, StoredAssistantRecord};
use nebula_assistant_storage::entities::{Error as StorageError, Mutation, NavigationQuery};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct BookmarkWrite {
    pub active: bool,
    pub expected_revision: Revision,
}

#[derive(Clone, Debug)]
pub struct SearchRequest {
    pub q: String,
    pub session_id: Option<String>,
    pub bookmarked: bool,
    pub offset: u64,
    pub limit: u32,
}

pub fn bookmark_id(session_id: &str, message_id: &str) -> String {
    format!(
        "bookmark-{:x}",
        Sha256::digest(format!("{session_id}:{message_id}").as_bytes())
    )
}

// CPython str.strip includes the four ASCII information separators in addition
// to the Unicode White_Space property used by Rust's is_whitespace.
fn strip(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

fn casefold(value: &str) -> String {
    let mut folded = String::with_capacity(value.len());
    for scalar in value.chars() {
        if scalar.is_ascii() {
            folded.push(scalar.to_ascii_lowercase());
            continue;
        }
        match crate::unicode_casefold::MAPPINGS.binary_search_by_key(&scalar, |(c, _)| *c) {
            Ok(index) => folded.push_str(crate::unicode_casefold::MAPPINGS[index].1),
            Err(_) => folded.push(scalar),
        }
    }
    folded
}

fn excerpt(text: &str, folded_query: &str) -> String {
    let start = if folded_query.is_empty() {
        0
    } else {
        let folded = casefold(text);
        folded
            .find(folded_query)
            .map(|byte| folded[..byte].chars().count().saturating_sub(80))
            .unwrap_or(0)
    };
    // Preserve the Python API's exact behavior: the index is measured in the
    // folded text but slices the original text. Full folds may expand scalars.
    text.chars().skip(start).take(400).collect()
}

impl AssistantRecords {
    pub async fn session_messages(
        &self,
        session_id: &str,
        include_replaced: bool,
    ) -> Result<Vec<StoredAssistantRecord>> {
        self.get(Kind::Session, session_id).await?;
        let mut records = self.store.session_messages(session_id).await?;
        if !include_replaced {
            records.retain(|record| !record.is_replaced_message());
        }
        Ok(records)
    }

    pub async fn bookmarks(&self, session_id: &str) -> Result<Vec<StoredAssistantRecord>> {
        let session = self.get(Kind::Session, session_id).await?;
        Ok(self
            .store
            .bookmarks(
                session_id,
                session.payload()["engagement_id"]
                    .as_str()
                    .ok_or(StorageError::CorruptEnvelope)?,
            )
            .await?)
    }

    pub async fn set_bookmark(
        &self,
        session_id: &str,
        message_id: &str,
        request: BookmarkWrite,
    ) -> Result<StoredAssistantRecord> {
        if request.expected_revision.is_negative() {
            return Err(Error::Invalid("Expected revision must be non-negative"));
        }
        let session = self.get(Kind::Session, session_id).await?;
        let message = self.get(Kind::Message, message_id).await?;
        if message.payload()["session_id"] != session.payload()["id"]
            || message.payload()["engagement_id"] != session.payload()["engagement_id"]
        {
            return Err(Error::StorageNotFound(
                "Message is not in this conversation",
            ));
        }
        let identity = bookmark_id(session_id, message_id);
        let mutation = if request.expected_revision.is_zero() {
            Mutation::Create(self.create_record(
                Kind::Bookmark,
                json!({"id":identity,"engagement_id":session.payload()["engagement_id"],
                    "session_id":session_id,"message_id":message_id,"active":request.active}),
            )?)
        } else {
            let current = self.get(Kind::Bookmark, &identity).await?;
            Mutation::Patch {
                kind: Kind::Bookmark,
                id: identity,
                expected_revision: request.expected_revision.as_i64().ok_or_else(|| {
                    Error::RevisionConflict {
                        expected: request.expected_revision.to_string(),
                        found: current.payload()["revision"].as_i64().unwrap_or(0),
                    }
                })?,
                changes: json!({"active":request.active})
                    .as_object()
                    .unwrap()
                    .clone(),
            }
        };
        let mut changed = self
            .store
            .apply_guarded(vec![guard(&session)?, guard(&message)?], vec![mutation])
            .await?;
        if changed.len() != 1 {
            return Err(StorageError::CorruptEnvelope.into());
        }
        changed
            .pop()
            .flatten()
            .ok_or(Error::Storage(StorageError::CorruptEnvelope))
    }

    pub async fn search_messages(&self, project_id: &str, request: SearchRequest) -> Result<Value> {
        if request.q.chars().count() > 512 || !(1..=100).contains(&request.limit) {
            return Err(Error::Invalid("Search request exceeds its field bounds"));
        }
        if project_id.is_empty()
            || project_id.chars().count() > 200
            || !self.store.project_exists(project_id).await?
        {
            return Err(Error::EntityNotFound {
                kind: "engagements",
                id: project_id.into(),
            });
        }
        if let Some(session_id) = request.session_id.as_deref().filter(|id| !id.is_empty()) {
            let session = self.get(Kind::Session, session_id).await?;
            if session.payload()["engagement_id"] != project_id {
                return Err(Error::StorageNotFound(
                    "Conversation is not in this project",
                ));
            }
        }
        let query = strip(&request.q);
        let folded_query = casefold(query);
        let page = self
            .store
            .search_messages(NavigationQuery {
                project_id: project_id.into(),
                session_id: request.session_id,
                text: query.into(),
                bookmarked: request.bookmarked,
                offset: request.offset,
                limit: request.limit,
            })
            .await?;
        let mut items = Vec::with_capacity(page.records.len());
        for message in page.records {
            if message.is_replaced_message() {
                continue;
            }
            let p = message.payload();
            let session = self
                .get(
                    Kind::Session,
                    p["session_id"]
                        .as_str()
                        .ok_or(StorageError::CorruptEnvelope)?,
                )
                .await?;
            items.push(json!({
                "message_id":p["id"],"session_id":session.payload()["id"],
                "title":session.payload()["title"],"role":p["role"],
                "excerpt":excerpt(p["content"].as_str().ok_or(StorageError::CorruptEnvelope)?, &folded_query),
                "sequence":p["sequence"],"created_at":history_timestamp(&p["created_at"])?
            }));
        }
        Ok(json!({"items":items,"next_offset":page.next_offset}))
    }
}
