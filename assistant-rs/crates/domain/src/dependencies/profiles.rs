//! Immutable Engagement/Provider hydration. No filesystem, secret or network I/O.
use super::{DependencyEnvironment, MAX_RECORD_BYTES, RecordError};
use crate::model_validation::dependency_scalar;
use chrono::{DateTime, SecondsFormat};
use serde::{
    Deserialize, Deserializer,
    de::{MapAccess, Visitor},
};
use serde_json::{Map, Value, value::RawValue};
use std::{
    collections::{HashMap, HashSet},
    io::Write,
};

type Result<T> = std::result::Result<T, RecordError>;
const MAX_ENTRIES: usize = 10_000;
const ENTITY: &[&str] = &["id", "created_at", "updated_at", "revision"];
const ENGAGEMENT: &[&str] = &[
    "name",
    "description",
    "status",
    "scope_policy_id",
    "client_name",
    "owner_id",
    "tags",
    "workspace_path",
    "metadata",
];
const PROVIDER: &[&str] = &[
    "name",
    "provider_type",
    "endpoint",
    "enabled",
    "is_local",
    "secret_ref",
    "model_allowlist",
    "capabilities",
    "capability_verifications",
    "privacy",
    "metadata",
];
const CAPABILITIES: &[&str] = &[
    "streaming",
    "cancellation",
    "tool_calling",
    "strict_structured_output",
    "parallel_tool_calls",
    "vision",
    "documents",
    "audio",
    "embeddings",
    "reasoning_controls",
];
const PRIVACY: &[&str] = &[
    "local_only",
    "retention",
    "residency",
    "permits_sensitive_data",
    "auto_share_tool_results",
];
const VERIFICATION: &[&str] = &[
    "model",
    "status",
    "checked_at",
    "contract_version",
    "failure_detail",
];

fn invalid() -> RecordError {
    RecordError::Invariant("retained project or provider is invalid")
}
struct Size(usize);
impl Write for Size {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("dependency byte bound"));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn bounded(value: &Value) -> Result<()> {
    serde_json::to_writer(&mut Size(0), value).map_err(|_| RecordError::TooLarge)
}
fn encoded_size(value: &impl serde::Serialize) -> Result<usize> {
    let mut size = Size(0);
    serde_json::to_writer(&mut size, value).map_err(|_| RecordError::TooLarge)?;
    Ok(size.0)
}
fn insert_bounded(
    output: &mut Map<String, Value>,
    bytes: &mut usize,
    key: String,
    value: Value,
) -> Result<()> {
    let previous = output.get(&key).map(encoded_size).transpose()?.unwrap_or(0);
    let entry = if previous != 0 {
        0
    } else {
        encoded_size(&key)?.saturating_add(1 + usize::from(!output.is_empty()))
    };
    let next = bytes
        .saturating_sub(previous)
        .saturating_add(entry)
        .saturating_add(encoded_size(&value)?);
    if next > MAX_RECORD_BYTES {
        return Err(RecordError::TooLarge);
    }
    output.insert(key, value);
    *bytes = next;
    Ok(())
}

