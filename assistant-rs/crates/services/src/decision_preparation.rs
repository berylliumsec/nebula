//! The source's passive operator-decision snapshot and exact prompt encoding.
//! No saved-context mutation or provider/turn authority is introduced here.
use crate::{AssistantRecords, Error, Result, execution_context::MAX_PREPARATION_BYTES};
use nebula_assistant_storage::entities::Error as StorageError;
use serde::{Deserialize, Deserializer, Serialize, de::Error as _};
use serde_json::{Number, Value, ser::Formatter, value::RawValue};
use std::io::{self, Write};

const PREFIX: &str = "\n\nOperator-saved context (authoritative outside derived summaries). Entries with kind=question remain unresolved until the operator explicitly supersedes or removes them; do not infer that model prose resolves them.\n";
const CONTEXT_FULL: &str =
    "Active operator context is too large. Supersede or remove decisions in Context before sending";

/// Declaration order is the Python snapshot's seven-field insertion order.
/// Revision remains an arbitrary precision JSON integer until a storage boundary.
/// Debug never emits saved text or source identities.
#[derive(Clone, Deserialize, Serialize)]
pub struct DecisionSnapshotEntry {
    id: String,
    #[serde(deserialize_with = "revision_number")]
    revision: Number,
    kind: String,
    text: String,
    scope: String,
    source_message_id: Option<String>,
    source_session_id: Option<String>,
}
// A Value deserializer may route large powers of ten through visit_f64 even
// with arbitrary_precision. Capture its JSON spelling before that conversion.
fn revision_number<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<Number, D::Error> {
    let raw = Box::<RawValue>::deserialize(deserializer)?;
    serde_json::from_str(raw.get()).map_err(D::Error::custom)
}
impl std::fmt::Debug for DecisionSnapshotEntry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("DecisionSnapshotEntry { [redacted] }")
    }
}
impl DecisionSnapshotEntry {
    pub fn to_value(&self) -> Result<Value> {
        Ok(serde_json::to_value(self)?)
    }
}

impl AssistantRecords {
    /// `None` is the actual not-yet-created conversation selector. An absent or
    /// empty project bypasses lookup before interpreting a session selector.
    pub async fn preparation_decision_snapshot(
        &self,
        session_id: Option<&str>,
        project_id: Option<&str>,
    ) -> Result<Vec<DecisionSnapshotEntry>> {
        let Some(project_id) = project_id.filter(|id| !id.is_empty()) else {
            return Ok(Vec::new());
        };
        let entries = self
            .store
            .preparation_decisions(session_id, project_id)
            .await
            .map_err(|error| match error {
                StorageError::RetainedModelValidation(report) => {
                    Error::RetainedModelValidation(report)
                }
                error => Error::Storage(error),
            })?;
        let active: Vec<_> = entries
            .iter()
            .filter(|entry| entry.payload()["status"] == "active")
            .collect();
        if active.len() > 100
            || active
                .iter()
                .map(|entry| {
                    entry.payload()["text"]
                        .as_str()
                        .map_or(0, |text| text.chars().count())
                })
                .sum::<usize>()
                > 40000
        {
            return Err(Error::Conflict(CONTEXT_FULL));
        }
        active
            .into_iter()
            .map(|entry| {
                let p = entry.payload();
                let text = |key: &str| {
                    p[key]
                        .as_str()
                        .map(str::to_owned)
                        .ok_or(Error::Storage(StorageError::CorruptEnvelope))
                };
                Ok(DecisionSnapshotEntry {
                    id: text("id")?,
                    revision: p["revision"]
                        .as_number()
                        .cloned()
                        .ok_or(Error::Storage(StorageError::CorruptEnvelope))?,
                    kind: text("kind")?,
                    text: text("text")?,
                    scope: text("scope")?,
                    source_message_id: p["source_message_id"].as_str().map(str::to_owned),
                    source_session_id: p["source_session_id"].as_str().map(str::to_owned),
                })
            })
            .collect()
    }
}

struct Spaced;
impl Formatter for Spaced {
    fn begin_array_value<W: ?Sized + Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }
    fn begin_object_key<W: ?Sized + Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }
    fn begin_object_value<W: ?Sized + Write>(&mut self, writer: &mut W) -> io::Result<()> {
        writer.write_all(b": ")
    }
}
struct BoundedOutput(Vec<u8>);
impl Write for BoundedOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > MAX_PREPARATION_BYTES.saturating_sub(self.0.len()) {
            return Err(io::Error::other("operator context instruction byte limit"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Python json.dumps(..., ensure_ascii=False), including comma/colon spaces and
/// the exact seven-field snapshot order. An empty snapshot adds no instructions.
/// The source character budget applies at snapshot time; this independently
/// bounds serialized output bytes for already-prepared or deserialized entries.
pub fn decision_instructions(snapshot: &[DecisionSnapshotEntry]) -> Result<String> {
    if snapshot.is_empty() {
        return Ok(String::new());
    }
    if snapshot.len() > 100 {
        return Err(Error::Conflict(CONTEXT_FULL));
    }
    let mut output = BoundedOutput(PREFIX.as_bytes().to_vec());
    let mut serializer = serde_json::Serializer::with_formatter(&mut output, Spaced);
    snapshot.serialize(&mut serializer).map_err(|_| {
        Error::Invalid("Operator context instructions exceed the preparation byte limit")
    })?;
    String::from_utf8(output.0)
        .map_err(|_| Error::Invalid("Operator context instructions are not UTF-8"))
}
