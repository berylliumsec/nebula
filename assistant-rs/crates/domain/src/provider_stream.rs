//! Immutable bytes for the provider replay boundary, not a publication grant.
//!
//! The writer assigns a sequence and commits these bytes before a subscriber
//! may receive them. Terminal snapshots remain unsequenced. No allocator/RSS or
//! aggregate memory guarantee is implied; the owner must retain byte credits.
use serde::{
    Deserializer, Serialize,
    de::{MapAccess, Visitor},
};
use serde_json::value::RawValue;
use sha2::{Digest, Sha256};
use std::{collections::HashSet, fmt, io::Write};

pub const MAX_SEQUENCE: u64 = 9_007_199_254_740_991;
pub const MAX_FRAME_BYTES: usize = 1024 * 1024;
const MAX_FIELDS: usize = 128;

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum Error {
    #[error("provider event exceeds its encoded byte or field limit")]
    Capacity,
    #[error("provider event is not an unsequenced object with a matching type")]
    Payload,
    #[error("provider event name is invalid")]
    EventType,
    #[error("provider event sequence is outside the supported cursor range")]
    Sequence,
    #[error("only terminal provider events can be encoded as snapshots")]
    Snapshot,
}

/// Validated unsequenced data. Raw values keep their existing Unicode, number
/// spelling and key order; no parse/render cycle can change a replay identity.
pub struct Draft {
    event_type: String,
    json: Vec<u8>,
}

impl Draft {
    /// The supplied serializer owns public-field selection. In particular,
    /// credentials and private receipts must never be passed to this boundary.
    pub fn from_payload(event_type: &str, payload: &impl Serialize) -> Result<Self, Error> {
        valid_type(event_type)?;
        let mut writer = BoundedWriter::new(MAX_FRAME_BYTES);
        serde_json::to_writer(&mut writer, payload).map_err(|_| {
            if writer.full {
                Error::Capacity
            } else {
                Error::Payload
            }
        })?;
        Self::from_bytes(event_type, writer.bytes)
    }

    pub fn from_raw(event_type: &str, raw: &str) -> Result<Self, Error> {
        valid_type(event_type)?;
        if raw.len() > MAX_FRAME_BYTES {
            return Err(Error::Capacity);
        }
        Self::from_bytes(event_type, raw.as_bytes().to_vec())
    }

    fn from_bytes(event_type: &str, mut bytes: Vec<u8>) -> Result<Self, Error> {
        let mut decoder = serde_json::Deserializer::from_slice(&bytes);
        let mut capacity = false;
        let fields = decoder
            .deserialize_map(Fields {
                capacity: &mut capacity,
            })
            .map_err(|_| {
                if capacity {
                    Error::Capacity
                } else {
                    Error::Payload
                }
            })?;
        decoder.end().map_err(|_| Error::Payload)?;
        if fields.kind != event_type {
            return Err(Error::Payload);
        }
        // Top-level space is immaterial to JSON but would make appending the
        // writer-owned sequence ambiguous. Preserve every byte within the object.
        let start = bytes
            .iter()
            .position(|b| !b.is_ascii_whitespace())
            .ok_or(Error::Payload)?;
        let end = bytes
            .iter()
            .rposition(|b| !b.is_ascii_whitespace())
            .ok_or(Error::Payload)?
            + 1;
        if start != 0 {
            bytes.copy_within(start..end, 0);
        }
        bytes.truncate(end - start);
        Ok(Self {
            event_type: event_type.into(),
            json: bytes,
        })
    }

    pub fn encode(self, sequence: u64) -> Result<Encoded, Error> {
        if sequence == 0 || sequence > MAX_SEQUENCE {
            return Err(Error::Sequence);
        }
        self.finish(Some(sequence))
    }

    /// A current authoritative terminal record may be returned without a cursor
    /// even when a reconnect cursor exceeds the durable stream head. This method
    /// does not establish that the caller actually holds that authority.
    pub fn terminal_snapshot(self) -> Result<Encoded, Error> {
        if !matches!(
            self.event_type.as_str(),
            "done" | "error" | "cancelled" | "interrupted"
        ) {
            return Err(Error::Snapshot);
        }
        self.finish(None)
    }