/// Python json.loads keeps first raw-key position and only the final raw value.
/// Typed dictionary key stripping is a later step and must validate every value
/// even when two distinct raw keys normalize to the same typed key.
struct Entries<'a>(Vec<(String, &'a RawValue)>);
impl<'de> Deserialize<'de> for Entries<'de> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct EntryVisitor;
        impl<'de> Visitor<'de> for EntryVisitor {
            type Value = Entries<'de>;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("retained configuration object")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut access: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut entries: Vec<(String, &RawValue)> = Vec::new();
                let mut positions: HashMap<String, usize> = HashMap::new();
                let mut count = 0usize;
                let mut key_bytes = 0usize;
                while let Some((key, value)) = access.next_entry::<String, &RawValue>()? {
                    count += 1;
                    if count > MAX_ENTRIES {
                        return Err(serde::de::Error::custom("dependency entry bound"));
                    }
                    if let Some(&index) = positions.get(&key) {
                        entries[index].1 = value;
                    } else {
                        key_bytes = key_bytes.saturating_add(key.len().saturating_mul(2));
                        if key_bytes > MAX_RECORD_BYTES {
                            return Err(serde::de::Error::custom("dependency key bound"));
                        }
                        positions.insert(key.clone(), entries.len());
                        entries.push((key, value));
                    }
                }
                Ok(Entries(entries))
            }
        }
        deserializer.deserialize_map(EntryVisitor)
    }
}
struct Normalized {
    value: Value,
    valid: bool,
}
impl Normalized {
    fn invalid(value: Value) -> Self {
        Self {
            value,
            valid: false,
        }
    }
}
struct Hydrator<'a, 'b> {
    root: &'a Value,
    environment: Option<&'b mut dyn DependencyEnvironment>,
    entries: usize,
}
impl Hydrator<'_, '_> {
    fn charge(&mut self, count: usize) -> Result<()> {
        self.entries = self.entries.saturating_add(count);
        if self.entries > MAX_ENTRIES {
            Err(RecordError::TooLarge)
        } else {
            Ok(())
        }
    }
    fn schema<'a>(&'a self, schema: &'a Value) -> Result<&'a Value> {
        if let Some(reference) = schema["$ref"].as_str() {
            self.root
                .pointer(reference.strip_prefix('#').ok_or(RecordError::Schema)?)
                .ok_or(RecordError::Schema)
        } else {
            Ok(schema)
        }
    }
    fn normalize(&mut self, schema: &Value, raw: &RawValue) -> Result<Normalized> {
        // Schemas are small immutable contracts; cloning one property releases
        // its borrow before contextual child hydration can sample trusted time.
        let schema = self.schema(schema)?.clone();
        let value: Value = serde_json::from_str(raw.get()).map_err(|_| RecordError::Json)?;
        if let Some(branches) = schema["anyOf"].as_array() {
            if value.is_null() {
                return Ok(Normalized {
                    value,
                    valid: branches.iter().any(|v| v["type"] == "null"),
                });
            }
            let branch = branches
                .iter()
                .find(|s| s["type"] != "null")
                .ok_or(RecordError::Schema)?;
            return self.normalize(branch, raw);
        }
        match schema["type"].as_str() {
            Some("object") => {
                if !value.is_object() {
                    return Ok(Normalized::invalid(value));
                }
                drop(value);
                let Entries(entries) =
                    serde_json::from_str(raw.get()).map_err(|_| RecordError::TooLarge)?;
                self.charge(entries.len().saturating_add(1))?;
                if schema["properties"].is_object() {
                    return self.model(&schema, entries);
                }
                let additional = schema
                    .get("additionalProperties")
                    .unwrap_or(&Value::Bool(true));
                let mut result = Map::new();
                let mut bytes = 2usize;
                let mut valid = true;
                for (key, raw) in entries {
                    let normalized = self.normalize(additional, raw)?;
                    valid &= normalized.valid;
                    insert_bounded(
                        &mut result,
                        &mut bytes,
                        key.trim().to_owned(),
                        normalized.value,
                    )?;
                }
                Ok(Normalized {
                    value: result.into(),
                    valid,
                })
            }
            Some("array") => {
                let Some(values) = value.as_array() else {
                    return Ok(Normalized::invalid(value));
                };
                self.charge(values.len())?;
                let mut valid = schema["maxItems"]
                    .as_u64()
                    .is_none_or(|n| values.len() as u64 <= n)
                    && schema["minItems"]
                        .as_u64()
                        .is_none_or(|n| values.len() as u64 >= n);
                drop(value);
                let entries: Vec<&RawValue> =
                    serde_json::from_str(raw.get()).map_err(|_| RecordError::Json)?;
                let mut result = Vec::with_capacity(entries.len());
                let mut bytes = 2usize;
                for raw in entries {
                    let normalized = self.normalize(&schema["items"], raw)?;
                    valid &= normalized.valid;
                    bytes = bytes
                        .saturating_add(encoded_size(&normalized.value)?)
                        .saturating_add(usize::from(!result.is_empty()));
                    if bytes > MAX_RECORD_BYTES {
                        return Err(RecordError::TooLarge);
                    }
                    result.push(normalized.value);
                }
                Ok(Normalized {
                    value: result.into(),
                    valid,
                })
            }
            Some("string" | "boolean" | "integer") => {
                Ok(match dependency_scalar(&schema, &value) {
                    Ok(value) => Normalized { value, valid: true },
                    Err(_) => Normalized::invalid(value),
                })
            }
            _ => Ok(Normalized { value, valid: true }),
        }
    }
    fn model(&mut self, schema: &Value, entries: Vec<(String, &RawValue)>) -> Result<Normalized> {
        let name = schema["title"].as_str().ok_or(RecordError::Schema)?;
        let (base, fields): (&[&str], &[&str]) = match name {
            "Engagement" => (ENTITY, ENGAGEMENT),
            "ProviderProfile" => (ENTITY, PROVIDER),
            "ModelCapabilities" => (&[], CAPABILITIES),
            "ProviderPrivacy" => (&[], PRIVACY),
            "ProviderCapabilityVerification" => (&[], VERIFICATION),
            _ => return Err(RecordError::Schema),
        };
        let input: HashMap<_, _> = entries.into_iter().collect();
        let mut valid = input
            .keys()
            .all(|key| base.contains(&key.as_str()) || fields.contains(&key.as_str()));
        let mut output = Map::new();
        let mut bytes = 2usize;
        for field in base.iter().chain(fields) {
            let property = &schema["properties"][*field];
            let normalized = if let Some(raw) = input.get(*field) {
                self.normalize(property, raw)?
            } else if name == "ProviderCapabilityVerification" && *field == "checked_at" {
                let environment = self.environment.as_mut().ok_or(RecordError::Invariant(
                    "retained dependency requires a trusted clock",
                ))?;
                let now = environment.now();
                Normalized {
                    value: now
                        .to_rfc3339_opts(
                            if now.timestamp_subsec_micros() == 0 {
                                SecondsFormat::Secs
                            } else {
                                SecondsFormat::Micros
                            },
                            true,
                        )
                        .into(),
                    valid: true,
                }
            } else if let Some(default) = property.get("default") {
                Normalized {
                    value: default.clone(),
                    valid: true,
                }
            } else if schema["required"]
                .as_array()
                .is_some_and(|required| required.iter().any(|required| required == field))
            {
                valid = false;
                continue;
            } else {
                let resolved = self.schema(property)?;
                let empty = match resolved["type"].as_str() {
                    Some("array") => "[]",
                    Some("object") => "{}",
                    _ => return Err(RecordError::Schema),
                };
                let raw: &RawValue =
                    serde_json::from_str(empty).map_err(|_| RecordError::Schema)?;
                self.normalize(property, raw)?
            };
            let mut field_valid = normalized.valid;
            let mut value = normalized.value;
            if field_valid {
                if name == "Engagement" && *field == "workspace_path" && !value.is_null() {
                    match workspace(value.as_str().ok_or_else(invalid)?, &mut self.environment)? {
                        Some(path) => value = path.into(),
                        None => field_valid = false,
                    }
                } else if name == "ProviderProfile" && *field == "secret_ref" && !value.is_null() {
                    field_valid = secret(value.as_str().ok_or_else(invalid)?);
                } else if name == "ProviderProfile" && *field == "model_allowlist" {
                    let values = value.as_array_mut().ok_or_else(invalid)?;
                    field_valid = values
                        .iter()
                        .all(|v| v.as_str().is_some_and(|s| !s.is_empty()));
                    let mut seen = HashSet::new();
                    values.retain(|v| seen.insert(v.as_str().unwrap_or_default().to_owned()));
                }
            }
            valid &= field_valid;
            insert_bounded(&mut output, &mut bytes, (*field).to_owned(), value)?;
        }
        // Independent later fields (including nested clock factories) were
        // processed even when an earlier field failed. Model-after hooks only
        // run once all fields are valid, in inherited-before-derived order.
        if valid && !base.is_empty() {
            let created =
                DateTime::parse_from_rfc3339(output["created_at"].as_str().ok_or_else(invalid)?)
                    .map_err(|_| invalid())?;
            let updated =
                DateTime::parse_from_rfc3339(output["updated_at"].as_str().ok_or_else(invalid)?)
                    .map_err(|_| invalid())?;
            valid = updated >= created;
        }
        if valid {
            valid = coherence(name, &mut output);
        }
        let value = Value::Object(output);
        bounded(&value)?;
        Ok(Normalized { value, valid })
    }
}

