//! Preserve the distinct NebulaModel and plain BaseModel settings contracts.
use super::{ApiError, BodyModel, boolean, error, field_error, integer};
use serde::de::{IgnoredAny, MapAccess, Visitor};
use serde_json::{Map, Value, json};
use std::collections::HashSet;

const FIELDS: [&str; 11] = [
    "title",
    "archived",
    "mcp_server_ids",
    "hook_ids",
    "reasoning_effort",
    "allow_subagents",
    "allow_agent_messaging",
    "max_active_subagents",
    "subagent_provider_id",
    "subagent_model",
    "expected_revision",
];

/// Extra-field errors follow the incoming JSON order, including duplicate-key
/// first insertion semantics. This does not enable preserve_order globally.
pub(crate) fn key_order(bytes: &[u8]) -> Vec<String> {
    struct Keys;
    impl<'de> Visitor<'de> for Keys {
        type Value = Vec<String>;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("a request object")
        }
        fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
            let mut keys = Vec::new();
            let mut seen = HashSet::new();
            while let Some((key, _)) = map.next_entry::<String, IgnoredAny>()? {
                if seen.insert(key.clone()) {
                    keys.push(key);
                }
            }
            Ok(keys)
        }
    }
    serde::Deserializer::deserialize_map(&mut serde_json::Deserializer::from_slice(bytes), Keys)
        .unwrap_or_default()
}

pub(super) fn validate(
    model: &BodyModel,
    input: &Value,
    fields: &Map<String, Value>,
    key_order: &[String],
) -> Result<Value, ApiError> {
    let mut output = Map::new();
    let mut errors = Vec::new();
    if *model == BodyModel::Settings {
        for name in FIELDS {
            let Some(value) = fields.get(name) else {
                continue;
            };
            if value.is_null() {
                output.insert(name.into(), Value::Null);
                continue;
            }
            match name {
                "title" | "subagent_provider_id" | "subagent_model" => {
                    let (min, max) = match name {
                        "title" => (Some(1), 300),
                        "subagent_provider_id" => (None, 200),
                        _ => (None, 300),
                    };
                    let normalized = value.as_str().map(|s| Value::from(s.trim()));
                    let before = errors.len();
                    super::string(
                        &mut output,
                        &mut errors,
                        name,
                        normalized.as_ref().unwrap_or(value),
                        false,
                        min,
                        Some(max),
                        None,
                    );
                    for error in &mut errors[before..] {
                        error["input"] = value.clone();
                    }
                }
                "archived" | "allow_subagents" | "allow_agent_messaging" => {
                    bool_field(&mut output, &mut errors, name, value);
                }
                "mcp_server_ids" | "hook_ids" => {
                    list_field(
                        &mut output,
                        &mut errors,
                        name,
                        value,
                        if name == "hook_ids" { 32 } else { 64 },
                    );
                }
                "reasoning_effort" => {
                    let expected = "'none', 'minimal', 'low', 'medium', 'high' or 'xhigh'";
                    if value.as_str().is_some_and(|s| {
                        ["none", "minimal", "low", "medium", "high", "xhigh"].contains(&s)
                    }) {
                        output.insert(name.into(), value.clone());
                    } else {
                        errors.push(error(
                            "literal_error",
                            Some(name),
                            format!("Input should be {expected}"),
                            value,
                            Some(json!({"expected":expected})),
                        ));
                    }
                }
                "max_active_subagents" | "expected_revision" => {
                    int_field(
                        &mut output,
                        &mut errors,
                        name,
                        value,
                        1,
                        (name == "max_active_subagents").then_some(100),
                    );
                }
                _ => unreachable!(),
            }
        }
        // The caller supplies original order after a bounded JSON parse. Fall
        // back to map order only for trusted callers without source bytes.
        let keys: Vec<&String> = if key_order.is_empty() {
            fields.keys().collect()
        } else {
            key_order.iter().collect()
        };
        for key in keys {
            if !FIELDS.contains(&key.as_str()) {
                errors.push(field_error(
                    "extra_forbidden",
                    key,
                    "Extra inputs are not permitted",
                    &fields[key],
                ));
            }
        }
        if errors.is_empty() {
            let supplied = FIELDS[..10].iter().any(|name| {
                output.get(*name).is_some_and(|v| !v.is_null())
                    || (["reasoning_effort", "max_active_subagents"].contains(name)
                        && output.contains_key(*name))
            });
            let mut detail =
                (!supplied).then_some("Provide a title, archived state, or assistant settings");
            if detail.is_none() {
                for (name, duplicate) in [
                    ("mcp_server_ids", "MCP server selection contains duplicates"),
                    ("hook_ids", "hook selection contains duplicates"),
                ] {
                    if let Some(values) = output.get(name).and_then(Value::as_array) {
                        let mut unique = HashSet::new();
                        if values
                            .iter()
                            .any(|v| !unique.insert(v.as_str().expect("normalized string list")))
                        {
                            detail = Some(duplicate);
                            break;
                        }
                    }
                }
            }
            if let Some(detail) = detail {
                errors.push(error(
                    "value_error",
                    None,
                    format!("Value error, {detail}"),
                    input,
                    Some(json!({"error":{}})),
                ));
            }
        }
    } else {
        let name = if *model == BodyModel::ScheduleCreate {
            "interval_seconds"
        } else {
            "expected_revision"
        };
        if let Some(value) = fields.get(name) {
            let (min, max) = if *model == BodyModel::ScheduleCreate {
                (3600, Some(30 * 24 * 3600))
            } else {
                (1, None)
            };
            int_field(&mut output, &mut errors, name, value, min, max);
        } else {
            errors.push(field_error("missing", name, "Field required", input));
        }
        if *model == BodyModel::ScheduleWrite
            && let Some(value) = fields.get("enabled")
        {
            if value.is_null() {
                output.insert("enabled".into(), Value::Null);
            } else {
                bool_field(&mut output, &mut errors, "enabled", value);
            }
        }
    }
    if errors.is_empty() {
        Ok(output.into())
    } else {
        Err(ApiError::validation(errors))
    }
}

