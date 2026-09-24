//! Python-compatible JSON field validation for Assistant context requests.
//! Unknown fields retain the existing BaseModel behavior: they are ignored.
use crate::ApiError;
use nebula_assistant_services::context::{CursorWrite, DecisionWrite};
use nebula_assistant_services::generated::GeneratedListRequest;
use nebula_assistant_services::goal_conversations::GoalConversationCreate;
use nebula_assistant_services::goal_drafts::{GoalDraft, GoalDraftUpdate};
use nebula_assistant_services::navigation::{BookmarkWrite, SearchRequest};
use nebula_assistant_services::results::ResultsQuery;
use nebula_assistant_services::settings::{ScheduleCreate, ScheduleWrite, SettingsWrite};
use serde_json::{Map, Value, json};
use speedate::{Date, DateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior, TimeConfig};
use strum::EnumMessage;

mod goals;
mod settings;
pub(crate) use settings::key_order;

pub(crate) trait RequestModel: serde::de::DeserializeOwned {
    const MODEL: BodyModel;
}
#[derive(PartialEq)]
pub(crate) enum BodyModel {
    Cursor,
    Decision,
    Bookmark,
    Settings,
    ScheduleCreate,
    ScheduleWrite,
    GoalCreate,
    GoalUpdate,
    GoalConversationCreate,
}
impl RequestModel for CursorWrite {
    const MODEL: BodyModel = BodyModel::Cursor;
}
impl RequestModel for DecisionWrite {
    const MODEL: BodyModel = BodyModel::Decision;
}
impl RequestModel for BookmarkWrite {
    const MODEL: BodyModel = BodyModel::Bookmark;
}
impl RequestModel for SettingsWrite {
    const MODEL: BodyModel = BodyModel::Settings;
}
impl RequestModel for ScheduleCreate {
    const MODEL: BodyModel = BodyModel::ScheduleCreate;
}
impl RequestModel for ScheduleWrite {
    const MODEL: BodyModel = BodyModel::ScheduleWrite;
}
impl RequestModel for GoalDraft {
    const MODEL: BodyModel = BodyModel::GoalCreate;
}
impl RequestModel for GoalDraftUpdate {
    const MODEL: BodyModel = BodyModel::GoalUpdate;
}
impl RequestModel for GoalConversationCreate {
    const MODEL: BodyModel = BodyModel::GoalConversationCreate;
}

fn error(
    kind: &str,
    field: Option<&str>,
    message: String,
    input: &Value,
    context: Option<Value>,
) -> Value {
    let mut location = vec![json!("body")];
    if let Some(field) = field {
        location.push(json!(field));
    }
    let mut value = json!({"type":kind,"loc":location,"msg":message,"input":input});
    if let Some(context) = context {
        value["ctx"] = context;
    }
    value
}
fn field_error(kind: &str, field: &str, message: &str, input: &Value) -> Value {
    error(kind, Some(field), message.into(), input, None)
}

