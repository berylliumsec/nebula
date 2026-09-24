//! Ordered diagnostics for direct retained Entity/ChatSchedule hydration.
//!
//! This is not request validation or a general Pydantic interpreter. No factory
//! runs while reading retained data. Reports share their input and never expose
//! it through Debug/Display; only their explicit Serialize interface emits it.
use crate::records::{MAX_RECORD_BYTES, RecordError};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Utc};
use serde::{
    Deserialize, Deserializer, Serialize, Serializer,
    de::{MapAccess, Visitor},
    ser::SerializeSeq,
};
use serde_json::{Map, Value, json, value::RawValue};
use speedate::{
    Date, DateTime as ParsedDateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior,
    TimeConfig,
};
use std::{collections::HashSet, fmt, io::Write, sync::Arc};
use strum::EnumMessage;

const MAX_ISSUES: usize = 10_000;
type Result<T> = std::result::Result<T, RecordError>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Model {
    Entity,
    ChatSchedule,
}
impl Model {
    pub const fn name(self) -> &'static str {
        match self {
            Self::Entity => "Entity",
            Self::ChatSchedule => "ChatSchedule",
        }
    }
    fn kind(self) -> &'static str {
        match self {
            Self::Entity => "entities",
            Self::ChatSchedule => "chat_schedules",
        }
    }
    fn fields(self) -> &'static [Field] {
        match self {
            Self::Entity => &FIELDS[..4],
            Self::ChatSchedule => &FIELDS,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InputOrigin {
    RetainedJson,
    /// The caller merged model_dump(mode="python") and trusted writer values.
    /// Known datetime strings stand for datetime objects in diagnostic inputs.
    WriterModelDump,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(untagged)]
pub enum Location {
    Field(String),
    Index(usize),
}

#[derive(Clone, PartialEq, Serialize)]
struct Issue {
    kind: &'static str,
    loc: Vec<Location>,
    msg: String,
    /// None references the whole shared input (missing/model-after errors).
    input_field: Option<String>,
    ctx: Option<Value>,
}

/// A borrowed wire view. Formatting this view implicitly would reveal input,
/// so it deliberately has no Debug implementation.
#[derive(Serialize)]
pub struct ValidationIssueRef<'a> {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub loc: &'a [Location],
    pub msg: &'a str,
    pub input: &'a Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ctx: Option<&'a Value>,
}

