//! Pure, bounded text preparation. Inputs have already passed their domain
//! hydration and authorization boundaries. No store, credentials, file or network
//! access is granted here; returned allocations need the owning runtime's lease.
use chrono::{DateTime, FixedOffset};
use nebula_assistant_domain::{
    model_validation::{InputOrigin, Model, hydrate},
    records::RecordError,
    tool_receipt::{exact_integer, python_equal},
};
use num_bigint::BigInt;
use num_traits::{FromPrimitive, ToPrimitive, Zero};
use serde::{Deserialize, Serialize};
use serde_json::{Number, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::LazyLock,
};

pub const MAX_PREPARATION_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_HISTORY_MESSAGES: usize = 10_000;
const MAX_INTEGER_DIGITS: usize = 4300;

pub enum Error {
    HistoryConflict(&'static str),
    Capacity(String),
    Unsupported(&'static str),
    Boundary(&'static str),
    Source { kind: &'static str, detail: String },
    Validation(RecordError),
    ContextValidation(Vec<Value>),
}
impl std::fmt::Debug for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::HistoryConflict(_) => "HistoryConflict",
            Self::Capacity(_) => "ContextCapacity",
            Self::Unsupported(_) => "UnsupportedTextProjection",
            Self::Boundary(_) => "PreparationBoundary",
            Self::Source { .. } => "SourceContextFailure",
            Self::Validation(_) | Self::ContextValidation(_) => "PreparationValidation",
        })
    }
}
pub type Result<T> = std::result::Result<T, Error>;
fn source(kind: &'static str, detail: impl Into<String>) -> Error {
    Error::Source {
        kind,
        detail: detail.into(),
    }
}
struct Count(usize);
impl std::io::Write for Count {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(bytes.len());
        if self.0 > MAX_PREPARATION_BYTES {
            return Err(std::io::Error::other("preparation byte limit"));
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn charge<T: Serialize + ?Sized>(value: &T, total: &mut usize) -> Result<()> {
    let mut count = Count(*total);
    serde_json::to_writer(&mut count, value)
        .map_err(|_| Error::Boundary("text preparation exceeds byte limit"))?;
    *total = count.0;
    Ok(())
}
fn check<T: Serialize + ?Sized>(value: &T) -> Result<()> {
    charge(value, &mut 0)
}
fn field<'a>(v: &'a Value, key: &str) -> Result<&'a Value> {
    v.get(key).ok_or(Error::Boundary("expected hydrated field"))
}
fn text<'a>(v: &'a Value, key: &str) -> Result<&'a str> {
    field(v, key)?
        .as_str()
        .ok_or(Error::Boundary("expected hydrated text"))
}
fn object(v: &Value) -> Result<&serde_json::Map<String, Value>> {
    v.as_object()
        .ok_or(Error::Boundary("expected hydrated object"))
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
        Value::Number(v) => v.as_f64().is_none_or(|v| v != 0.0),
    }
}
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