pub(crate) fn validate<T: RequestModel>(input: Value, key_order: &[String]) -> Result<T, ApiError> {
    let Some(fields) = input.as_object() else {
        return Err(ApiError::validation(vec![error(
            if input.is_null() {
                "missing"
            } else {
                "model_attributes_type"
            },
            None,
            if input.is_null() {
                "Field required"
            } else {
                "Input should be a valid dictionary or object to extract fields from"
            }
            .into(),
            &input,
            None,
        )]));
    };
    if matches!(
        T::MODEL,
        BodyModel::Settings | BodyModel::ScheduleCreate | BodyModel::ScheduleWrite
    ) {
        return serde_json::from_value(settings::validate(&T::MODEL, &input, fields, key_order)?)
            .map_err(|_| {
                ApiError::http(
                    422,
                    "Assistant request cannot be represented by its validated contract",
                )
            });
    }
    if matches!(
        T::MODEL,
        BodyModel::GoalCreate | BodyModel::GoalUpdate | BodyModel::GoalConversationCreate
    ) {
        return serde_json::from_value(goals::validate(&T::MODEL, &input, fields)?).map_err(|_| {
            ApiError::http(
                422,
                "Assistant request cannot be represented by its validated contract",
            )
        });
    }
    let mut output = Map::new();
    let mut errors = Vec::new();
    if T::MODEL == BodyModel::Bookmark {
        if let Some(value) = fields.get("active") {
            match boolean(value) {
                Ok(active) => {
                    output.insert("active".into(), active.into());
                }
                Err((kind, message)) => errors.push(field_error(kind, "active", message, value)),
            }
        } else {
            errors.push(field_error("missing", "active", "Field required", &input));
        }
    }
    let mut required = |name: &str| {
        if let Some(value) = fields.get(name) {
            Some(value)
        } else {
            errors.push(field_error("missing", name, "Field required", &input));
            None
        }
    };
    if let Some(value) = required("expected_revision") {
        match integer(value) {
            Ok(number) if number.to_string().starts_with('-') => errors.push(error(
                "greater_than_equal",
                Some("expected_revision"),
                "Input should be greater than or equal to 0".into(),
                value,
                Some(json!({"ge":0})),
            )),
            Ok(number) => {
                output.insert("expected_revision".into(), number);
            }
            Err((kind, message)) => {
                errors.push(field_error(kind, "expected_revision", message, value))
            }
        }
    }
    if T::MODEL == BodyModel::Cursor {
        if let Some(value) = fields.get("through_at") {
            match datetime(value) {
                Ok(stamp) => {
                    output.insert("through_at".into(), stamp.into());
                }
                Err((kind, detail)) => {
                    let (message, context) = match detail {
                        Some(detail) => (
                            format!(
                                "Input should be a valid datetime{}, {detail}",
                                if kind == "datetime_from_date_parsing" {
                                    " or date"
                                } else {
                                    ""
                                }
                            ),
                            Some(json!({"error":detail})),
                        ),
                        None => ("Input should be a valid datetime".into(), None),
                    };
                    errors.push(error(kind, Some("through_at"), message, value, context));
                }
            }
        } else {
            errors.push(field_error(
                "missing",
                "through_at",
                "Field required",
                &input,
            ));
        }
        if let Some(value) = fields.get("device_id") {
            string(
                &mut output,
                &mut errors,
                "device_id",
                value,
                false,
                Some(1),
                Some(200),
                None,
            );
        } else {
            errors.push(field_error(
                "missing",
                "device_id",
                "Field required",
                &input,
            ));
        }
    } else if T::MODEL == BodyModel::Decision {
        for (name, nullable, max, pattern) in [
            (
                "action",
                false,
                None,
                Some("^(save|supersede|remove|promote)$"),
            ),
            (
                "kind",
                false,
                None,
                Some("^(decision|constraint|assumption|question)$"),
            ),
            ("text", false, Some(4000), None),
            ("source_message_id", true, None, None),
            ("source_selection", true, Some(200000), None),
        ] {
            if let Some(value) = fields.get(name) {
                string(
                    &mut output,
                    &mut errors,
                    name,
                    value,
                    nullable,
                    None,
                    max,
                    pattern,
                );
            }
        }
    }
    if !errors.is_empty() {
        return Err(ApiError::validation(errors));
    }
    serde_json::from_value(Value::Object(output)).map_err(|_| {
        ApiError::http(
            422,
            "Assistant request cannot be represented by its validated contract",
        )
    })
}

#[allow(clippy::too_many_arguments)]
fn string(
    output: &mut Map<String, Value>,
    errors: &mut Vec<Value>,
    field: &str,
    value: &Value,
    nullable: bool,
    min: Option<usize>,
    max: Option<usize>,
    pattern: Option<&str>,
) {
    if nullable && value.is_null() {
        output.insert(field.into(), Value::Null);
        return;
    }
    let Some(text) = value.as_str() else {
        errors.push(field_error(
            "string_type",
            field,
            "Input should be a valid string",
            value,
        ));
        return;
    };
    let len = text.chars().count();
    if let Some(min) = min.filter(|min| len < *min) {
        errors.push(error(
            "string_too_short",
            Some(field),
            format!(
                "String should have at least {min} character{}",
                if min == 1 { "" } else { "s" }
            ),
            value,
            Some(json!({"min_length":min})),
        ));
    } else if let Some(max) = max.filter(|max| len > *max) {
        errors.push(error(
            "string_too_long",
            Some(field),
            format!("String should have at most {max} characters"),
            value,
            Some(json!({"max_length":max})),
        ));
    } else if let Some(pattern) =
        pattern.filter(|p| !p[2..p.len() - 2].split('|').any(|choice| choice == text))
    {
        errors.push(error(
            "string_pattern_mismatch",
            Some(field),
            format!("String should match pattern '{pattern}'"),
            value,
            Some(json!({"pattern":pattern})),
        ));
    } else {
        output.insert(field.into(), value.clone());
    }
}