#[derive(Clone, PartialEq)]
pub struct ValidationReport {
    model: Model,
    origin: InputOrigin,
    input: Arc<Value>,
    input_order: Arc<[String]>,
    datetime_fields: Vec<&'static str>,
    issues: Vec<Issue>,
    retained_bytes: usize,
}
impl fmt::Debug for ValidationReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ValidationReport")
            .field("model", &self.model.name())
            .field("issues", &self.issues.len())
            .finish_non_exhaustive()
    }
}
impl fmt::Display for ValidationReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "retained {} failed {} model validation checks",
            self.model.name(),
            self.issues.len()
        )
    }
}
impl Serialize for ValidationReport {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(self.len()))?;
        for issue in self.issues() {
            seq.serialize_element(&issue)?;
        }
        seq.end()
    }
}
impl ValidationReport {
    pub const fn model_name(&self) -> &'static str {
        self.model.name()
    }
    pub const fn input_origin(&self) -> InputOrigin {
        self.origin
    }
    pub fn input_order(&self) -> &[String] {
        &self.input_order
    }
    /// Only the trusted writer entry point supplies typed datetime values.
    /// Ordinary ISO-looking strings in retained JSON or metadata are not typed.
    pub fn is_datetime_input(&self, field: &str) -> bool {
        self.datetime_fields.contains(&field)
    }
    pub fn len(&self) -> usize {
        self.issues.len()
    }
    pub fn is_empty(&self) -> bool {
        self.issues.is_empty()
    }
    /// Shared input plus encoded issue metadata, for outer aggregate budgets.
    pub const fn retained_bytes(&self) -> usize {
        self.retained_bytes
    }
    pub fn issues(&self) -> impl ExactSizeIterator<Item = ValidationIssueRef<'_>> {
        self.issues.iter().map(|issue| ValidationIssueRef {
            kind: issue.kind,
            loc: &issue.loc,
            msg: &issue.msg,
            input: issue
                .input_field
                .as_ref()
                .map_or(self.input.as_ref(), |field| &self.input[field]),
            ctx: issue.ctx.as_ref(),
        })
    }
    pub fn to_json_bytes(&self, limit: usize) -> Result<Vec<u8>> {
        let mut output = Limited::new(Vec::new(), limit.min(MAX_RECORD_BYTES));
        serde_json::to_writer(&mut output, self).map_err(|_| RecordError::TooLarge)?;
        Ok(output.inner)
    }
    fn add(
        &mut self,
        kind: &'static str,
        field: Option<&str>,
        missing: bool,
        msg: String,
        ctx: Option<Value>,
    ) -> Result<()> {
        if self.issues.len() >= MAX_ISSUES {
            return Err(RecordError::TooLarge);
        }
        let issue = Issue {
            kind,
            loc: field.map_or_else(Vec::new, |field| vec![Location::Field(field.into())]),
            msg,
            input_field: if missing {
                None
            } else {
                field.map(str::to_owned)
            },
            ctx,
        };
        let bytes = encoded_len(&issue, MAX_RECORD_BYTES)?;
        self.retained_bytes = self.retained_bytes.saturating_add(bytes);
        if self.retained_bytes > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        self.issues.push(issue);
        Ok(())
    }
    fn error(self) -> Result<Value> {
        // The same large input may occur in many missing-field errors. Count
        // the wire representation before it can escape, without cloning it.
        encoded_len(&self, MAX_RECORD_BYTES)?;
        Err(RecordError::ModelValidation(self))
    }
}

struct Limited<W> {
    inner: W,
    written: usize,
    limit: usize,
}
impl<W> Limited<W> {
    fn new(inner: W, limit: usize) -> Self {
        Self {
            inner,
            written: 0,
            limit,
        }
    }
}
impl<W: Write> Write for Limited<W> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.written) {
            return Err(std::io::Error::other("retained validation byte limit"));
        }
        self.inner.write_all(bytes)?;
        self.written += bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}
fn encoded_len(value: &impl Serialize, limit: usize) -> Result<usize> {
    let mut output = Limited::new(std::io::sink(), limit);
    serde_json::to_writer(&mut output, value).map_err(|_| RecordError::TooLarge)?;
    Ok(output.written)
}

struct Keys(Vec<String>);
impl<'de> Deserialize<'de> for Keys {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct KeyVisitor;
        impl<'de> Visitor<'de> for KeyVisitor {
            type Value = Keys;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("retained model dictionary")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut access: A,
            ) -> std::result::Result<Keys, A::Error> {
                let mut keys = Vec::new();
                let mut seen = HashSet::new();
                let mut count = 0;
                while let Some((key, _)) = access.next_entry::<String, &'de RawValue>()? {
                    count += 1;
                    if count > MAX_ISSUES {
                        return Err(serde::de::Error::custom("retained model entry limit"));
                    }
                    // JSON duplicate keys take their last value, but Python
                    // dictionaries keep the first insertion position.
                    if seen.insert(key.clone()) {
                        keys.push(key);
                    }
                }
                Ok(Keys(keys))
            }
        }
        deserializer.deserialize_map(KeyVisitor)
    }
}

