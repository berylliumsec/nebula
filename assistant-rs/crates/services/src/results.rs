//! Projections of retained output and recorded context. These methods never run
//! tools, extract new documents, or synthesize artifacts from model claims.
use crate::{AssistantRecords, Error, Result, artifact_preview::ArtifactPreview};
use nebula_assistant_domain::records::AssistantKind as Kind;
use nebula_assistant_storage::entities::Error as StorageError;
use serde_json::{Value, json};
use std::{collections::HashMap, sync::LazyLock};

#[derive(Clone, Copy, Debug)]
pub struct ResultsQuery {
    pub offset: u64,
    pub limit: u32,
}

static POLICIES: LazyLock<std::result::Result<Value, serde_json::Error>> = LazyLock::new(|| {
    serde_json::from_str::<Value>(include_str!("../../../compatibility/python-results.json"))
        .map(|fixture| fixture["policies"].clone())
});
fn policy(name: &str) -> Result<&'static str> {
    POLICIES
        .as_ref()
        .ok()
        .and_then(|value| value[name].as_str())
        .ok_or(Error::Unavailable(
            "The retained Assistant policy snapshot is unavailable",
        ))
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

// Charge encoded bytes before an item enters the retained response. Encoding
// uses a counting writer, so budget checks never allocate another copy of text.
struct Items {
    values: Vec<Value>,
    bytes: usize,
}
impl Items {
    fn new() -> Self {
        Self {
            values: Vec::new(),
            bytes: 256,
        }
    }
    fn push(&mut self, value: Value) -> Result<()> {
        struct Counter(usize);
        impl std::io::Write for Counter {
            fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
                self.0 = self.0.saturating_add(bytes.len());
                if self.0 > 16 * 1024 * 1024 {
                    return Err(std::io::Error::other(
                        "Result expansion exceeds its byte bound",
                    ));
                }
                Ok(bytes.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        if self.values.len() >= 10_000 {
            return Err(StorageError::ReadLimit.into());
        }
        let mut count = Counter(self.bytes + 1);
        serde_json::to_writer(&mut count, &value).map_err(|_| StorageError::ReadLimit)?;
        self.bytes = count.0;
        self.values.push(value);
        Ok(())
    }
}

fn fences(mut remaining: &str) -> impl Iterator<Item = (&str, &str)> {
    std::iter::from_fn(move || {
        let start = remaining.find("```")? + 3;
        let newline = remaining[start..].find('\n')? + start;
        let end = remaining[newline + 1..].find("```")? + newline + 1;
        let result = (&remaining[start..newline], &remaining[newline + 1..end]);
        remaining = &remaining[end + 3..];
        Some(result)
    })
}

impl AssistantRecords {
    pub async fn results(
        &self,
        session_id: &str,
        query: ResultsQuery,
        preview: Option<&ArtifactPreview>,
    ) -> Result<Value> {
        if !(1..=100).contains(&query.limit) || query.offset > i64::MAX as u64 {
            return Err(Error::Invalid("Results page exceeds its field bounds"));
        }
        let session = self.get(Kind::Session, session_id).await?;
        let snapshot = self
            .store
            .results_snapshot(
                session_id,
                session.payload()["engagement_id"]
                    .as_str()
                    .ok_or(StorageError::CorruptEnvelope)?,
                query.offset,
                query.limit,
                preview.is_some(),
            )
            .await?;
        // Resolve each retained call once, preserving the source call order in
        // every message bucket. Large pages do not rescan all calls per message.
        let mut calls_by_message: HashMap<&str, Vec<&Value>> = HashMap::new();
        for call in &snapshot.calls {
            let call = call.payload();
            let Some(turn_id) = call["chat_turn_id"].as_str().filter(|id| !id.is_empty()) else {
                continue;
            };
            if let Some(message_id) = snapshot
                .call_turns
                .get(turn_id)
                .and_then(|turn| turn.payload()["final_message_id"].as_str())
            {
                calls_by_message.entry(message_id).or_default().push(call);
            }
        }
        let mut items = Items::new();
        for message in &snapshot.messages {
            let p = message.payload();
            if message.is_replaced_message() || p["role"] != "assistant" {
                continue;
            }
            let id = p["id"].as_str().ok_or(StorageError::CorruptEnvelope)?;
            for (index, block) in p["content_blocks"]
                .as_array()
                .ok_or(StorageError::CorruptEnvelope)?
                .iter()
                .enumerate()
            {
                if matches!(block["type"].as_str(), Some("artifact" | "image" | "code"))
                    && (truthy(&block["artifact_id"]) || block["type"] == "code")
                {
                    let label = block["alt"]
                        .as_str()
                        .filter(|text| !text.is_empty())
                        .or_else(|| block["language"].as_str().filter(|text| !text.is_empty()))
                        .unwrap_or("Retained output");
                    items.push(json!({"id":format!("{id}-block-{index}"),"message_id":id,
                        "kind":block["type"],"label":label,"artifact_id":block["artifact_id"],"text":block["text"]}))?;
                }
            }
            for (index, (language, content)) in
                fences(p["content"].as_str().ok_or(StorageError::CorruptEnvelope)?).enumerate()
            {
                items.push(json!({"id":format!("{id}-code-{index}"),"message_id":id,"kind":"code",
                    "label":if language.is_empty(){"Code excerpt"}else{language},"text":content,"artifact_id":null}))?;
            }
            // The snapshot transaction has committed before any file work.
            if let Some(preview) = preview {
                for artifact in snapshot.diffs.get(id).into_iter().flatten() {
                    let artifact = artifact.payload();
                    let content = preview.read_diff(artifact).await?;
                    items.push(json!({"id":artifact["id"],"message_id":id,"kind":"file_change",
                        "label":"Recorded file changes (bounded preview)","text":content,"artifact_id":artifact["id"]}))?;
                }
            }
            for citation in p["citations"]
                .as_array()
                .ok_or(StorageError::CorruptEnvelope)?
            {
                items.push(json!({"id":format!("{id}-{}",citation["chunk_id"].as_str().ok_or(StorageError::CorruptEnvelope)?),
                    "message_id":id,"kind":"citation","label":citation["name"],"text":citation["excerpt"],"source_id":citation["source_id"]}))?;
            }
            for call in calls_by_message.get(id).into_iter().flatten() {
                items.push(json!({"id":call["id"],"message_id":id,"kind":"tool","label":call["tool_name"],
                    "tool_call_id":call["id"],"artifact_id":call["result_artifact_id"],"status":call["status"],"text":call["error"]}))?;
            }
        }
        Ok(json!({"items":items.values,"next_offset":snapshot.next_offset}))
    }

    pub async fn context_sources(&self, session_id: &str, offset: u64) -> Result<Value> {
        if offset > i64::MAX as u64 {
            return Err(Error::Invalid(
                "Recorded context page exceeds its field bounds",
            ));
        }
        let session = self.get(Kind::Session, session_id).await?;
        let page = self
            .store
            .context_sources_page(
                session_id,
                session.payload()["engagement_id"]
                    .as_str()
                    .ok_or(StorageError::CorruptEnvelope)?,
                offset,
            )
            .await?;
        let mut items = Items::new();
        for message in page.records {
            let p = message.payload();
            if !message.is_replaced_message()
                && (truthy(&p["metadata"]["context_attachments"])
                    || truthy(&p["metadata"]["operator_decisions"]))
            {
                let empty = json!([]);
                items.push(json!({"message_id":p["id"],"sequence":p["sequence"],
                    "attachments":p["metadata"].get("context_attachments").unwrap_or(&empty),
                    "operator_decisions":p["metadata"].get("operator_decisions").unwrap_or(&empty)}))?;
            }
        }
        let provider = session.payload()["backend"] == "provider";
        let instructions = if provider {
            Some(policy(
                if truthy(&session.payload()["metadata"]["tools_enabled"]) {
                    "tools_enabled"
                } else {
                    "text_only"
                },
            )?)
        } else {
            None
        };
        let note = policy(if provider {
            "instruction_note_provider"
        } else {
            "instruction_note_harness"
        })?;
        let response = json!({"items":items.values,"next_offset":page.next_offset,"core_instructions":instructions,"instruction_note":note});
        // Policy text participates in the same response budget as recorded data.
        let mut final_budget = Items::new();
        final_budget.push(response)?;
        Ok(final_budget.values.pop().unwrap())
    }
}