#[derive(Deserialize)]
struct StaticData {
    known_model_revision: String,
    known_models: BTreeMap<String, (u64, Option<u64>)>,
    isdigit_ranges: Vec<(u32, u32)>,
    decimal_zeroes: Vec<u32>,
}
static DATA: LazyLock<StaticData> = LazyLock::new(|| {
    serde_json::from_str(include_str!("execution_context/static_data.json"))
        .expect("generated source context data")
});
fn decimal(c: char) -> Option<u8> {
    let code = c as u32;
    let i = DATA.decimal_zeroes.partition_point(|start| *start <= code);
    (i != 0 && code < DATA.decimal_zeroes[i - 1] + 10)
        .then(|| (code - DATA.decimal_zeroes[i - 1]) as u8)
}
fn digit(c: char) -> bool {
    let code = c as u32;
    let i = DATA
        .isdigit_ranges
        .partition_point(|(start, _)| *start <= code);
    i != 0 && code <= DATA.isdigit_ranges[i - 1].1
}
fn number(value: BigInt) -> Result<Number> {
    value
        .to_string()
        .parse()
        .map_err(|_| Error::Boundary("context integer encoding failed"))
}
fn positive(value: &Value) -> Result<BigInt> {
    if let Value::Number(n) = value
        && !n.to_string().contains(['.', 'e', 'E'])
        && n.to_string().trim_start_matches('-').len() > MAX_INTEGER_DIGITS
    {
        return Err(Error::Boundary("context integer exceeds digit limit"));
    }
    if let Some(integer) = exact_integer(value) {
        return Ok(integer.max(BigInt::zero()));
    }
    if let Value::String(s) = value
        && !s.is_empty()
        && s.chars().all(digit)
    {
        if s.chars().count() > MAX_INTEGER_DIGITS {
            return Err(Error::Boundary("context integer exceeds digit limit"));
        }
        let mut digits = Vec::with_capacity(s.len());
        for c in s.chars() {
            let Some(d) = decimal(c) else {
                return Err(source(
                    "ValueError",
                    format!(
                        "invalid literal for int() with base 10: {}",
                        python_literal(s)
                    ),
                ));
            };
            digits.push(b'0' + d);
        }
        return BigInt::parse_bytes(&digits, 10).ok_or(Error::Boundary("invalid context integer"));
    }
    Ok(BigInt::zero())
}
fn python_literal(s: &str) -> String {
    // Digit-only strings cannot contain a quote, backslash or whitespace.
    format!("'{s}'")
}
fn fraction(value: &BigInt, multiplier: f64) -> Result<Number> {
    // Python converts the integer to double before multiplication; integer-ratio
    // arithmetic would differ for large legitimate retained metadata values.
    let value = value
        .to_f64()
        .filter(|v| v.is_finite())
        .ok_or_else(|| source("OverflowError", "int too large to convert to float"))?;
    let result = (value * multiplier).floor();
    let result = BigInt::from_f64(result)
        .ok_or_else(|| source("OverflowError", "cannot convert float infinity to integer"))?;
    number(result.max(BigInt::from(1)))
}
/// Source catalog lookup, including hosted prefixes, snapshots and longest-id wins.
pub fn known_model_limits(model: Option<&str>) -> Result<Option<(u64, Option<u64>)>> {
    if model.is_some_and(|m| m.len() > MAX_PREPARATION_BYTES) {
        return Err(Error::Boundary("model exceeds byte limit"));
    }
    let Some(model) = model.filter(|m| !m.is_empty()) else {
        return Ok(None);
    };
    let mut normalized = model
        .trim_matches(py_space)
        .to_lowercase()
        .rsplit('/')
        .next()
        .unwrap_or("")
        .to_owned();
    normalized.truncate(normalized.find([':', '@']).unwrap_or(normalized.len()));
    let vendors = [
        "anthropic",
        "amazon",
        "meta",
        "mistral",
        "deepseek",
        "qwen",
        "openai",
        "cohere",
        "moonshot",
        "moonshotai",
        "minimax",
        "zai",
        "writer",
    ];
    let mut tail = normalized.as_str();
    if let Some((region, rest)) = tail.split_once('.')
        && ["us", "eu", "apac", "au", "jp", "global"].contains(&region)
    {
        tail = rest;
    }
    if let Some((vendor, rest)) = tail.split_once('.')
        && vendors.contains(&vendor)
    {
        normalized = rest.to_owned();
    }
    normalized = normalized.replace(['.', '_'], "-");
    loop {
        if let Some(limits) = DATA.known_models.get(&normalized) {
            return Ok(Some(*limits));
        }
        let Some(index) = normalized.rfind('-') else {
            return Ok(None);
        };
        normalized.truncate(index);
    }
}
#[derive(Clone, Serialize)]
pub struct ContextLimits {
    pub model: Option<String>,
    pub context_window: Number,
    pub max_output_tokens: Number,
    pub input_capacity: Number,
    pub target_input_tokens: Number,
    pub compacted_input_target: Number,
    pub source: &'static str,
    pub estimated: bool,
    pub metadata_revision: Option<String>,
    pub route_limits_verified: bool,
    pub eligible_route_count: Option<usize>,
    pub route_context_window: Option<Number>,
    pub route_input_limit: Option<Number>,
    pub route_limits_required: bool,
}
impl std::fmt::Debug for ContextLimits {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ContextLimits")
            .field("source", &self.source)
            .finish_non_exhaustive()
    }
}
fn iterable_objects(value: &Value) -> Result<Vec<&Value>> {
    match value {
        Value::Array(a) if a.len() <= MAX_HISTORY_MESSAGES => {
            Ok(a.iter().filter(|v| v.is_object()).collect())
        }
        Value::Array(_) => Err(Error::Boundary("catalog exceeds entry limit")),
        Value::Object(_) | Value::String(_) => Ok(vec![]),
        value => Err(source(
            "TypeError",
            format!(
                "'{}' object is not iterable",
                match value {
                    Value::Null => "NoneType",
                    Value::Bool(_) => "bool",
                    Value::Number(n) if n.to_string().contains(['.', 'e', 'E']) => "float",
                    _ => "int",
                }
            ),
        )),
    }
}
fn slug(value: &Value) -> Option<&str> {
    value
        .as_str()
        .filter(|s| s.chars().count() <= 500)
        .map(|s| s.trim_matches(py_space))
        .filter(|s| !s.is_empty())
}
fn verified(descriptor: &Value, model: &str) -> bool {
    descriptor["route_limits_verified"] == Value::Bool(true)
        && slug(&descriptor["route_limits_source_model"]).unwrap_or(model)
            == slug(&descriptor["alias_target"]).unwrap_or(model)
}
fn minimum_nonzero(values: impl IntoIterator<Item = BigInt>) -> BigInt {
    values
        .into_iter()
        .filter(|v| !v.is_zero())
        .min()
        .unwrap_or_default()
}
/// Resolve source-compatible limits from a hydrated immutable ProviderProfile.
/// Catalog discovery and remote endpoint verification are not performed here.
pub fn resolve_context_limits(
    profile: &Value,
    model: Option<&str>,
    requested_output_tokens: Option<&Number>,
    required_parameters: &BTreeSet<String>,
) -> Result<ContextLimits> {
    let mut bytes = 0;
    charge(profile, &mut bytes)?;
    charge(&model, &mut bytes)?;
    charge(&requested_output_tokens, &mut bytes)?;
    charge(required_parameters, &mut bytes)?;
    let metadata = object(field(profile, "metadata")?)?;
    let local = field(profile, "is_local")?
        .as_bool()
        .ok_or(Error::Boundary("expected hydrated provider locality"))?;
    let provider_type = text(profile, "provider_type")?;
    let options = metadata.get("options").and_then(Value::as_object);
    let option = |key: &str| options.and_then(|o| o.get(key)).unwrap_or(&Value::Null);
    let mut configured_window = positive(option("context_window"))?;
    let mut configured_output = positive(option("max_output_tokens"))?;
    let descriptors = metadata.get("model_descriptors").unwrap_or(&Value::Null);
    let empty = Value::Array(vec![]);
    let descriptors = if metadata.contains_key("model_descriptors") {
        descriptors
    } else {
        &empty
    };
    let descriptor = iterable_objects(descriptors)?
        .into_iter()
        .find(|v| model.is_some_and(|m| v["id"].as_str() == Some(m)))
        .unwrap_or(&Value::Null);
    let mut model_window = positive(&descriptor["context_window"])?;
    let mut model_output = positive(&descriptor["max_output_tokens"])?;
    let mut input_limit = positive(&descriptor["max_input_tokens"])?;
    let mut known = false;
    if !local
        && (model_window.is_zero() || model_output.is_zero())
        && let Some((window, output)) = known_model_limits(model)?
    {
        if model_window.is_zero() {
            model_window = window.into();
            known = true;
        }
        if model_output.is_zero() {
            model_output = output.unwrap_or(0).into();
        }
    }
    let route_verified = model.is_some_and(|m| verified(descriptor, m));
    let mut route_window = BigInt::zero();
    let mut route_input = BigInt::zero();
    let mut eligible_count = None;
    if route_verified {
        let raw = descriptor.get("route_limits").unwrap_or(&empty);
        let eligible: Vec<_> = iterable_objects(raw)?
            .into_iter()
            .filter(|v| {
                if !python_equal(v.get("status").unwrap_or(&json!(0)), &json!(0)) {
                    return false;
                }
                let parameters: BTreeSet<_> = v["supported_parameters"]
                    .as_array()
                    .map(|a| a.iter().filter_map(Value::as_str).collect())
                    .unwrap_or_default();
                required_parameters
                    .iter()
                    .all(|p| parameters.contains(p.as_str()))
            })
            .collect();
        eligible_count = Some(eligible.len());
        if eligible.is_empty() {
            return Err(Error::Capacity(format!(
                "no verified OpenRouter endpoint supports {} for {}",
                if required_parameters.is_empty() {
                    "text generation".to_owned()
                } else {
                    required_parameters
                        .iter()
                        .cloned()
                        .collect::<Vec<_>>()
                        .join(", ")
                },
                model.unwrap_or("")
            )));
        }
        let mut windows = vec![];
        let mut inputs = vec![];
        let mut outputs = vec![];
        // Evaluate all window options, then all input options, then output options,
        // matching Python list-comprehension error precedence.
        for route in &eligible {
            windows.push(positive(&route["context_window"])?);
        }
        for route in &eligible {
            inputs.push(positive(&route["max_input_tokens"])?);
        }
        for route in &eligible {
            outputs.push(positive(&route["max_output_tokens"])?);
        }
        if windows
            .iter()
            .chain(&inputs)
            .chain(&outputs)
            .any(Zero::is_zero)
        {
            return Err(Error::Capacity(format!(
                "verified OpenRouter endpoint limits are incomplete for {}",
                model.unwrap_or("")
            )));
        }
        route_window = windows.into_iter().min().unwrap_or_default();
        route_input = inputs.into_iter().min().unwrap_or_default();
        let route_output = outputs.into_iter().min().unwrap_or_default();
        model_window = minimum_nonzero([model_window, route_window.clone()]);
        model_output = minimum_nonzero([model_output, route_output]);
        input_limit = minimum_nonzero([input_limit, route_input.clone()]);
    } else if provider_type == "openrouter" {
        let primary = positive(&descriptor["primary_route_context_window"])?;
        let cap = if primary.is_zero() {
            BigInt::from(8192)
        } else {
            primary.clone()
        };
        if model_window.is_zero() {
            model_window = primary.clone();
        }
        if !model_window.is_zero() {
            model_window = model_window.min(cap.clone());
        }
        if !configured_window.is_zero() {
            configured_window = configured_window.min(cap);
        }
        if primary.is_zero() {
            if !model_output.is_zero() {
                model_output = model_output.min(2048.into());
            }
            if !configured_output.is_zero() {
                configured_output = configured_output.min(2048.into());
            }
        }
    }
    let (window, provenance, estimated) = if !model_window.is_zero() {
        (
            minimum_nonzero([model_window, configured_window]),
            if known {
                "known_model"
            } else {
                "model_catalog"
            },
            provider_type == "openrouter" && !route_verified,
        )
    } else if !configured_window.is_zero() {
        (configured_window, "configured", true)
    } else {
        (8192.into(), "fallback", true)
    };
    let mut caps = vec![(&window - BigInt::from(1)).max(1.into())];
    if !model_output.is_zero() {
        caps.push(model_output.clone());
    }
    if !configured_output.is_zero() {
        caps.push(configured_output.clone());
    }
    let cap = caps.into_iter().min().unwrap_or_else(|| 1.into());
    let default = if !model_output.is_zero() || !configured_output.is_zero() {
        cap.clone()
    } else {
        cap.clone().min(2048.into())
    };
    let requested = match requested_output_tokens {
        Some(n) => exact_integer(&Value::Number(n.clone()))
            .filter(|n| n > &BigInt::zero())
            .ok_or(Error::Boundary(
                "output allowance must be a positive hydrated integer",
            ))?,
        None => default,
    };
    let output = requested.min(cap);
    let mut input = &window - &output;
    if !input_limit.is_zero() {
        input = input.min(input_limit);
    }
    let target = fraction(&input, 0.75)?;
    let compact = fraction(&input, 0.60)?;
    let revision = metadata
        .get("route_catalog_revision")
        .filter(|v| truthy(v))
        .or_else(|| metadata.get("model_catalog_revision").filter(|v| truthy(v)));
    let revision = match revision {
        Some(Value::String(s)) => Some(s.clone()),
        Some(_) => None,
        None if known => Some(format!("known-models:{}", DATA.known_model_revision)),
        None => None,
    };
    if input < BigInt::from(1) {
        return Err(Error::ContextValidation(vec![
            json!({"type":"greater_than_equal","loc":["input_capacity"],"msg":"Input should be greater than or equal to 1","input":number(input)?,"ctx":{"ge":1}}),
        ]));
    }
    let result = ContextLimits {
        model: model.map(str::to_owned),
        context_window: number(window)?,
        max_output_tokens: number(output)?,
        input_capacity: number(input)?,
        target_input_tokens: target,
        compacted_input_target: compact,
        source: provenance,
        estimated,
        metadata_revision: revision,
        route_limits_verified: route_verified,
        eligible_route_count: eligible_count,
        route_context_window: if route_window.is_zero() {
            None
        } else {
            Some(number(route_window)?)
        },
        route_input_limit: if route_input.is_zero() {
            None
        } else {
            Some(number(route_input)?)
        },
        route_limits_required: provider_type == "openrouter",
    };
    charge(&result, &mut bytes)?;
    Ok(result)
}