fn secret(value: &str) -> bool {
    if let Some(name) = value.strip_prefix("env:") {
        let mut bytes = name.bytes();
        return bytes
            .next()
            .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
            && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_');
    }
    if let Some(name) = value.strip_prefix("systemd:") {
        return (1..=128).contains(&name.len())
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b));
    }
    value
        .strip_prefix("vault:")
        .or_else(|| value.strip_prefix("session:"))
        .is_some_and(|id| {
            id.len() == 32
                && id
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
}
fn coherence(name: &str, p: &mut Map<String, Value>) -> bool {
    match name {
        "ProviderPrivacy" => {
            !(p["auto_share_tool_results"] == true && p["permits_sensitive_data"] != true)
        }
        "ProviderCapabilityVerification" => match p["status"].as_str() {
            Some("verified") => p["failure_detail"].is_null(),
            Some("failed") => p["failure_detail"].as_str().is_some_and(|v| !v.is_empty()),
            _ => true,
        },
        "ProviderProfile" => {
            if p["privacy"]["local_only"] == true && p["is_local"] != true {
                return false;
            }
            let allowlist = p["model_allowlist"].as_array().unwrap();
            if p["metadata"]["default_model"].is_string()
                && !allowlist.is_empty()
                && !allowlist.contains(&p["metadata"]["default_model"])
            {
                return false;
            }
            if let Some(options) = p["metadata"]["options"].as_object() {
                for key in ["context_window", "max_output_tokens"] {
                    if let Some(value) = options.get(key).filter(|v| !v.is_null()) {
                        let Some(number) = value.as_number() else {
                            return false;
                        };
                        let raw = number.as_str();
                        if raw.contains(['.', 'e', 'E'])
                            || raw.starts_with('-')
                            || raw.bytes().all(|b| b == b'0')
                        {
                            return false;
                        }
                    }
                }
            }
            let verifications = p["capability_verifications"].as_object().unwrap();
            if verifications
                .iter()
                .any(|(key, value)| value["model"] != *key)
            {
                return false;
            }
            let verified = verifications
                .values()
                .any(|v| v["status"] == "verified" && v["contract_version"] == "required-tool-v1");
            p["capabilities"]["tool_calling"] = verified.into();
            p["capabilities"]["parallel_tool_calls"] = false.into();
            true
        }
        _ => true,
    }
}

// pathlib's POSIX lexical normalization preserves `..` and exactly two leading
// slashes. No stat, canonicalize, symlink traversal or directory creation.
fn lexical(path: &str) -> String {
    let root = if path.starts_with("//") && !path.starts_with("///") {
        "//"
    } else if path.starts_with('/') {
        "/"
    } else {
        ""
    };
    let parts = path
        .split('/')
        .filter(|s| !s.is_empty() && *s != ".")
        .collect::<Vec<_>>();
    let joined = parts.join("/");
    if root.is_empty() && joined.is_empty() {
        ".".into()
    } else {
        format!("{root}{joined}")
    }
}
fn workspace(
    value: &str,
    environment: &mut Option<&mut dyn DependencyEnvironment>,
) -> Result<Option<String>> {
    let mut path = lexical(value);
    if path.starts_with('~') {
        let (first, rest) = path.split_once('/').unwrap_or((&path, ""));
        let environment = environment.as_mut().ok_or(RecordError::Invariant(
            "retained dependency requires trusted home expansion",
        ))?;
        let home = environment.expand_user(first)?;
        if home.len().saturating_add(rest.len()).saturating_add(1) > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        let home = lexical(&home);
        path = if rest.is_empty() {
            home
        } else {
            lexical(&format!(
                "{home}{}{rest}",
                if home.ends_with('/') { "" } else { "/" }
            ))
        };
    }
    Ok((path.starts_with('/') && path != "/").then_some(path))
}

pub(super) fn hydrate(
    schema: &Value,
    bytes: &[u8],
    environment: Option<&mut dyn DependencyEnvironment>,
) -> Result<Value> {
    let raw: &RawValue = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
    let mut hydrator = Hydrator {
        root: schema,
        environment,
        entries: 0,
    };
    let result = hydrator.normalize(schema, raw)?;
    if !result.valid {
        return Err(invalid());
    }
    bounded(&result.value)?;
    Ok(result.value)
}