type IntegerError = (&'static str, &'static str);

fn boolean(value: &Value) -> Result<bool, IntegerError> {
    match value {
        Value::Bool(value) => return Ok(*value),
        Value::Number(number) => {
            // Pydantic converts numeric booleans through a signed i64. Keep
            // integer spelling: i64::MAX rounds to 2^63 if converted to f64.
            let signed_integer = if number.to_string().contains(['.', 'e', 'E']) {
                number.as_f64().is_some_and(|n| {
                    n.is_finite()
                        && n.fract() == 0.0
                        && n > -9_223_372_036_854_775_808.0
                        && n < 9_223_372_036_854_775_808.0
                })
            } else {
                number.as_i64().is_some()
            };
            if !signed_integer {
                return Err(("bool_type", "Input should be a valid boolean"));
            }
            if number.as_f64() == Some(1.0) {
                return Ok(true);
            }
            if number.as_f64() == Some(0.0) {
                return Ok(false);
            }
        }
        Value::String(text) => match text.to_ascii_lowercase().as_str() {
            "1" | "true" | "t" | "yes" | "y" | "on" => return Ok(true),
            "0" | "false" | "f" | "no" | "n" | "off" => return Ok(false),
            _ => {}
        },
        _ => return Err(("bool_type", "Input should be a valid boolean")),
    }
    Err((
        "bool_parsing",
        "Input should be a valid boolean, unable to interpret input",
    ))
}

// Starlette scalar query parameters use the last repeated value and decode
// form-style '+' and invalid UTF-8 the same way as form_urlencoded.
fn query_fields(query: Option<&str>) -> Result<Map<String, Value>, ApiError> {
    let query = query.unwrap_or_default();
    if query.len() > 65536 {
        return Err(ApiError::http(
            414,
            "Assistant query exceeds its configured limit",
        ));
    }
    Ok(form_urlencoded::parse(query.as_bytes())
        .map(|(key, value)| (key.into_owned(), Value::String(value.into_owned())))
        .collect())
}
fn query_errors(mut errors: Vec<Value>) -> ApiError {
    for error in &mut errors {
        error["loc"][0] = "query".into();
    }
    ApiError::validation(errors)
}

pub(crate) fn include_replaced(query: Option<&str>) -> Result<bool, ApiError> {
    let fields = query_fields(query)?;
    fields.get("include_replaced").map_or(Ok(false), |value| {
        boolean(value).map_err(|(kind, message)| {
            query_errors(vec![field_error(kind, "include_replaced", message, value)])
        })
    })
}