#[derive(Clone, Copy)]
enum FieldType {
    String { min: usize, max: Option<usize> },
    Integer { min: i64, max: Option<i64> },
    Timestamp { schedule: bool },
    Boolean,
    Archive,
}
#[derive(Clone, Copy)]
struct Field {
    name: &'static str,
    kind: FieldType,
    nullable: bool,
}
const fn text(name: &'static str, min: usize, max: Option<usize>, nullable: bool) -> Field {
    Field {
        name,
        kind: FieldType::String { min, max },
        nullable,
    }
}
const fn timestamp(name: &'static str, schedule: bool, nullable: bool) -> Field {
    Field {
        name,
        kind: FieldType::Timestamp { schedule },
        nullable,
    }
}
// Source Entity.model_fields order, followed by ChatSchedule.model_fields.
const FIELDS: [Field; 16] = [
    text("id", 1, Some(200), false),
    timestamp("created_at", false, false),
    timestamp("updated_at", false, false),
    Field {
        name: "revision",
        kind: FieldType::Integer { min: 1, max: None },
        nullable: false,
    },
    text("engagement_id", 0, None, false),
    text("session_id", 0, None, false),
    text("provider_profile_id", 0, None, false),
    text("model", 0, None, false),
    Field {
        name: "interval_seconds",
        kind: FieldType::Integer {
            min: 3600,
            max: Some(2_592_000),
        },
        nullable: false,
    },
    timestamp("next_run_at", true, false),
    Field {
        name: "enabled",
        kind: FieldType::Boolean,
        nullable: false,
    },
    Field {
        name: "paused_by",
        kind: FieldType::Archive,
        nullable: true,
    },
    timestamp("last_run_at", true, true),
    text("last_turn_id", 0, Some(200), true),
    text("last_status", 0, Some(40), true),
    text("skip_reason", 0, Some(1000), true),
];

struct Failure {
    kind: &'static str,
    msg: String,
    ctx: Option<Value>,
}
impl Failure {
    fn simple(kind: &'static str, msg: impl Into<String>) -> Self {
        Self {
            kind,
            msg: msg.into(),
            ctx: None,
        }
    }
    fn context(kind: &'static str, msg: impl Into<String>, ctx: Value) -> Self {
        Self {
            kind,
            msg: msg.into(),
            ctx: Some(ctx),
        }
    }
    fn value(msg: &'static str) -> Self {
        Self::context(
            "value_error",
            format!("Value error, {msg}"),
            json!({"error":{}}),
        )
    }
}
type FieldResult = std::result::Result<Value, Failure>;

