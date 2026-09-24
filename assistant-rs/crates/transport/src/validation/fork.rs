//! The fork request is a NebulaModel: nullable strings are trimmed, extras fail.
//! Boundary exclusivity is a route/service guard after complete field validation.
use super::{ApiError, field_error};
use serde_json::{Map, Value};

const FIELDS: [&str; 3] = ["through_message_id", "before_message_id", "title"];

pub(super) fn validate(
    fields: &Map<String, Value>,
    key_order: &[String],
) -> Result<Value, ApiError> {
    let mut output = Map::new();
    let mut errors = Vec::new();
    for name in FIELDS {
        let Some(value) = fields.get(name).filter(|value| !value.is_null()) else {
            output.insert(name.into(), Value::Null);
            continue;
        };
        let normalized = value.as_str().map(|text| Value::from(text.trim()));
        let before = errors.len();
        super::string(
            &mut output,
            &mut errors,
            name,
            normalized.as_ref().unwrap_or(value),
            false,
            Some(1),
            Some(if name == "title" { 300 } else { 200 }),
            None,
        );
        for error in &mut errors[before..] {
            error["input"] = value.clone();
        }
    }
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
        Ok(output.into())
    } else {
        Err(ApiError::validation(errors))
    }
}
