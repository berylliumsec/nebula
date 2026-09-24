//! ScopePolicy/MissionGrant retained coercions and ordered diagnostics.
use super::*;
use crate::{dependencies::DependencyEnvironment, scope_policy as normalize};

const fn field(name: &'static str, kind: FieldType, nullable: bool) -> Field {
    Field {
        name,
        kind,
        nullable,
    }
}
const fn strings(name: &'static str, max: usize) -> Field {
    field(name, FieldType::Strings { min: 0, max }, false)
}
pub(super) const FIELDS: [Field; 21] = [
    super::FIELDS[0],
    super::FIELDS[1],
    super::FIELDS[2],
    super::FIELDS[3],
    text("engagement_id", 0, None, false),
    strings("allowed_cidrs", usize::MAX),
    strings("allowed_domains", usize::MAX),
    strings("allowed_urls", usize::MAX),
    strings("allowed_ports", usize::MAX),
    field("allow_all_targets", FieldType::Boolean, false),
    field("not_before", FieldType::OptionalTime, true),
    field("not_after", FieldType::OptionalTime, true),
    strings("prohibited_actions", usize::MAX),
    field("local_only", FieldType::Boolean, false),
    field("tool_suggestions", FieldType::Boolean, false),
    field("on_demand_tools", FieldType::Boolean, false),
    field("web_search", FieldType::Boolean, false),
    field("web_search_discloses_scope", FieldType::Boolean, false),
    strings("always_loaded_tools", 500),
    field(
        "max_concurrency",
        FieldType::Integer {
            min: 1,
            max: Some(256),
        },
        false,
    ),
    strings("grants", usize::MAX),
];

