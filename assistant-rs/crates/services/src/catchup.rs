//! Pure catch-up and turn-summary projections over retained state. Read cursors
//! control unseen activity; they never resolve approvals or harness questions.
use crate::{AssistantRecords, Error, Result, history_timestamp, timestamp};
use chrono::{DateTime, FixedOffset, Utc};
use nebula_assistant_domain::records::AssistantKind as Kind;
use nebula_assistant_storage::entities::{Error as StorageError, PendingSnapshot};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};

fn text<'a>(value: &'a Value, field: &str) -> Result<&'a str> {
    value[field]
        .as_str()
        .ok_or(StorageError::CorruptEnvelope.into())
}

fn recorded_time(value: &Value) -> Result<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(value.as_str().ok_or(StorageError::CorruptEnvelope)?)
        .map_err(|_| StorageError::CorruptEnvelope.into())
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64().is_none_or(|n| n != 0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn python_whitespace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

// Match chat_naming._GREETING's Unicode re.I behavior for these ASCII words.
// Python's ASCII letter ranges also fold dotted/dotless I, long S and Kelvin K.
fn substantive(prompt: &str) -> bool {
    let prompt = prompt.trim_matches(python_whitespace);
    if prompt.is_empty() {
        return false;
    }
    let candidate = prompt.trim_end_matches(|c| python_whitespace(c) || c == '!' || c == '.');
    // The longest allowed greeting has 14 scalars. Normal prompts need no
    // allocation or complete case conversion to establish they are substantive.
    if candidate.chars().take(15).count() > 14 {
        return true;
    }
    let candidate: String = candidate
        .chars()
        .map(|c| match c {
            '\u{130}' | '\u{131}' => 'i',
            '\u{17f}' => 's',
            '\u{212a}' => 'k',
            c => c.to_ascii_lowercase(),
        })
        .collect();
    !matches!(
        candidate.as_str(),
        "hi" | "hello"
            | "hey"
            | "greetings"
            | "good morning"
            | "good afternoon"
            | "good evening"
            | "thanks"
            | "thank you"
    )
}

fn terminal(status: &str) -> bool {
    matches!(status, "complete" | "failed" | "cancelled" | "interrupted")
}

fn pending_notices(snapshot: PendingSnapshot, now: DateTime<Utc>) -> Result<Vec<Value>> {
    let harnesses: HashMap<_, _> = snapshot
        .harnesses
        .iter()
        .map(|record| Ok((text(record.payload(), "id")?, record.payload())))
        .collect::<Result<_>>()?;
    let mut active_ids = HashSet::new();
    let mut active_harnesses = HashSet::new();
    let mut approval_owners = HashMap::new();
    let mut harness_owners = HashMap::new();
    for turn in &snapshot.turns {
        let p = turn.payload();
        let id = text(p, "id")?;
        let harness_id = p["harness_turn_id"].as_str();
        // Snapshot turns are newest first; preserve Python's first-match owner
        // while avoiding a scan of every turn for each retained pending item.
        if let Some(approval) = p["approval_id"].as_str() {
            approval_owners.entry(approval).or_insert(id);
        }
        harness_owners.entry(harness_id).or_insert(id);
        let finished_harness = harness_id
            .and_then(|id| harnesses.get(id))
            .map(|p| text(p, "status").map(terminal))
            .transpose()?
            .unwrap_or(false);
        if !terminal(text(p, "status")?) && !finished_harness {
            active_ids.insert(id);
            active_harnesses.insert(harness_id);
        }
    }
    let mut pending = Vec::new();
    for record in &snapshot.approvals {
        let p = record.payload();
        let owner = p["chat_turn_id"]
            .as_str()
            .filter(|id| !id.is_empty())
            .or_else(|| {
                p["id"]
                    .as_str()
                    .and_then(|id| approval_owners.get(id).copied())
            });
        if owner.is_some_and(|id| active_ids.contains(id))
            && p["status"] == "pending"
            && (p["expires_at"].is_null() || recorded_time(&p["expires_at"])? > now)
        {
            pending.push(json!({"id":p["id"],"turn_id":owner,"kind":"pending",
                "text":"Review the requested action","at":history_timestamp(&p["updated_at"])?,"message_id":null}));
        }
    }
    for record in &snapshot.questions {
        let p = record.payload();
        if p["status"] == "pending" && active_harnesses.contains(&p["harness_turn_id"].as_str()) {
            let owner = harness_owners.get(&p["harness_turn_id"].as_str()).copied();
            pending.push(json!({"id":p["id"],"turn_id":owner,"kind":"pending",
                "text":if p["contains_secret"] == true {Value::from("A secret answer is required")} else {p["prompt"].clone()},
                "at":history_timestamp(&p["updated_at"])?,"message_id":null}));
        }
    }
    pending.sort_by(|left, right| {
        left["at"]
            .as_str()
            .cmp(&right["at"].as_str())
            .then_with(|| left["id"].as_str().cmp(&right["id"].as_str()))
    });
    Ok(pending)
}

impl AssistantRecords {
    pub async fn catch_up(
        &self,
        session_id: &str,
        supplied_device: &str,
        authenticated_device: Option<&str>,
    ) -> Result<Value> {
        let session = self.get(Kind::Session, session_id).await?;
        let cursor = self
            .read_cursor(session_id, supplied_device, authenticated_device)
            .await?;
        let cursor_through = cursor
            .as_ref()
            .map(|p| recorded_time(&p.payload()["through_at"]))
            .transpose()?;
        let through = (self.clock)();
        let snapshot = self
            .store
            .catchup_snapshot(
                session_id,
                text(session.payload(), "engagement_id")?,
                cursor_through,
                through,
            )
            .await?;
        let mut items = Vec::new();
        for turn in snapshot.turns.iter().take(100) {
            let p = turn.payload();
            let status = text(p, "status")?;
            let unseen = match cursor_through {
                Some(cursor) => recorded_time(&p["updated_at"])? > cursor,
                None => false,
            };
            if unseen && matches!(status, "failed" | "interrupted" | "cancelled") {
                let message = snapshot
                    .sources
                    .get(text(p, "id")?)
                    .and_then(Option::as_ref);
                items.push(json!({"id":p["id"],"turn_id":p["id"],
                    "message_id":message.map(|m|m.payload()["id"].clone()),
                    "at":history_timestamp(&p["updated_at"])?,"kind":"failure",
                    "text":format!("Response {status}: {}",p["error"].as_str().filter(|s|!s.is_empty()).unwrap_or("Inspect the recorded response"))}));
            }
        }
        // Python calculates this only for an initialized cursor, after its
        // 101-row message window has been filtered for replacement markers.
        let mut truncated = false;
        if cursor.is_some() {
            let candidates: Vec<_> = snapshot
                .messages
                .iter()
                .filter(|m| !m.is_replaced_message())
                .collect();
            truncated = candidates.len() > 100 || snapshot.turns.len() > 100;
            for message in candidates.into_iter().take(100) {
                let p = message.payload();
                let outputs = p["citations"]
                    .as_array()
                    .is_some_and(|items| !items.is_empty())
                    || p["content_blocks"].as_array().is_some_and(|blocks| {
                        blocks.iter().any(|block| {
                            matches!(block["type"].as_str(), Some("artifact" | "image" | "code"))
                        })
                    })
                    || text(p, "content")?.contains("```")
                    || truthy(&p["metadata"]["tool_results"]);
                let prompt = snapshot
                    .prompts
                    .get(text(p, "id")?)
                    .map(String::as_str)
                    .unwrap_or("");
                if !outputs && !substantive(prompt) {
                    continue;
                }
                items.push(json!({"id":p["id"],"message_id":p["id"],"turn_id":p["metadata"]["chat_turn_id"],
                    "at":history_timestamp(&p["created_at"])?,"kind":if outputs {"results"} else {"completed"},
                    "text":text(p,"content")?.chars().take(240).collect::<String>()}));
            }
        }
        items.sort_by(|left, right| right["at"].as_str().cmp(&left["at"].as_str()));
        let pending = pending_notices(snapshot.pending, (self.clock)())?;
        let mut unseen_pending = Vec::new();
        if let Some(cursor) = cursor_through {
            for item in &pending {
                if recorded_time(&item["at"])? > cursor {
                    unseen_pending.push(item.clone());
                }
            }
        }
        truncated |= items.len() + unseen_pending.len() > 50;
        unseen_pending.extend(items);
        unseen_pending.truncate(50);
        Ok(
            json!({"revision":cursor.as_ref().map(|p|p.payload()["revision"].clone()).unwrap_or_else(||json!(0)),
            "initialized":cursor.is_some(),"through_at":timestamp(through,false),
            "items":unseen_pending,"pending":pending,"truncated":truncated}),
        )
    }

    pub async fn turn_summary(&self, session_id: &str, turn_id: &str) -> Result<Value> {
        self.get(Kind::Session, session_id).await?;
        let turn = self.get(Kind::Turn, turn_id).await?;
        let p = turn.payload();
        if p["session_id"] != session_id {
            return Err(Error::NotFound(
                "Response does not belong to this conversation",
            ));
        }
        let message = self.store.source_message(session_id, &turn).await?;
        Ok(
            json!({"id":p["id"],"status":p["status"],"error":p["error"],"model":p["model"],
            "created_at":history_timestamp(&p["created_at"])?,"updated_at":history_timestamp(&p["updated_at"])?,
            "message_id":message.map(|m|m.payload()["id"].clone())}),
        )
    }
}