pub(crate) fn search(query: Option<&str>) -> Result<SearchRequest, ApiError> {
    let fields = query_fields(query)?;
    let mut output = Map::new();
    let mut errors = Vec::new();
    let q = fields.get("q").cloned().unwrap_or_else(|| json!(""));
    string(
        &mut output,
        &mut errors,
        "q",
        &q,
        false,
        None,
        Some(512),
        None,
    );
    let bookmarked = fields.get("bookmarked").is_some_and(|value| {
        boolean(value).unwrap_or_else(|(kind, message)| {
            errors.push(field_error(kind, "bookmarked", message, value));
            false
        })
    });
    pagination(
        &fields,
        &mut output,
        &mut errors,
        50,
        100,
        i64::MAX as u64 - 101,
    )?;
    if !errors.is_empty() {
        return Err(query_errors(errors));
    }
    Ok(SearchRequest {
        q: output["q"].as_str().expect("validated text").into(),
        session_id: fields
            .get("session_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        bookmarked,
        offset: output["offset"].as_u64().expect("validated offset"),
        limit: output["limit"].as_u64().expect("validated limit") as u32,
    })
}

pub(crate) fn catalog(query: Option<&str>) -> Result<GeneratedListRequest, ApiError> {
    let fields = query_fields(query)?;
    let mut output = Map::new();
    let mut errors = Vec::new();
    pagination(
        &fields,
        &mut output,
        &mut errors,
        100,
        1000,
        i64::MAX as u64,
    )?;
    if !errors.is_empty() {
        return Err(query_errors(errors));
    }
    Ok(GeneratedListRequest {
        engagement_id: fields
            .get("engagement_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        offset: output["offset"].as_u64().expect("validated offset"),
        limit: output["limit"].as_u64().expect("validated limit") as u32,
    })
}

pub(crate) fn catchup_device(query: Option<&str>) -> Result<String, ApiError> {
    let fields = query_fields(query)?;
    let mut output = Map::new();
    let mut errors = Vec::new();
    if let Some(device) = fields.get("device_id") {
        string(
            &mut output,
            &mut errors,
            "device_id",
            device,
            false,
            Some(1),
            Some(200),
            None,
        );
    } else {
        errors.push(field_error(
            "missing",
            "device_id",
            "Field required",
            &Value::Null,
        ));
    }
    if !errors.is_empty() {
        return Err(query_errors(errors));
    }
    Ok(output["device_id"]
        .as_str()
        .expect("validated device")
        .into())
}

pub(crate) fn activity_project(query: Option<&str>) -> Result<String, ApiError> {
    let fields = query_fields(query)?;
    fields
        .get("engagement_id")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .ok_or_else(|| {
            query_errors(vec![field_error(
                "missing",
                "engagement_id",
                "Field required",
                &Value::Null,
            )])
        })
}

pub(crate) fn results(query: Option<&str>) -> Result<ResultsQuery, ApiError> {
    let fields = query_fields(query)?;
    let mut output = Map::new();
    let mut errors = Vec::new();
    pagination(&fields, &mut output, &mut errors, 40, 100, i64::MAX as u64)?;
    if !errors.is_empty() {
        return Err(query_errors(errors));
    }
    Ok(ResultsQuery {
        offset: output["offset"].as_u64().expect("validated offset"),
        limit: output["limit"].as_u64().expect("validated limit") as u32,
    })
}

pub(crate) fn context_offset(query: Option<&str>) -> Result<u64, ApiError> {
    let mut fields = query_fields(query)?;
    // This route accepts only offset; a supplied limit is an ignored query key.
    fields.retain(|name, _| name == "offset");
    let mut output = Map::new();
    let mut errors = Vec::new();
    pagination(&fields, &mut output, &mut errors, 40, 40, i64::MAX as u64)?;
    if !errors.is_empty() {
        return Err(query_errors(errors));
    }
    Ok(output["offset"].as_u64().expect("validated offset"))
}

fn pagination(
    fields: &Map<String, Value>,
    output: &mut Map<String, Value>,
    errors: &mut Vec<Value>,
    default_limit: u64,
    max_limit: u64,
    max_offset: u64,
) -> Result<(), ApiError> {
    let mut unsupported_offset = false;
    for (name, default, minimum, maximum) in [
        ("offset", 0_u64, 0_u64, None),
        ("limit", default_limit, 1, Some(max_limit)),
    ] {
        let Some(value) = fields.get(name) else {
            output.insert(name.into(), default.into());
            continue;
        };
        match integer(value) {
            Ok(number) => {
                let nonnegative = !number.to_string().starts_with('-');
                let numeric = number.as_u64();
                if !nonnegative || numeric.is_some_and(|n| n < minimum) {
                    errors.push(error(
                        "greater_than_equal",
                        Some(name),
                        format!("Input should be greater than or equal to {minimum}"),
                        value,
                        Some(json!({"ge":minimum})),
                    ));
                } else if let Some(max) = maximum.filter(|max| numeric.is_none_or(|n| n > *max)) {
                    errors.push(error(
                        "less_than_equal",
                        Some(name),
                        format!("Input should be less than or equal to {max}"),
                        value,
                        Some(json!({"le":max})),
                    ));
                } else if numeric.is_none_or(|n| n > max_offset) {
                    unsupported_offset = true;
                } else {
                    output.insert(name.into(), number);
                }
            }
            Err((kind, message)) => errors.push(field_error(kind, name, message, value)),
        }
    }
    // Model validation precedes the database's narrower integer range. Keep
    // ordinary field errors authoritative even when the offset cannot be bound.
    if unsupported_offset && errors.is_empty() {
        return Err(ApiError::http(
            422,
            "Assistant offset exceeds supported storage bounds",
        ));
    }
    Ok(())
}

fn integer(value: &Value) -> Result<Value, IntegerError> {
    const PARSE: IntegerError = (
        "int_parsing",
        "Input should be a valid integer, unable to parse string as an integer",
    );
    const SIZE: IntegerError = (
        "int_parsing_size",
        "Unable to parse input string as an integer, exceeded maximum size",
    );
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
        let number = value.as_f64().ok_or(SIZE)?;
        if !number.is_finite() {
            return Err(("finite_number", "Input should be a finite number"));
        }
        if number.fract() != 0.0 {
            return Err((
                "int_from_float",
                "Input should be a valid integer, got a number with a fractional part",
            ));
        }
        // Pydantic accepts arbitrary-size JSON integer tokens, but its float
        // conversion excludes both endpoints of the signed 64-bit range.
        if number <= i64::MIN as f64 || number >= i64::MAX as f64 {
            return Err(SIZE);
        }
        return format!("{number:.0}")
            .parse::<serde_json::Number>()
            .map(Value::Number)
            .map_err(|_| SIZE);
    }
    let Some(raw) = value.as_str() else {
        return Err(("int_type", "Input should be a valid integer"));
    };
    if raw.len() > 4300 {
        return Err(SIZE);
    }
    let mut text = raw.trim();
    if let Some(rest) = text.strip_prefix('+') {
        if rest.starts_with('-') {
            return Err(PARSE);
        }
        text = rest;
    }
    let negative = text.starts_with('-');
    if negative {
        text = &text[1..];
    }
    if text.is_empty() || !text.as_bytes()[0].is_ascii_digit() {
        return Err(PARSE);
    }
    // Pydantic permits underscores within leading zero padding, including
    // repeated underscores there. Remaining digits use single separators.
    while text.len() > 1
        && (text.starts_with('0') || text.starts_with('_'))
        && !text[1..].starts_with('.')
    {
        text = &text[1..];
    }
    if let Some((whole, fraction)) = text.split_once('.') {
        if fraction.is_empty() || !fraction.bytes().all(|b| b == b'0') {
            return Err(PARSE);
        }
        text = whole;
    }
    if text.starts_with('_')
        || text.ends_with('_')
        || text.contains("__")
        || !text.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return Err(PARSE);
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
        .map_err(|_| PARSE)
}

fn datetime(value: &Value) -> Result<String, (&'static str, Option<&'static str>)> {
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
        Value::String(text) => match DateTime::parse_str_with_config(text, &config) {
            Ok(date) => Ok(date),
            Err(_) => match Date::parse_str(text) {
                Ok(date) if date.year == 0 => {
                    return Err(("datetime_parsing", Some("year 0 is out of range")));
                }
                Ok(date) => return Ok(format!("{date}T00:00:00")),
                Err(error) => {
                    return Err(("datetime_from_date_parsing", error.get_documentation()));
                }
            },
        },
        Value::Number(number) => {
            DateTime::from_float_with_config(number.as_f64().unwrap_or(f64::INFINITY), &config)
        }
        _ => return Err(("datetime_type", None)),
    };
    parsed
        .map_err(|error| ("datetime_parsing", error.get_documentation()))
        .and_then(|date| {
            if date.date.year == 0 {
                Err(("datetime_parsing", Some("year 0 is out of range")))
            } else {
                Ok(date.to_string())
            }
        })
}