/// Hydrate one of the complete retained contracts, preserving field-error order
/// and original diagnostic input. Missing factory-backed identity/time fields
/// remain a strict retained-data error even though Python can invent defaults.
pub fn hydrate(model: Model, origin: InputOrigin, bytes: &[u8]) -> Result<Value> {
    if bytes.len() > MAX_RECORD_BYTES {
        return Err(RecordError::TooLarge);
    }
    let mut input: Value = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
    if input.is_object() {
        for field in ["id", "revision", "created_at", "updated_at"] {
            if input.get(field).is_none() {
                return Err(RecordError::Shape(model.kind()));
            }
        }
    }
    let mut keys = if input.is_object() {
        serde_json::from_slice::<Keys>(bytes)
            .map_err(|_| RecordError::TooLarge)?
            .0
    } else {
        Vec::new()
    };
    let datetime_fields = if origin == InputOrigin::WriterModelDump {
        let datetime_fields = writer_datetime_inputs(model, &mut input);
        let mut ordered: Vec<String> = model
            .fields()
            .iter()
            .filter(|field| input.get(field.name).is_some())
            .map(|field| field.name.into())
            .collect();
        ordered.extend(
            keys.into_iter()
                .filter(|key| !model.fields().iter().any(|field| field.name == key)),
        );
        keys = ordered;
        datetime_fields
    } else {
        Vec::new()
    };
    let input_bytes = encoded_len(&input, MAX_RECORD_BYTES)?;
    let retained_bytes = input_bytes
        .saturating_add(encoded_len(&keys, MAX_RECORD_BYTES)?)
        .saturating_add(encoded_len(&datetime_fields, MAX_RECORD_BYTES)?);
    if retained_bytes > MAX_RECORD_BYTES {
        return Err(RecordError::TooLarge);
    }
    let keys: Arc<[String]> = keys.into();
    let input = Arc::new(input);
    let mut report = ValidationReport {
        model,
        origin,
        input: input.clone(),
        input_order: keys.clone(),
        datetime_fields,
        issues: Vec::new(),
        retained_bytes,
    };
    let Some(fields) = input.as_object() else {
        report.add(
            "model_type",
            None,
            false,
            format!(
                "Input should be a valid dictionary or instance of {}",
                model.name()
            ),
            Some(json!({"class_name":model.name()})),
        )?;
        return report.error();
    };
    let mut output = Map::new();
    for field in model.fields() {
        let Some(value) = fields.get(field.name) else {
            if field.name == "enabled" {
                output.insert(field.name.into(), true.into());
            } else if field.nullable {
                output.insert(field.name.into(), Value::Null);
            } else {
                report.add(
                    "missing",
                    Some(field.name),
                    true,
                    "Field required".into(),
                    None,
                )?;
            }
            continue;
        };
        match validate_field(*field, value) {
            Ok(value) => {
                output.insert(field.name.into(), value);
            }
            Err(error) => report.add(error.kind, Some(field.name), false, error.msg, error.ctx)?,
        }
    }
    for key in keys.iter() {
        if !model.fields().iter().any(|field| field.name == key) {
            report.add(
                "extra_forbidden",
                Some(key),
                false,
                "Extra inputs are not permitted".into(),
                None,
            )?;
        }
    }
    if !report.is_empty() {
        return report.error();
    }
    let created =
        DateTime::parse_from_rfc3339(output["created_at"].as_str().ok_or(RecordError::Schema)?)
            .map_err(|_| RecordError::Schema)?;
    let updated =
        DateTime::parse_from_rfc3339(output["updated_at"].as_str().ok_or(RecordError::Schema)?)
            .map_err(|_| RecordError::Schema)?;
    if updated < created {
        let error = Failure::value("updated_at cannot be earlier than created_at");
        report.add(error.kind, None, false, error.msg, error.ctx)?;
        return report.error();
    }
    let output = Value::Object(output);
    encoded_len(&output, MAX_RECORD_BYTES)?;
    Ok(output)
}

fn writer_datetime_inputs(model: Model, input: &mut Value) -> Vec<&'static str> {
    let mut typed = Vec::new();
    if !input.is_object() {
        return typed;
    }
    for field in model
        .fields()
        .iter()
        .filter(|f| matches!(f.kind, FieldType::Timestamp { .. }))
    {
        let Some(value) = input.get_mut(field.name) else {
            continue;
        };
        let Some(text) = value.as_str() else {
            continue;
        };
        let normalized = if let Ok(time) = DateTime::parse_from_rfc3339(text) {
            Some(time.to_rfc3339_opts(
                if time.timestamp_subsec_micros() == 0 {
                    SecondsFormat::Secs
                } else {
                    SecondsFormat::Micros
                },
                false,
            ))
        } else if let Ok(time) = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f") {
            Some(
                time.format(if time.and_utc().timestamp_subsec_micros() == 0 {
                    "%Y-%m-%dT%H:%M:%S"
                } else {
                    "%Y-%m-%dT%H:%M:%S%.6f"
                })
                .to_string(),
            )
        } else {
            None
        };
        if let Some(normalized) = normalized {
            *value = normalized.into();
            typed.push(field.name);
        }
    }
    typed
}

