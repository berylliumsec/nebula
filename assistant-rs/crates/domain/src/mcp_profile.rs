//! Immutable saved MCP configuration validation. No connection or secret lookup.
use crate::records::{MAX_RECORD_BYTES, RecordError};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Timelike};
use serde::{
    Deserialize, Deserializer,
    de::{MapAccess, Visitor},
};
use serde_json::Value;
use serde_json::value::RawValue;
use std::io::Write;
use std::{collections::HashSet, net::Ipv6Addr};

type Result<T> = std::result::Result<T, RecordError>;

struct Entries<'a>(Vec<(String, &'a RawValue)>);
impl<'de> Deserialize<'de> for Entries<'de> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct EntriesVisitor;
        impl<'de> Visitor<'de> for EntriesVisitor {
            type Value = Entries<'de>;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("retained MCP dictionary")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut access: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut entries = Vec::new();
                while let Some(entry) = access.next_entry()? {
                    entries.push(entry);
                }
                Ok(Entries(entries))
            }
        }
        deserializer.deserialize_map(EntriesVisitor)
    }
}
fn numeric(text: &str) -> Option<f64> {
    let text = text.trim();
    let bytes = text.as_bytes();
    if !bytes.is_ascii()
        || bytes.iter().enumerate().any(|(i, &b)| {
            b == b'_'
                && (i == 0
                    || i + 1 == bytes.len()
                    || !bytes[i - 1].is_ascii_digit()
                    || !bytes[i + 1].is_ascii_digit())
        })
    {
        return None;
    }
    text.replace('_', "").parse().ok()
}
fn normalize(schema: &Value, root: &Value, raw: &RawValue) -> Result<Value> {
    let schema = if let Some(reference) = schema["$ref"].as_str() {
        root.pointer(reference.strip_prefix('#').ok_or(RecordError::Schema)?)
            .ok_or(RecordError::Schema)?
    } else {
        schema
    };
    let mut value: Value = serde_json::from_str(raw.get()).map_err(|_| RecordError::Json)?;
    if let Some(branches) = schema["anyOf"].as_array() {
        if !value.is_null()
            && let Some(branch) = branches.iter().find(|branch| branch["type"] != "null")
        {
            return normalize(branch, root, raw);
        }
        return Ok(value);
    }
    match schema["type"].as_str() {
        Some("string") if schema["format"] == "date-time" => {
            if let Some(text) = value.as_str() {
                if let Ok(time) = DateTime::parse_from_rfc3339(text) {
                    value = time
                        .to_rfc3339_opts(
                            if time.timestamp_subsec_micros() == 0 {
                                SecondsFormat::Secs
                            } else {
                                SecondsFormat::Micros
                            },
                            true,
                        )
                        .into();
                } else if let Ok(time) = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f")
                {
                    value = time
                        .format(if time.nanosecond() / 1000 == 0 {
                            "%Y-%m-%dT%H:%M:%S"
                        } else {
                            "%Y-%m-%dT%H:%M:%S%.6f"
                        })
                        .to_string()
                        .into();
                }
            }
        }
        Some("string")
            if schema.get("enum").is_none()
                && schema.get("const").is_none()
                && schema.get("format").is_none() =>
        {
            if let Some(text) = value.as_str() {
                value = text.trim().into();
            }
        }
        Some("number") => {
            let number = match &value {
                Value::Bool(v) => Some(f64::from(u8::from(*v))),
                Value::String(s) => numeric(s),
                _ => value.as_f64(),
            };
            if let Some(number) = number.and_then(serde_json::Number::from_f64) {
                value = number.into();
            }
        }
        Some("boolean") => {
            let boolean = match &value {
                Value::Bool(v) => Some(*v),
                Value::Number(n) => n.as_f64().and_then(|n| {
                    if n == 0.0 {
                        Some(false)
                    } else if n == 1.0 {
                        Some(true)
                    } else {
                        None
                    }
                }),
                Value::String(s) => match s.to_ascii_lowercase().as_str() {
                    "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
                    "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
                    _ => None,
                },
                _ => None,
            };
            if let Some(boolean) = boolean {
                value = boolean.into();
            }
        }
        Some("object") if value.is_object() => {
            let entries: Entries<'_> =
                serde_json::from_str(raw.get()).map_err(|_| RecordError::Json)?;
            if let Some(properties) = schema["properties"].as_object() {
                for (key, raw) in entries.0 {
                    if let Some(property) = properties.get(&key) {
                        value[&key] = normalize(property, root, raw)?;
                    }
                }
            } else if let Some(additional) = schema.get("additionalProperties") {
                let mut result = serde_json::Map::new();
                for (key, raw) in entries.0 {
                    result.insert(key.trim().into(), normalize(additional, root, raw)?);
                }
                value = result.into();
            }
        }
        Some("array") if value.is_array() => {
            let entries: Vec<&RawValue> =
                serde_json::from_str(raw.get()).map_err(|_| RecordError::Json)?;
            value = entries
                .into_iter()
                .map(|raw| normalize(&schema["items"], root, raw))
                .collect::<Result<Vec<_>>>()?
                .into();
        }
        _ => {}
    }
    Ok(value)
}
pub(crate) fn hydrate(schema: &Value, bytes: &[u8]) -> Result<Value> {
    let raw: &RawValue = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
    normalize(schema, schema, raw)
}
struct Size(usize);
impl Write for Size {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > MAX_RECORD_BYTES {
            return Err(std::io::Error::other("MCP record exceeds byte bound"));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn require(ok: bool) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(RecordError::Invariant("MCP profile is incoherent"))
    }
}
fn identifier(value: &str) -> bool {
    let mut bytes = value.bytes();
    bytes
        .next()
        .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
        && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
}
fn secret(value: &str) -> bool {
    if let Some(name) = value.strip_prefix("env:") {
        return identifier(name);
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
fn header(value: &str) -> bool {
    !value.is_empty()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+.^_`|~-".contains(&b))
}
fn nonempty(value: &Value) -> bool {
    value.as_str().is_some_and(|v| !v.is_empty())
}

// Mirror urllib.urlsplit's retained validation, not URL request normalization.
// No port parsing, DNS resolution, credential resolution or network access.
fn endpoint_valid(endpoint: &str) -> bool {
    let normalized = endpoint
        .trim_start_matches(|c| c <= '\u{20}')
        .replace(['\t', '\r', '\n'], "");
    let Some((scheme, rest)) = normalized.split_once(':') else {
        return false;
    };
    let http = scheme.eq_ignore_ascii_case("http");
    if !http && !scheme.eq_ignore_ascii_case("https") {
        return false;
    }
    let Some(rest) = rest.strip_prefix("//") else {
        return false;
    };
    let split = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..split];
    let tail = &rest[split..];
    let (tail, fragment) = tail.split_once('#').unwrap_or((tail, ""));
    let (_, query) = tail.split_once('?').unwrap_or((tail, ""));
    if !query.is_empty() || !fragment.is_empty() {
        return false;
    }
    // Unicode15 NFKC delimiter set used by Python's authority check.
    if authority.chars().any(|c| {
        matches!(
            c as u32,
            0x2047
                | 0x2048
                | 0x2049
                | 0x2100
                | 0x2101
                | 0x2105
                | 0x2106
                | 0x2a74
                | 0xfe13
                | 0xfe16
                | 0xfe55
                | 0xfe56
                | 0xfe5f
                | 0xfe6b
                | 0xff03
                | 0xff0f
                | 0xff1a
                | 0xff1f
                | 0xff20
        )
    }) {
        return false;
    }
    if authority.contains('[') != authority.contains(']') {
        return false;
    }
    if let Some((_, bracketed)) = authority.split_once('[') {
        let Some((address, _)) = bracketed.split_once(']') else {
            return false;
        };
        let valid = if let Some(future) = address.strip_prefix('v') {
            future.split_once('.').is_some_and(|(version, host)| {
                !version.is_empty()
                    && version.bytes().all(|c| c.is_ascii_hexdigit())
                    && !host.is_empty()
            })
        } else {
            let (address, scope) = address
                .split_once('%')
                .map_or((address, None), |(address, scope)| (address, Some(scope)));
            scope.is_none_or(|s| !s.is_empty() && !s.contains('%'))
                && address.parse::<Ipv6Addr>().is_ok()
        };
        if !valid {
            return false;
        }
    }
    let host_port = if let Some((userinfo, host)) = authority.rsplit_once('@') {
        let (username, password) = userinfo.split_once(':').unwrap_or((userinfo, ""));
        if !username.is_empty() || !password.is_empty() {
            return false;
        }
        host
    } else {
        authority
    };
    let hostname = if let Some((_, rest)) = host_port.split_once('[') {
        rest.split_once(']').map_or("", |(host, _)| host)
    } else {
        host_port
            .split_once(':')
            .map_or(host_port, |(host, _)| host)
    };
    !hostname.is_empty()
        && (!http
            || hostname.eq_ignore_ascii_case("localhost")
            || hostname == "127.0.0.1"
            || hostname == "::1")
}

pub(crate) fn validate(p: &Value) -> Result<()> {
    serde_json::to_writer(Size(0), p).map_err(|_| RecordError::TooLarge)?;
    for field in ["command", "cwd"] {
        if let Some(value) = p[field].as_str() {
            require(value.starts_with('/'))?;
        }
    }
    if let Some(value) = p["bearer_secret_ref"].as_str() {
        require(secret(value))?;
    }
    for field in ["header_secret_refs", "environment_secret_refs"] {
        for (name, value) in p[field]
            .as_object()
            .ok_or(RecordError::Shape("mcp_servers"))?
        {
            require(secret(value.as_str().unwrap_or_default()))?;
            require(if field == "header_secret_refs" {
                header(name)
            } else {
                identifier(name)
            })?;
        }
    }
    for argument in p["arguments"]
        .as_array()
        .ok_or(RecordError::Shape("mcp_servers"))?
    {
        require(
            argument
                .as_str()
                .is_some_and(|value| value.chars().count() <= 8192),
        )?;
    }
    for (name, value) in p["environment"]
        .as_object()
        .ok_or(RecordError::Shape("mcp_servers"))?
    {
        let lower = name.to_lowercase();
        require(
            identifier(name)
                && !["secret", "token", "password", "credential", "api_key"]
                    .iter()
                    .any(|part| lower.contains(part)),
        )?;
        require(
            value
                .as_str()
                .is_some_and(|value| value.chars().count() <= 8192),
        )?;
    }
    if p["transport"] == "stdio" {
        require(nonempty(&p["command"]) && p["url"].is_null() && p["auth_mode"] == "none")?;
        require(p["enabled"] != true || p["trusted_stdio"] == true)?;
        require(p["cwd_policy"] != "fixed" || nonempty(&p["cwd"]))?;
        require(p["cwd_policy"] != "workspace" || p["cwd"].is_null())?;
    } else {
        require(
            p["command"].is_null()
                && p["arguments"].as_array().is_some_and(Vec::is_empty)
                && nonempty(&p["url"]),
        )?;
        require(endpoint_valid(p["url"].as_str().unwrap_or_default()))?;
        require(
            p["cwd"].is_null()
                && p["environment"]
                    .as_object()
                    .is_some_and(serde_json::Map::is_empty)
                && p["environment_secret_refs"]
                    .as_object()
                    .is_some_and(serde_json::Map::is_empty),
        )?;
    }
    require(p["auth_mode"] != "bearer" || nonempty(&p["bearer_secret_ref"]))?;
    require(
        p["auth_mode"] != "headers"
            || p["header_secret_refs"]
                .as_object()
                .is_some_and(|map| !map.is_empty()),
    )?;
    let enabled: HashSet<_> = p["enabled_tools"]
        .as_array()
        .ok_or(RecordError::Shape("mcp_servers"))?
        .iter()
        .filter_map(Value::as_str)
        .collect();
    require(
        !p["disabled_tools"]
            .as_array()
            .ok_or(RecordError::Shape("mcp_servers"))?
            .iter()
            .filter_map(Value::as_str)
            .any(|name| enabled.contains(name)),
    )
}