fn bool_field(output: &mut Map<String, Value>, errors: &mut Vec<Value>, name: &str, value: &Value) {
    match boolean(value) {
        Ok(value) => {
            output.insert(name.into(), value.into());
        }
        Err((kind, detail)) => errors.push(field_error(kind, name, detail, value)),
    }
}

pub(super) fn int_field(
    output: &mut Map<String, Value>,
    errors: &mut Vec<Value>,
    name: &str,
    value: &Value,
    min: i64,
    max: Option<i64>,
) {
    match integer(value) {
        Ok(number) => {
            // Validated integers may exceed i64. Positive overflow exceeds any
            // bounded setting; unbounded revisions stay arbitrary precision.
            let spelling = number.to_string();
            let small = number.as_i64();
            let constraint = if spelling.starts_with('-') || small.is_some_and(|n| n < min) {
                Some(("greater_than_equal", "greater than or equal to", "ge", min))
            } else {
                max.filter(|max| small.is_none_or(|n| n > *max))
                    .map(|max| ("less_than_equal", "less than or equal to", "le", max))
            };
            if let Some((kind, phrase, key, bound)) = constraint {
                let mut context = Map::new();
                context.insert(key.into(), bound.into());
                errors.push(error(
                    kind,
                    Some(name),
                    format!("Input should be {phrase} {bound}"),
                    value,
                    Some(context.into()),
                ));
            } else {
                output.insert(name.into(), number);
            }
        }
        Err((kind, detail)) => errors.push(field_error(kind, name, detail, value)),
    }
}

fn list_field(
    output: &mut Map<String, Value>,
    errors: &mut Vec<Value>,
    name: &str,
    value: &Value,
    max: usize,
) {
    let Some(values) = value.as_array() else {
        errors.push(field_error(
            "list_type",
            name,
            "Input should be a valid list",
            value,
        ));
        return;
    };
    if values.len() > max {
        errors.push(error(
            "too_long",
            Some(name),
            format!(
                "List should have at most {max} items after validation, not {}",
                values.len()
            ),
            value,
            Some(json!({"field_type":"List", "max_length":max,"actual_length":values.len()})),
        ));
        return;
    }
    let mut normalized = Vec::with_capacity(values.len());
    for (index, value) in values.iter().enumerate() {
        if let Some(text) = value.as_str() {
            normalized.push(Value::from(text.trim()));
        } else {
            let mut error =
                field_error("string_type", name, "Input should be a valid string", value);
            error["loc"]
                .as_array_mut()
                .expect("field location")
                .push(index.into());
            errors.push(error);
        }
    }
    output.insert(name.into(), normalized.into());
}