pub fn estimate_text_tokens(text: &str, message_count: usize) -> Result<usize> {
    if text.len() > MAX_PREPARATION_BYTES || message_count > MAX_HISTORY_MESSAGES {
        return Err(Error::Boundary("text estimate exceeds preparation bounds"));
    }
    Ok(text.len().div_ceil(3).max(1) + message_count * 8)
}
pub fn estimate_text_messages(messages: &[Value], instructions: &str) -> Result<usize> {
    if messages.len() > MAX_HISTORY_MESSAGES {
        return Err(Error::Boundary("history exceeds record limit"));
    }
    let mut bytes = 0;
    charge(messages, &mut bytes)?;
    charge(instructions, &mut bytes)?;
    let mut total = estimate_text_tokens(instructions, 0)?;
    for message in messages {
        let content = message["content"].as_str().ok_or(Error::Unsupported(
            "image and non-text model content require their projection",
        ))?;
        total += estimate_text_tokens(content, 1)?;
    }
    Ok(total)
}
pub fn estimate_text_request(request: &Value) -> Result<usize> {
    check(request)?;
    if ["tools", "tool_results", "response_schema"]
        .iter()
        .any(|k| truthy(&request[*k]))
    {
        return Err(Error::Unsupported(
            "tool or structured request estimation requires its projection",
        ));
    }
    let messages = field(request, "messages")?
        .as_array()
        .ok_or(Error::Boundary("expected normalized model messages"))?;
    let instructions = request["instructions"].as_str().unwrap_or("");
    let mut total = estimate_text_messages(messages, instructions)?;
    if truthy(&request["metadata"]["continuation"]) {
        let continuation = request["metadata"]["continuation"]
            .as_str()
            .ok_or(Error::Boundary("expected normalized continuation text"))?;
        let rendered = serde_json::to_string(continuation)
            .map_err(|_| Error::Boundary("invalid continuation"))?;
        total += estimate_text_tokens(&rendered, 0)?;
    }
    Ok(total)
}

