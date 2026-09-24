//! Plain BaseModel goal configuration requests. Strings are stripped only by
//! the later persisted Goal model, after source lookup and identity allocation.
use super::{ApiError, BodyModel, error, field_error, settings::int_field};
use serde_json::{Map, Value, json};

pub(super) fn validate(
    model: &BodyModel,
    input: &Value,
    fields: &Map<String, Value>,
) -> Result<Value, ApiError> {
    let mut output = Map::new();
    let mut errors = Vec::new();
    if let Some(value) = fields.get("objective") {
        super::string(
            &mut output,
            &mut errors,
            "objective",
            value,
            false,
            Some(1),
            Some(20_000),
            None,
        );
    } else {
        errors.push(field_error("missing", "objective", "Field required", input));
    }
    for (name, minimum, maximum, required) in [
        ("completion_criteria", 1, 50, true),
        ("plan", 0, 200, false),
    ] {
        if let Some(value) = fields.get(name) {
            strings(&mut output, &mut errors, name, value, minimum, maximum);
        } else if required {
            errors.push(field_error("missing", name, "Field required", input));
        } else {
            output.insert(name.into(), json!([]));
        }
    }
    for name in [
        "token_budget",
        "time_budget_seconds",
        "step_budget",
        "child_budget",
    ] {
        if let Some(value) = fields.get(name).filter(|value| !value.is_null()) {
            let child = name == "child_budget";
            int_field(
                &mut output,
                &mut errors,
                name,
                value,
                if child { 0 } else { 1 },
                child.then_some(32),
            );
        } else {
            output.insert(name.into(), Value::Null);
        }
    }
    if *model == BodyModel::GoalUpdate {
        if let Some(value) = fields.get("expected_revision") {
            int_field(
                &mut output,
                &mut errors,
                "expected_revision",
                value,
                1,
                None,
            );
        } else {
            errors.push(field_error(
                "missing",
                "expected_revision",
                "Field required",
                input,
            ));
        }
    }
    if errors.is_empty() {
        Ok(output.into())
    } else {
        Err(ApiError::validation(errors))
    }
}

fn strings(
    output: &mut Map<String, Value>,
    errors: &mut Vec<Value>,
    name: &str,
    input: &Value,
    minimum: usize,
    maximum: usize,
) {
    let Some(values) = input.as_array() else {
        errors.push(field_error(
            "list_type",
            name,
            "Input should be a valid list",
            input,
        ));
        return;
    };
    if values.len() > maximum {
        errors.push(error(
            "too_long",
            Some(name),
            format!(
                "List should have at most {maximum} items after validation, not {}",
                values.len()
            ),
            input,
            Some(json!({"field_type":"List","max_length":maximum,"actual_length":values.len()})),
        ));
        return;
    }
    let before = errors.len();
    for (index, value) in values.iter().enumerate() {
        if !value.is_string() {
            let mut issue =
                field_error("string_type", name, "Input should be a valid string", value);
            issue["loc"]
                .as_array_mut()
                .expect("field location")
                .push(index.into());
            errors.push(issue);
        }
    }
    if errors.len() == before && values.len() < minimum {
        errors.push(error(
            "too_short",
            Some(name),
            format!(
                "List should have at least {minimum} item{} after validation, not {}",
                if minimum == 1 { "" } else { "s" },
                values.len()
            ),
            input,
            Some(json!({"field_type":"List","min_length":minimum,"actual_length":values.len()})),
        ));
    }
    output.insert(name.into(), input.clone());
}