pub(super) const GRANT_FIELDS: [Field; 6] = [
    strings("risk_classes", usize::MAX),
    strings("tool_names", usize::MAX),
    strings("targets", usize::MAX),
    field("granted_at", FieldType::OptionalTime, false),
    field("expires_at", FieldType::OptionalTime, false),
    text("granted_by", 1, None, false),
];
const RISKS: &[&str] = &[
    "local_read",
    "passive",
    "active_scan",
    "workspace_write",
    "credential_use",
    "exploitation",
    "persistence",
    "destructive",
    "scope_change",
];
pub(super) fn default(model: Model, field: Field) -> Option<Value> {
    match (model, field.name) {
        (
            Model::ScopePolicy,
            "allowed_cidrs"
            | "allowed_domains"
            | "allowed_urls"
            | "allowed_ports"
            | "prohibited_actions"
            | "always_loaded_tools"
            | "grants",
        )
        | (Model::MissionGrant, "tool_names" | "targets") => Some(json!([])),
        (
            Model::ScopePolicy,
            "allow_all_targets"
            | "local_only"
            | "tool_suggestions"
            | "web_search"
            | "web_search_discloses_scope",
        ) => Some(false.into()),
        (Model::ScopePolicy, "on_demand_tools") => Some(true.into()),
        (Model::ScopePolicy, "max_concurrency") => Some(1.into()),
        _ => None,
    }
}
fn fail(
    report: &mut ValidationReport,
    path: Vec<Location>,
    failure: Failure,
) -> Result<Option<Value>> {
    report.add_at(failure.kind, path.clone(), path, failure.msg, failure.ctx)?;
    Ok(None)
}
fn value_error(message: String) -> Failure {
    Failure::context(
        "value_error",
        format!("Value error, {message}"),
        json!({"error":{}}),
    )
}
fn aware(value: &Value, message: &'static str) -> FieldResult {
    let parsed = datetime(value)?;
    let time = DateTime::parse_from_rfc3339(&parsed)
        .map_err(|_| Failure::value(message))?
        .with_timezone(&Utc);
    Ok(time
        .to_rfc3339_opts(
            if time.timestamp_subsec_micros() == 0 {
                SecondsFormat::Secs
            } else {
                SecondsFormat::Micros
            },
            true,
        )
        .into())
}
pub(super) fn clock(environment: &mut Option<&mut dyn DependencyEnvironment>) -> Result<Value> {
    let time = environment
        .as_deref_mut()
        .ok_or(RecordError::Shape("scope_policies"))?
        .now();
    Ok(time
        .to_rfc3339_opts(
            if time.timestamp_subsec_micros() == 0 {
                SecondsFormat::Secs
            } else {
                SecondsFormat::Micros
            },
            true,
        )
        .into())
}
fn grant(
    value: &Value,
    path: Vec<Location>,
    report: &mut ValidationReport,
    environment: &mut Option<&mut dyn DependencyEnvironment>,
) -> Result<Option<Value>> {
    let Some(object) = value.as_object() else {
        return fail(
            report,
            path,
            Failure::context(
                "model_type",
                "Input should be a valid dictionary or instance of MissionGrant",
                json!({"class_name":"MissionGrant"}),
            ),
        );
    };
    let before = report.len();
    let mut output = Map::new();
    for field in GRANT_FIELDS {
        let mut child = path.clone();
        child.push(Location::Field(field.name.into()));
        if let Some(value) = object.get(field.name) {
            if let Some(value) = validate(
                Model::MissionGrant,
                field,
                value,
                child,
                report,
                environment,
            )? {
                output.insert(field.name.into(), value);
            }
        } else if field.name == "granted_at" {
            output.insert(field.name.into(), clock(environment)?);
        } else if let Some(value) = default(Model::MissionGrant, field) {
            output.insert(field.name.into(), value);
        } else {
            report.add_at(
                "missing",
                child,
                path.clone(),
                "Field required".into(),
                None,
            )?;
        }
    }
    let keys: Vec<_> = report
        .input_order_at(&path)
        .map_or_else(|| object.keys().cloned().collect(), |keys| keys.to_vec());
    for key in keys {
        if !GRANT_FIELDS.iter().any(|f| f.name == key) {
            let mut child = path.clone();
            child.push(Location::Field(key));
            fail(
                report,
                child,
                Failure::simple("extra_forbidden", "Extra inputs are not permitted"),
            )?;
        }
    }
    if before != report.len() {
        return Ok(None);
    }
    if let Some(message) = coherence(Model::MissionGrant, &output) {
        return fail(report, path, Failure::value(message));
    }
    Ok(Some(output.into()))
}
pub(super) fn validate(
    model: Model,
    field: Field,
    value: &Value,
    path: Vec<Location>,
    report: &mut ValidationReport,
    environment: &mut Option<&mut dyn DependencyEnvironment>,
) -> Result<Option<Value>> {
    if field.nullable && value.is_null() {
        return Ok(Some(Value::Null));
    }
    if matches!(
        (model, field.name),
        (Model::ScopePolicy, "not_before" | "not_after")
            | (Model::MissionGrant, "granted_at" | "expires_at")
    ) {
        return match aware(
            value,
            if model == Model::ScopePolicy {
                "scope timestamps must include a timezone"
            } else {
                "grant timestamps must include a timezone"
            },
        ) {
            Ok(v) => Ok(Some(v)),
            Err(error) => fail(report, path, error),
        };
    }
    if matches!(
        (model, field.name),
        (Model::ScopePolicy, "allowed_ports" | "grants") | (Model::MissionGrant, "risk_classes")
    ) {
        let Some(values) = value.as_array() else {
            return fail(
                report,
                path,
                Failure::simple("list_type", "Input should be a valid list"),
            );
        };
        if values.len() > MAX_ISSUES {
            return Err(RecordError::TooLarge);
        }
        let before = report.len();
        let mut output = Vec::with_capacity(values.len());
        for (index, item) in values.iter().enumerate() {
            let mut child = path.clone();
            child.push(Location::Index(index));
            if field.name == "grants" {
                if let Some(value) = grant(item, child, report, environment)? {
                    output.push(value);
                }
                continue;
            }
            let result = if field.name == "allowed_ports" {
                integer(item)
            } else {
                validate_field(field_with_enum(), item)
            };
            match result {
                Ok(v) => output.push(v),
                Err(e) => {
                    fail(report, child, e)?;
                }
            }
        }
        if before != report.len() {
            return Ok(None);
        }
        if field.name == "allowed_ports" {
            if output.iter().any(|v| v.as_u64().is_none_or(|v| v > 65535)) {
                return fail(
                    report,
                    path,
                    Failure::value("ports must be between 0 and 65535"),
                );
            }
            output.sort_by_key(|v| v.as_u64());
            output.dedup();
        }
        return Ok(Some(output.into()));
    }
    let Some(hydrated) = goal::validate(field, value, path.clone(), report)? else {
        return Ok(None);
    };
    if model != Model::ScopePolicy
        || ![
            "allowed_cidrs",
            "allowed_domains",
            "allowed_urls",
            "always_loaded_tools",
        ]
        .contains(&field.name)
    {
        return Ok(Some(hydrated));
    }
    let mut output = Vec::new();
    for value in hydrated.as_array().ok_or(RecordError::Schema)? {
        let value = value.as_str().ok_or(RecordError::Schema)?;
        // Bound normalization scratch even for intentionally malformed saved
        // input. This is an explicit capacity error, never a privacy fallback.
        if value.len() > 64 * 1024 {
            return Err(RecordError::TooLarge);
        }
        let result = match field.name {
            "allowed_cidrs" => normalize::cidr(value),
            "allowed_domains" => normalize::domain(value),
            "allowed_urls" => normalize::url(value),
            "always_loaded_tools" => {
                let name = normalize::python_trim(value);
                if name.is_empty() {
                    continue;
                }
                if name.chars().count() > 300 {
                    Err(format!(
                        "tool name is too long: {}",
                        name.chars().take(80).collect::<String>()
                    ))
                } else {
                    Ok(name.into())
                }
            }
            _ => unreachable!(),
        };
        match result {
            Ok(v) => output.push(v),
            Err(e) => return fail(report, path, value_error(e)),
        }
    }
    Ok(Some(normalize::sorted_strings(output.into_iter()).into()))
}
fn field_with_enum() -> Field {
    field("risk_classes", FieldType::Enum(RISKS), false)
}
pub(super) fn coherence(model: Model, output: &Map<String, Value>) -> Option<&'static str> {
    let (start, end, message) = match model {
        Model::ScopePolicy => (
            "not_before",
            "not_after",
            "not_after must be later than not_before",
        ),
        Model::MissionGrant => (
            "granted_at",
            "expires_at",
            "expires_at must be later than granted_at",
        ),
        _ => return None,
    };
    let (Some(start), Some(end)) = (output[start].as_str(), output[end].as_str()) else {
        return None;
    };
    let start = DateTime::parse_from_rfc3339(start).ok()?;
    let end = DateTime::parse_from_rfc3339(end).ok()?;
    (end <= start).then_some(message)
}