fn text_blocks(message: &Value) -> Result<()> {
    let blocks = field(message, "content_blocks")?
        .as_array()
        .ok_or(Error::Boundary("expected hydrated content blocks"))?;
    if blocks.iter().any(|b| b["type"] != "text") {
        return Err(Error::Unsupported(
            "historical images require artifact and runtime projection",
        ));
    }
    Ok(())
}
/// Sort the supplied scoped records as Python does, excluding every truthy
/// retracted_at value. Scope/authorization are the caller's responsibility.
pub fn visible_history(messages: &[Value]) -> Result<Vec<Value>> {
    if messages.len() > MAX_HISTORY_MESSAGES {
        return Err(Error::Boundary("history exceeds record limit"));
    }
    let mut budget = 0;
    charge(messages, &mut budget)?;
    let mut keys = Vec::with_capacity(messages.len());
    for message in messages {
        if truthy(&message["metadata"]["retracted_at"]) {
            continue;
        }
        let sequence = exact_integer(field(message, "sequence")?)
            .filter(|v| v > &BigInt::zero())
            .ok_or(Error::Boundary("expected hydrated positive sequence"))?;
        let created = DateTime::<FixedOffset>::parse_from_rfc3339(text(message, "created_at")?)
            .map_err(|_| Error::Boundary("expected aware message time"))?;
        charge(message, &mut budget)?;
        keys.push((sequence, created, text(message, "id")?, message));
    }
    keys.sort_by(|a, b| (&a.0, &a.1, a.2).cmp(&(&b.0, &b.1, b.2)));
    Ok(keys.into_iter().map(|(_, _, _, m)| m.clone()).collect())
}
#[derive(Serialize)]
struct Attachment<'a> {
    source_kind: &'a Value,
    source_id: &'a Value,
    source_label: &'a Value,
    text: &'a Value,
    sha256: &'a Value,
    truncated: &'a Value,
}
/// Rebuild the exact source text envelope. Invalid historical selected context
/// falls back to visible content, as Python does; the caller may record a safe
/// diagnostic from the returned flag without including the attachment contents.
pub fn stored_model_text(message: &Value) -> Result<(String, bool)> {
    check(message)?;
    let content = text(message, "content")?;
    let role = text(message, "role")?;
    let metadata = object(field(message, "metadata")?)?;
    if role == "system" && metadata.get("kind") == Some(&json!("agent_message")) {
        let label = metadata
            .get("sender_title")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("Peer agent");
        let identity = metadata
            .get("sender_session_id")
            .and_then(Value::as_str)
            .map(|s| format!(" ({s})"))
            .unwrap_or_default();
        let out = format!("Peer agent message from {label}{identity}:\n\n{content}");
        check(&out)?;
        return Ok((out, false));
    }
    let Some(raw) = metadata
        .get("context_attachments")
        .and_then(Value::as_array)
        .filter(|a| !a.is_empty())
    else {
        return Ok((content.into(), false));
    };
    if role != "user" {
        return Ok((content.into(), false));
    }
    if raw.len() > MAX_HISTORY_MESSAGES {
        return Err(Error::Boundary(
            "historical context exceeds attachment limit",
        ));
    }
    let mut budget = 0;
    charge(message, &mut budget)?;
    let mut validated = Vec::with_capacity(raw.len());
    for value in raw {
        let bytes =
            serde_json::to_vec(value).map_err(|_| Error::Boundary("invalid attachment JSON"))?;
        match hydrate(
            Model::ChatContextAttachment,
            InputOrigin::RetainedJson,
            &bytes,
        ) {
            Ok(value) => {
                charge(&value, &mut budget)?;
                validated.push(value);
            }
            Err(RecordError::TooLarge) => {
                return Err(Error::Boundary("historical context exceeds model bound"));
            }
            Err(_) => return Ok((content.into(), true)),
        }
    }
    let entries: Vec<_> = validated
        .iter()
        .map(|v| Attachment {
            source_kind: &v["source_kind"],
            source_id: &v["source_id"],
            source_label: &v["source_label"],
            text: &v["text"],
            sha256: &v["sha256"],
            truncated: &v["truncated"],
        })
        .collect();
    let rendered =
        serde_json::to_string(&entries).map_err(|_| Error::Boundary("invalid attachment JSON"))?;
    charge(&rendered, &mut budget)?;
    let out = format!(
        "{}\n\nBEGIN SELECTED CONTEXT (JSON)\n{rendered}\nEND SELECTED CONTEXT",
        content.trim_end_matches(py_space)
    );
    charge(&out, &mut budget)?;
    Ok((out, false))
}
#[derive(Clone, Serialize)]
pub struct MergedHistory {
    pub history: Vec<Value>,
    pub new_messages: Vec<Value>,
    /// Historical attachment validation failures, for a content-free diagnostic.
    pub invalid_context_message_ids: Vec<String>,
}
impl std::fmt::Debug for MergedHistory {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MergedHistory")
            .field("history_count", &self.history.len())
            .field("new_count", &self.new_messages.len())
            .finish_non_exhaustive()
    }
}
/// Merge an already-scoped, hydrated transcript and hydrated request messages.
/// Inputs may be unordered; retracted records never participate in replay checks.
pub fn merge_text_history(stored: &[Value], incoming: &[Value]) -> Result<MergedHistory> {
    if incoming.len() > 200 {
        return Err(Error::Boundary("request message limit"));
    }
    let mut bytes = 0;
    charge(stored, &mut bytes)?;
    charge(incoming, &mut bytes)?;
    let visible = visible_history(stored)?;
    charge(&visible, &mut bytes)?;
    let mut invalid_context_message_ids = Vec::new();
    let mut durable = Vec::with_capacity(visible.len());
    let mut history = Vec::with_capacity(visible.len() + incoming.len());
    for message in &visible {
        // Source constructs this request view without content_blocks, then copies
        // the already typed saved blocks without running their validation again.
        let supplied = json!({"role":message["role"],"content":message["content"]});
        let mut base = hydrate(
            Model::ChatRequestMessage,
            InputOrigin::RetainedJson,
            &serde_json::to_vec(&supplied).map_err(|_| Error::Boundary("invalid history JSON"))?,
        )
        .map_err(|error| match &error {
            RecordError::ModelValidation(report)
                if report.retained_bytes().saturating_add(bytes) > MAX_PREPARATION_BYTES =>
            {
                Error::Boundary("history diagnostic exceeds preparation bound")
            }
            _ => Error::Validation(error),
        })?;
        base["content_blocks"] = message["content_blocks"].clone();
        text_blocks(message)?;
        charge(&base, &mut bytes)?;
        durable.push(base.clone());
        let (model_text, invalid_context) = stored_model_text(message)?;
        if invalid_context {
            invalid_context_message_ids.push(text(message, "id")?.to_owned());
        }
        base["content"] = model_text.into();
        charge(&base, &mut bytes)?;
        history.push(base);
    }
    for message in incoming {
        text_blocks(message)?;
    }
    let matches = |a: &[Value], b: &[Value]| {
        a.len() == b.len() && a.iter().zip(b).all(|(a, b)| python_equal(a, b))
    };
    let new = if incoming.len() >= durable.len() && matches(&incoming[..durable.len()], &durable) {
        let new = &incoming[durable.len()..];
        if new.is_empty() {
            return Err(Error::HistoryConflict(
                "chat request contains no new message",
            ));
        }
        if new.len() != 1 || new[0]["role"] != "user" {
            return Err(Error::HistoryConflict(
                "a durable chat request may append exactly one user message",
            ));
        }
        new
    } else if incoming.len() == 1 && incoming[0]["role"] == "user" {
        incoming
    } else {
        let spoken: Vec<_> = durable
            .iter()
            .zip(&visible)
            .filter(|(_, m)| m["metadata"]["kind"] != "turn_outcome")
            .map(|(v, _)| v)
            .collect();
        if spoken.len() < durable.len()
            && incoming.len() == spoken.len() + 1
            && incoming[..spoken.len()]
                .iter()
                .zip(&spoken)
                .all(|(a, b)| python_equal(a, b))
            && incoming.last().is_some_and(|m| m["role"] == "user")
        {
            &incoming[incoming.len() - 1..]
        } else {
            return Err(Error::HistoryConflict(
                "supplied history diverges from the durable chat transcript",
            ));
        }
    };
    history.extend_from_slice(new);
    let result = MergedHistory {
        history,
        new_messages: new.to_vec(),
        invalid_context_message_ids,
    };
    charge(&result, &mut bytes)?;
    Ok(result)
}
/// Provider-facing projection for ordinary text messages; supplemental text
/// blocks remain in the transcript, while source sends its content field.
pub fn text_model_messages(messages: &[Value]) -> Result<Vec<Value>> {
    check(messages)?;
    if messages.len() > MAX_HISTORY_MESSAGES {
        return Err(Error::Boundary("history exceeds record limit"));
    }
    let mut bytes = 0;
    charge(messages, &mut bytes)?;
    let mut out = Vec::with_capacity(messages.len());
    for message in messages {
        text_blocks(message)?;
        let value = json!({"role":text(message,"role")?,"content":text(message,"content")?});
        charge(&value, &mut bytes)?;
        out.push(value);
    }
    join_text_assistant_messages(&out)
}
pub fn join_text_assistant_messages(messages: &[Value]) -> Result<Vec<Value>> {
    let mut bytes = 0;
    charge(messages, &mut bytes)?;
    if messages.len() > MAX_HISTORY_MESSAGES {
        return Err(Error::Boundary("history exceeds record limit"));
    }
    let mut output: Vec<Value> = vec![];
    for message in messages {
        charge(message, &mut bytes)?;
        let role = text(message, "role")?;
        let content = message["content"].as_str().ok_or(Error::Unsupported(
            "non-text joins require their content projection",
        ))?;
        if let Some(previous) = output.last_mut()
            && previous["role"] == "assistant"
            && role == "assistant"
        {
            let Value::String(old) = &mut previous["content"] else {
                return Err(Error::Boundary("invalid normalized model message"));
            };
            if !content.is_empty() {
                if !old.is_empty() {
                    old.push_str("\n\n");
                }
                old.push_str(content);
            }
        } else {
            output.push(json!({"role":role,"content":content}));
        }
    }
    check(&output)?;
    Ok(output)
}
