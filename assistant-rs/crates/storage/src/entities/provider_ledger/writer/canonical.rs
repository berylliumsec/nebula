//! Bounded, lexical-number-preserving canonical JSON for local command identity.
use serde::{
    Deserializer as _,
    de::{MapAccess, SeqAccess, Visitor},
};
use serde_json::value::RawValue;
use std::{collections::HashSet, fmt, io::Write};

pub const MAX_BYTES: usize = 1024 * 1024;
const MAX_NODES: usize = 10_000;
const MAX_DEPTH: usize = 64;
const MAX_SCANNED: usize = 16 * 1024 * 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum Error {
    #[error("invalid canonical command JSON")]
    Invalid,
    #[error("canonical command exceeds its resource bound")]
    Capacity,
}

#[derive(Clone)]
pub struct CanonicalJson {
    bytes: std::sync::Arc<[u8]>,
}
impl fmt::Debug for CanonicalJson {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("CanonicalJson")
            .field("bytes", &self.bytes.len())
            .finish()
    }
}
impl CanonicalJson {
    pub fn from_raw(raw: &str) -> Result<Self, Error> {
        if raw.len() > MAX_BYTES {
            return Err(Error::Capacity);
        }
        let value: &RawValue = serde_json::from_str(raw).map_err(|_| Error::Invalid)?;
        let mut budget = Budget {
            nodes: 0,
            scanned: 0,
        };
        let mut out = Buffer::default();
        canonical(value, 0, &mut budget, &mut out)?;
        Ok(Self {
            bytes: out.bytes.into(),
        })
    }
    pub fn from_value(value: &impl serde::Serialize) -> Result<Self, Error> {
        let mut raw = Buffer::default();
        serde_json::to_writer(&mut raw, value).map_err(|_| {
            if raw.full {
                Error::Capacity
            } else {
                Error::Invalid
            }
        })?;
        let raw = std::str::from_utf8(&raw.bytes).map_err(|_| Error::Invalid)?;
        Self::from_raw(raw)
    }
    pub fn bytes(&self) -> &[u8] {
        &self.bytes
    }
    pub fn text(&self) -> &str {
        std::str::from_utf8(&self.bytes).expect("canonical UTF-8")
    }
}
struct Budget {
    nodes: usize,
    scanned: usize,
}
#[derive(Default)]
struct Buffer {
    bytes: Vec<u8>,
    full: bool,
}
impl Write for Buffer {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > MAX_BYTES.saturating_sub(self.bytes.len()) {
            self.full = true;
            return Err(std::io::Error::other("canonical byte limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn emit(out: &mut Buffer, bytes: &[u8]) -> Result<(), Error> {
    out.write_all(bytes).map_err(|_| Error::Capacity)
}
fn canonical(
    raw: &RawValue,
    depth: usize,
    budget: &mut Budget,
    out: &mut Buffer,
) -> Result<(), Error> {
    budget.nodes += 1;
    budget.scanned = budget
        .scanned
        .checked_add(raw.get().len())
        .ok_or(Error::Capacity)?;
    if depth > MAX_DEPTH || budget.nodes > MAX_NODES || budget.scanned > MAX_SCANNED {
        return Err(Error::Capacity);
    }
    let text = raw.get();
    match text.as_bytes().first() {
        Some(b'{') => {
            let mut d = serde_json::Deserializer::from_str(text);
            let mut full = false;
            let mut fields = d.deserialize_map(Object { full: &mut full }).map_err(|_| {
                if full {
                    Error::Capacity
                } else {
                    Error::Invalid
                }
            })?;
            d.end().map_err(|_| Error::Invalid)?;
            fields.sort_unstable_by(|a, b| a.0.cmp(&b.0));
            emit(out, b"{")?;
            for (index, (key, value)) in fields.into_iter().enumerate() {
                if index > 0 {
                    emit(out, b",")?;
                }
                serde_json::to_writer(&mut *out, &key).map_err(|_| {
                    if out.full {
                        Error::Capacity
                    } else {
                        Error::Invalid
                    }
                })?;
                emit(out, b":")?;
                canonical(value, depth + 1, budget, out)?;
            }
            emit(out, b"}")
        }
        Some(b'[') => {
            let mut d = serde_json::Deserializer::from_str(text);
            let mut full = false;
            let values = d.deserialize_seq(Array { full: &mut full }).map_err(|_| {
                if full {
                    Error::Capacity
                } else {
                    Error::Invalid
                }
            })?;
            d.end().map_err(|_| Error::Invalid)?;
            emit(out, b"[")?;
            for (index, value) in values.into_iter().enumerate() {
                if index > 0 {
                    emit(out, b",")?;
                }
                canonical(value, depth + 1, budget, out)?;
            }
            emit(out, b"]")
        }
        Some(b'"') => {
            let value: String = serde_json::from_str(text).map_err(|_| Error::Invalid)?;
            serde_json::to_writer(&mut *out, &value).map_err(|_| {
                if out.full {
                    Error::Capacity
                } else {
                    Error::Invalid
                }
            })
        }
        Some(b'-' | b'0'..=b'9' | b't' | b'f' | b'n') => emit(out, text.as_bytes()),
        _ => Err(Error::Invalid),
    }
}
struct Object<'a> {
    full: &'a mut bool,
}
impl<'de> Visitor<'de> for Object<'_> {
    type Value = Vec<(String, &'de RawValue)>;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("bounded object")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut fields = Vec::new();
        let mut keys = HashSet::new();
        while let Some((key, value)) = map.next_entry::<String, &RawValue>()? {
            if !keys.insert(key.clone()) {
                return Err(serde::de::Error::custom("duplicate key"));
            }
            if fields.len() >= MAX_NODES {
                *self.full = true;
                return Err(serde::de::Error::custom("object bound"));
            }
            fields.push((key, value));
        }
        Ok(fields)
    }
}
struct Array<'a> {
    full: &'a mut bool,
}
impl<'de> Visitor<'de> for Array<'_> {
    type Value = Vec<&'de RawValue>;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("bounded array")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let mut values = Vec::new();
        while let Some(value) = seq.next_element::<&RawValue>()? {
            if values.len() >= MAX_NODES {
                *self.full = true;
                return Err(serde::de::Error::custom("array bound"));
            }
            values.push(value);
        }
        Ok(values)
    }
}
