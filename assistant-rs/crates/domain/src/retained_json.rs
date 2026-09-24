//! Preserve opaque callback dictionary order while repairing another recorded
//! effect. The validated model owns values; raw fragments only retain spelling.
use crate::records::{MAX_RECORD_BYTES, RecordError};
use serde_json::{Value, value::RawValue};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, VecDeque},
    io::Write,
};

struct Output(Vec<u8>);
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.0.len().saturating_add(bytes.len()) > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("retained record exceeds byte limit"));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
impl Output {
    fn bytes(&mut self, bytes: &[u8]) -> Result<(), RecordError> {
        self.write_all(bytes).map_err(|_| RecordError::TooLarge)
    }
    fn json<T: serde::Serialize>(&mut self, value: &T) -> Result<(), RecordError> {
        serde_json::to_writer(self, value).map_err(|_| RecordError::TooLarge)
    }
}
struct HashWriter(Sha256);
impl Write for HashWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn fingerprint(value: &Value) -> Result<[u8; 32], RecordError> {
    let mut writer = HashWriter(Sha256::new());
    serde_json::to_writer(&mut writer, value).map_err(|_| RecordError::Json)?;
    Ok(writer.0.finalize().into())
}

/// Retain unchanged fragments only when they still equal the validated value.
/// An unchanged history entry may move after recovery sorts provider steps;
/// identify it by content, then verify equality after the hash lookup. This
/// avoids a quadratic scan and prevents a digest collision from changing data.
pub fn repair_turn_json(original: &str, next: &Value) -> Result<String, RecordError> {
    if original.len() > MAX_RECORD_BYTES {
        return Err(RecordError::TooLarge);
    }
    let before: Value = serde_json::from_str(original).map_err(|_| RecordError::Json)?;
    let raw: HashMap<String, &RawValue> =
        serde_json::from_str(original).map_err(|_| RecordError::Json)?;
    let fields = next.as_object().ok_or(RecordError::Json)?;
    let mut output = Output(Vec::new());
    output.bytes(b"{")?;
    for (index, (key, value)) in fields.iter().enumerate() {
        if index != 0 {
            output.bytes(b",")?;
        }
        output.json(key)?;
        output.bytes(b":")?;
        if before.get(key) == Some(value) {
            output.bytes(raw.get(key).ok_or(RecordError::Json)?.get().as_bytes())?;
        } else if key == "tool_history" && before[key].is_array() {
            let entries = value.as_array().ok_or(RecordError::Json)?;
            let old = before[key].as_array().ok_or(RecordError::Json)?;
            if entries.len() > 10_000 || old.len() > 10_000 {
                return Err(RecordError::TooLarge);
            }
            let old_raw: Vec<&RawValue> =
                serde_json::from_str(raw.get(key).ok_or(RecordError::Json)?.get())
                    .map_err(|_| RecordError::Json)?;
            let mut candidates: HashMap<[u8; 32], VecDeque<usize>> = HashMap::new();
            for (i, entry) in old.iter().enumerate() {
                candidates
                    .entry(fingerprint(entry)?)
                    .or_default()
                    .push_back(i);
            }
            output.bytes(b"[")?;
            for (i, entry) in entries.iter().enumerate() {
                if i != 0 {
                    output.bytes(b",")?;
                }
                // Equal values may have distinct opaque dictionary spelling;
                // consume each occurrence in stable source order.
                if let Some(previous) = candidates.get_mut(&fingerprint(entry)?).and_then(|ids| {
                    ids.iter()
                        .position(|&j| old[j] == *entry)
                        .and_then(|position| ids.remove(position))
                }) {
                    output.bytes(old_raw[previous].get().as_bytes())?;
                } else {
                    output.json(entry)?;
                }
            }
            output.bytes(b"]")?;
        } else {
            output.json(value)?;
        }
    }
    output.bytes(b"}")?;
    String::from_utf8(output.0).map_err(|_| RecordError::Json)
}