    fn finish(mut self, sequence: Option<u64>) -> Result<Encoded, Error> {
        if let Some(sequence) = sequence {
            self.json.pop(); // from_bytes guarantees the final object delimiter.
            let mut writer = BoundedWriter {
                bytes: self.json,
                limit: MAX_FRAME_BYTES,
                full: false,
            };
            write!(writer, ",\"sequence\":{sequence}}}").map_err(|_| Error::Capacity)?;
            self.json = writer.bytes;
        }
        let mut sse = BoundedWriter::new(MAX_FRAME_BYTES);
        if let Some(sequence) = sequence {
            writeln!(sse, "id: {sequence}").map_err(|_| Error::Capacity)?;
        }
        writeln!(sse, "event: {}", self.event_type).map_err(|_| Error::Capacity)?;
        sse.write_all(b"data: ").map_err(|_| Error::Capacity)?;
        // A raw newline outside a JSON string would split the SSE data field.
        // Serializers produce compact JSON; raw callers must do the same.
        if self.json.iter().any(|b| matches!(b, b'\n' | b'\r')) {
            return Err(Error::Payload);
        }
        sse.write_all(&self.json).map_err(|_| Error::Capacity)?;
        sse.write_all(b"\n\n").map_err(|_| Error::Capacity)?;
        let content_sha256 = digest_parts(&[
            b"nebula.assistant-provider-event-content/v1",
            self.event_type.as_bytes(),
            &self.json,
        ]);
        Ok(Encoded {
            event_type: self.event_type,
            sequence,
            json: self.json,
            sse: sse.bytes,
            content_sha256,
        })
    }
}

/// Store and replay `sse_bytes()` verbatim. The content digest is a local byte
/// identity, not a completion receipt, provider signature or tamper-proof log.
pub struct Encoded {
    event_type: String,
    sequence: Option<u64>,
    json: Vec<u8>,
    sse: Vec<u8>,
    content_sha256: [u8; 32],
}
impl Encoded {
    pub fn event_type(&self) -> &str {
        &self.event_type
    }
    pub fn sequence(&self) -> Option<u64> {
        self.sequence
    }
    pub fn json_bytes(&self) -> &[u8] {
        &self.json
    }
    pub fn sse_bytes(&self) -> &[u8] {
        &self.sse
    }
    pub fn content_sha256(&self) -> &[u8; 32] {
        &self.content_sha256
    }
    pub fn retained_bytes(&self) -> usize {
        self.event_type.len() + self.json.len() + self.sse.len()
    }
}
impl fmt::Debug for Draft {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProviderEventDraft")
            .field("type", &self.event_type)
            .field("bytes", &self.json.len())
            .finish()
    }
}
impl fmt::Debug for Encoded {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProviderEvent")
            .field("type", &self.event_type)
            .field("sequence", &self.sequence)
            .field("bytes", &self.retained_bytes())
            .finish()
    }
}

/// Length-prefix every component, including the caller's versioned domain
/// separator, so concatenation cannot create an ambiguous receipt identity.
pub fn digest_parts(parts: &[&[u8]]) -> [u8; 32] {
    let mut hash = Sha256::new();
    for part in parts {
        hash.update((part.len() as u64).to_be_bytes());
        hash.update(part);
    }
    hash.finalize().into()
}

fn valid_type(value: &str) -> Result<(), Error> {
    if value.is_empty()
        || value.len() > 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        || !value.as_bytes()[0].is_ascii_lowercase()
    {
        return Err(Error::EventType);
    }
    Ok(())
}

struct ParsedFields {
    kind: String,
}
struct Fields<'a> {
    capacity: &'a mut bool,
}
impl<'de> Visitor<'de> for Fields<'_> {
    type Value = ParsedFields;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("an unsequenced event object")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut keys = HashSet::new();
        let mut kind = None;
        while let Some(key) = map.next_key::<String>()? {
            if keys.len() >= MAX_FIELDS || key.len() > 256 {
                *self.capacity = true;
                return Err(serde::de::Error::custom("event field limit"));
            }
            if !keys.insert(key.clone()) || matches!(key.as_str(), "sequence" | "id") {
                return Err(serde::de::Error::custom("invalid event fields"));
            }
            let value = map.next_value::<&'de RawValue>()?;
            if key == "type" {
                kind = Some(
                    serde_json::from_str::<String>(value.get())
                        .map_err(serde::de::Error::custom)?,
                );
            }
        }
        Ok(ParsedFields {
            kind: kind.ok_or_else(|| serde::de::Error::custom("missing event type"))?,
        })
    }
}

struct BoundedWriter {
    bytes: Vec<u8>,
    limit: usize,
    full: bool,
}
impl BoundedWriter {
    fn new(limit: usize) -> Self {
        Self {
            bytes: Vec::new(),
            limit,
            full: false,
        }
    }
}
impl Write for BoundedWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.bytes.len()) {
            self.full = true;
            return Err(std::io::Error::other("event byte limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