fn validate_field(field: Field, value: &Value) -> FieldResult {
    if field.nullable && value.is_null() {
        return Ok(Value::Null);
    }
    match field.kind {
        FieldType::String { min, max } => {
            let Some(text) = value.as_str() else {
                return Err(Failure::simple(
                    "string_type",
                    "Input should be a valid string",
                ));
            };
            // Pydantic's str_strip_whitespace uses Unicode White_Space, unlike
            // Python str.strip which also strips U+001C through U+001F.
            let text = text.trim();
            let len = text.chars().count();
            if len < min {
                return Err(Failure::context(
                    "string_too_short",
                    format!(
                        "String should have at least {min} character{}",
                        if min == 1 { "" } else { "s" }
                    ),
                    json!({"min_length":min}),
                ));
            }
            if let Some(max) = max.filter(|max| len > *max) {
                return Err(Failure::context(
                    "string_too_long",
                    format!(
                        "String should have at most {max} character{}",
                        if max == 1 { "" } else { "s" }
                    ),
                    json!({"max_length":max}),
                ));
            }
            Ok(text.into())
        }
        FieldType::Integer { min, max } => {
            let number = integer(value)?;
            let text = number.to_string();
            if text.starts_with('-') || number.as_i64().is_some_and(|value| value < min) {
                return Err(Failure::context(
                    "greater_than_equal",
                    format!("Input should be greater than or equal to {min}"),
                    json!({"ge":min}),
                ));
            }
            if let Some(max) = max.filter(|max| number.as_i64().is_none_or(|value| value > *max)) {
                return Err(Failure::context(
                    "less_than_equal",
                    format!("Input should be less than or equal to {max}"),
                    json!({"le":max}),
                ));
            }
            Ok(number)
        }
        FieldType::Boolean => boolean(value).map(Value::Bool),
        FieldType::Archive => {
            if value == "archive" {
                Ok(value.clone())
            } else {
                Err(Failure::context(
                    "literal_error",
                    "Input should be 'archive'",
                    json!({"expected":"'archive'"}),
                ))
            }
        }
        FieldType::Timestamp { schedule } => {
            let parsed = datetime(value)?;
            let aware = DateTime::parse_from_rfc3339(&parsed).map_err(|_| {
                Failure::value(if schedule {
                    "schedule times must include a timezone"
                } else {
                    "timestamps must include a timezone"
                })
            })?;
            let utc = aware.with_timezone(&Utc);
            Ok(utc
                .to_rfc3339_opts(
                    if utc.timestamp_subsec_micros() == 0 {
                        SecondsFormat::Secs
                    } else {
                        SecondsFormat::Micros
                    },
                    true,
                )
                .into())
        }
    }
}

fn boolean(value: &Value) -> std::result::Result<bool, Failure> {
    match value {
        Value::Bool(value) => return Ok(*value),
        Value::Number(number) => {
            if number.as_f64() == Some(1.0) {
                return Ok(true);
            }
            if number.as_f64() == Some(0.0) {
                return Ok(false);
            }
            if number
                .as_f64()
                .is_none_or(|n| !n.is_finite() || n.fract() != 0.0)
            {
                return Err(Failure::simple(
                    "bool_type",
                    "Input should be a valid boolean",
                ));
            }
        }
        Value::String(text) => match text.to_ascii_lowercase().as_str() {
            "1" | "true" | "t" | "yes" | "y" | "on" => return Ok(true),
            "0" | "false" | "f" | "no" | "n" | "off" => return Ok(false),
            _ => {}
        },
        _ => {
            return Err(Failure::simple(
                "bool_type",
                "Input should be a valid boolean",
            ));
        }
    }
    Err(Failure::simple(
        "bool_parsing",
        "Input should be a valid boolean, unable to interpret input",
    ))
}

