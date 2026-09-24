//! Plain BaseModel goal configuration requests. Strings are stripped only by
//! the later persisted Goal model, after source lookup and identity allocation.
use super::{ApiError, BodyModel, error, field_error, settings::int_field};
use serde_json::{Map, Value, json};
use std::collections::HashSet;

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
    if *model == BodyModel::GoalConversationCreate {
        conversation_fields(&mut output, &mut errors, input, fields);
    }
    if errors.is_empty() {
        Ok(output.into())
    } else {
        Err(ApiError::validation(errors))
    }
}

fn conversation_fields(
    output: &mut Map<String, Value>,
    errors: &mut Vec<Value>,
    input: &Value,
    fields: &Map<String, Value>,
) {
    for (name, maximum) in [("engagement_id", 200), ("provider_id", 200), ("model", 500)] {
        if let Some(value) = fields.get(name) {
            super::string(
                output,
                errors,
                name,
                value,
                false,
                Some(1),
                Some(maximum),
                None,
            );
        } else {
            errors.push(field_error("missing", name, "Field required", input));
        }
    }
    bool_default(output, errors, fields, "tools_enabled");
    for (name, maximum) in [("mcp_server_ids", 64), ("hook_ids", 32)] {
        if let Some(value) = fields.get(name) {
            strings(output, errors, name, value, 0, maximum);
        } else {
            output.insert(name.into(), json!([]));
        }
    }
    if let Some(value) = fields
        .get("reasoning_effort")
        .filter(|value| !value.is_null())
    {
        let expected = "'none', 'minimal', 'low', 'medium', 'high' or 'xhigh'";
        if value.as_str().is_some_and(|value| {
            ["none", "minimal", "low", "medium", "high", "xhigh"].contains(&value)
        }) {
            output.insert("reasoning_effort".into(), value.clone());
        } else {
            errors.push(error(
                "literal_error",
                Some("reasoning_effort"),
                format!("Input should be {expected}"),
                value,
                Some(json!({"expected":expected})),
            ));
        }
    } else {
        output.insert("reasoning_effort".into(), Value::Null);
    }
    bool_default(output, errors, fields, "allow_subagents");
    bool_default(output, errors, fields, "allow_agent_messaging");
    if let Some(value) = fields
        .get("max_active_subagents")
        .filter(|value| !value.is_null())
    {
        int_field(output, errors, "max_active_subagents", value, 1, Some(100));
    } else {
        output.insert("max_active_subagents".into(), Value::Null);
    }
    // The plain BaseModel's after-validator compares exact request strings,
    // only after every field succeeds. Entity models trim later.
    if errors.is_empty() {
        for (name, detail) in [
            ("mcp_server_ids", "MCP server selection contains duplicates"),
            ("hook_ids", "hook selection contains duplicates"),
        ] {
            let mut unique = HashSet::new();
            if output[name]
                .as_array()
                .expect("validated selection list")
                .iter()
                .any(|value| !unique.insert(value.as_str().expect("validated selection string")))
            {
                errors.push(error(
                    "value_error",
                    None,
                    format!("Value error, {detail}"),
                    input,
                    Some(json!({"error":{}})),
                ));
                break;
            }
        }
    }
}

fn bool_default(
    output: &mut Map<String, Value>,
    errors: &mut Vec<Value>,
    fields: &Map<String, Value>,
    name: &str,
) {
    if let Some(value) = fields.get(name) {
        match super::boolean(value) {
            Ok(value) => {
                output.insert(name.into(), value.into());
            }
            Err((kind, message)) => errors.push(field_error(kind, name, message, value)),
        }
    } else {
        output.insert(name.into(), false.into());
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