fn integer(value: &Value) -> FieldResult {
    let parse = || {
        Failure::simple(
            "int_parsing",
            "Input should be a valid integer, unable to parse string as an integer",
        )
    };
    let size = || {
        Failure::simple(
            "int_parsing_size",
            "Unable to parse input string as an integer, exceeded maximum size",
        )
    };
    if let Value::Bool(value) = value {
        return Ok(json!(i64::from(*value)));
    }
    if let Value::Number(value) = value {
        if value.as_i64() == Some(0) || value.as_f64() == Some(0.0) {
            return Ok(json!(0));
        }
        let raw = value.to_string();
        if !raw.contains(['.', 'e', 'E']) {
            return Ok(Value::Number(value.clone()));
        }
        let number = value.as_f64().ok_or_else(size)?;
        if !number.is_finite() {
            return Err(Failure::simple(
                "finite_number",
                "Input should be a finite number",
            ));
        }
        if number.fract() != 0.0 {
            return Err(Failure::simple(
                "int_from_float",
                "Input should be a valid integer, got a number with a fractional part",
            ));
        }
        if number <= i64::MIN as f64 || number >= i64::MAX as f64 {
            return Err(size());
        }
        return format!("{number:.0}")
            .parse::<serde_json::Number>()
            .map(Value::Number)
            .map_err(|_| size());
    }
    let Some(raw) = value.as_str() else {
        return Err(Failure::simple(
            "int_type",
            "Input should be a valid integer",
        ));
    };
    if raw.len() > 4300 {
        return Err(size());
    }
    let mut text = raw.trim();
    if let Some(rest) = text.strip_prefix('+') {
        if rest.starts_with('-') {
            return Err(parse());
        }
        text = rest;
    }
    let negative = text.starts_with('-');
    if negative {
        text = &text[1..];
    }
    if text.is_empty() || !text.as_bytes()[0].is_ascii_digit() {
        return Err(parse());
    }
    while text.len() > 1
        && (text.starts_with('0') || text.starts_with('_'))
        && !text[1..].starts_with('.')
    {
        text = &text[1..];
    }
    if let Some((whole, fraction)) = text.split_once('.') {
        if fraction.is_empty() || !fraction.bytes().all(|b| b == b'0') {
            return Err(parse());
        }
        text = whole;
    }
    if text.starts_with('_')
        || text.ends_with('_')
        || text.contains("__")
        || !text.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return Err(parse());
    }
    let digits = text.replace('_', "");
    let digits = digits.trim_start_matches('0');
    let canonical = if digits.is_empty() {
        "0".into()
    } else {
        format!("{}{digits}", if negative { "-" } else { "" })
    };
    canonical
        .parse::<serde_json::Number>()
        .map(Value::Number)
        .map_err(|_| parse())
}

fn datetime(value: &Value) -> std::result::Result<String, Failure> {
    let failure = |kind: &'static str, detail: Option<&'static str>| match detail {
        Some(detail) => Failure::context(
            kind,
            format!(
                "Input should be a valid datetime{}, {detail}",
                if kind == "datetime_from_date_parsing" {
                    " or date"
                } else {
                    ""
                }
            ),
            json!({"error":detail}),
        ),
        None => Failure::simple(kind, "Input should be a valid datetime"),
    };
    let config = DateTimeConfig::builder()
        .time_config(
            TimeConfig::builder()
                .unix_timestamp_offset(Some(0))
                .microseconds_precision_overflow_behavior(
                    MicrosecondsPrecisionOverflowBehavior::Truncate,
                )
                .build(),
        )
        .build();
    let parsed = match value {
        Value::String(text) => match ParsedDateTime::parse_str_with_config(text, &config) {
            Ok(date) => Ok(date),
            Err(_) => match Date::parse_str(text) {
                Ok(date) if date.year == 0 => {
                    return Err(failure("datetime_parsing", Some("year 0 is out of range")));
                }
                Ok(date) => return Ok(format!("{date}T00:00:00")),
                Err(error) => {
                    return Err(failure(
                        "datetime_from_date_parsing",
                        error.get_documentation(),
                    ));
                }
            },
        },
        Value::Number(number) => ParsedDateTime::from_float_with_config(
            number.as_f64().unwrap_or(f64::INFINITY),
            &config,
        ),
        _ => return Err(failure("datetime_type", None)),
    };
    parsed
        .map_err(|error| failure("datetime_parsing", error.get_documentation()))
        .and_then(|date| {
            if date.date.year == 0 {
                Err(failure("datetime_parsing", Some("year 0 is out of range")))
            } else {
                Ok(date.to_string())
            }
        })
}
