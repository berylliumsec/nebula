use axum::{
    body::Body,
    http::{HeaderValue, StatusCode},
    response::Response,
};
use nebula_assistant_services::Error as ServiceError;
use nebula_assistant_storage::entities::Error as StorageError;
use serde_json::{Value, json};
use std::sync::LazyLock;

// Shared product text is compiled into Rust. No Python process or runtime file
// lookup is required, and other feature implementations remain unchanged.
static GUIDANCE: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!(
        "../../../../src/nebula/v3/diagnostic_guidance.json"
    ))
    .expect("checked-in diagnostic guidance is JSON")
});
pub(crate) struct ApiError {
    status: u16,
    detail: Value,
    code: String,
    feature: &'static str,
    exception: String,
}
impl ApiError {
    pub(crate) fn http(status: u16, detail: impl Into<String>) -> Self {
        let detail = detail.into();
        Self {
            status,
            exception: format!("{status}: {detail}"),
            detail: detail.into(),
            code: format!("api.http_{status}"),
            feature: "chat",
        }
    }
    pub(crate) fn validation(errors: Vec<Value>) -> Self {
        let mut exception = format!(
            "{} validation error{}:\n",
            errors.len(),
            if errors.len() == 1 { "" } else { "s" }
        );
        for error in &errors {
            append(&mut exception, "  {");
            for (i, key) in ["type", "loc", "msg", "input", "ctx"]
                .iter()
                .filter(|key| error.get(**key).is_some())
                .enumerate()
            {
                if i != 0 {
                    append(&mut exception, ", ");
                }
                repr(&mut exception, &json!(key));
                append(&mut exception, ": ");
                if *key == "loc" {
                    append(&mut exception, "(");
                    if let Some(items) = error[*key].as_array() {
                        for (i, item) in items.iter().enumerate() {
                            if i != 0 {
                                append(&mut exception, ", ");
                            }
                            repr(&mut exception, item);
                        }
                        if items.len() == 1 {
                            append(&mut exception, ",");
                        }
                    }
                    append(&mut exception, ")");
                } else {
                    repr(&mut exception, &error[*key]);
                }
            }
            append(&mut exception, "}\n");
            if exception.chars().count() >= 300 {
                break;
            }
        }
        Self {
            status: 422,
            detail: errors.into(),
            code: "api.request_validation".into(),
            feature: "chat",
            exception,
        }
    }
    pub(crate) fn unauthorized() -> Self {
        Self::http(401, "valid bearer token required")
    }
    pub(crate) fn capacity() -> Self {
        Self::http(
            503,
            "Assistant request capacity is full; retry after capacity becomes available",
        )
    }
    fn named(status: u16, detail: String, code: &str, feature: &'static str) -> Self {
        Self {
            status,
            exception: detail.clone(),
            detail: detail.into(),
            code: code.into(),
            feature,
        }
    }
    pub(crate) fn storage(error: StorageError) -> Self {
        match error {
            StorageError::Capacity => Self::capacity(),
            StorageError::AlreadyExists(_) => {
                Self::named(409, error.to_string(), "storage.conflict_error", "storage")
            }
            StorageError::Conflict | StorageError::RevisionConflict { .. } => {
                Self::named(409, error.to_string(), "chat.conflict_error", "chat")
            }
            StorageError::NotFound => {
                Self::named(404, error.to_string(), "chat.not_found_error", "chat")
            }
            StorageError::ReadLimit => Self::http(413, error.to_string()),
            _ => Self::http(
                503,
                "Assistant storage is unavailable; inspect durable state before retrying a mutation",
            ),
        }
    }
    pub(crate) fn service(error: ServiceError) -> Self {
        match error {
            ServiceError::Invalid(detail) => Self::http(422, detail),
            ServiceError::NotFound(detail) => Self::http(404, detail),
            ServiceError::EntityNotFound { .. } => {
                Self::named(404, error.to_string(), "chat.not_found_error", "chat")
            }
            ServiceError::Conflict(_) | ServiceError::RevisionConflict { .. } => {
                Self::named(409, error.to_string(), "chat.conflict_error", "chat")
            }
            ServiceError::Storage(error) => Self::storage(error),
            _ => Self::http(422, "Assistant record does not match its storage contract"),
        }
    }
    pub(crate) fn response(self, request_id: &str, operation_id: Option<&str>) -> Response {
        let reason = reason(self.status, &self.code, &self.exception);
        let guidance = &GUIDANCE["reason_families"][reason];
        let operator = self
            .detail
            .as_str()
            .filter(|s| !s.is_empty())
            .map(Value::from)
            .unwrap_or_else(|| guidance["cause"].clone());
        let retryable = self.status >= 500;
        let mut value = json!({"detail":self.detail,"code":self.code,"feature":self.feature,"request_id":request_id,"error_id":format!("err_{}",uuid::Uuid::new_v4().simple()),"retryable":retryable,"help_article":GUIDANCE["features"][self.feature]["help_article"],"reason_code":reason,"operator_detail":operator,"impact":guidance["impact"],"remediation_id":format!("{}.{reason}",self.feature),"recovery_action":if retryable {"Retry this operation"} else {"Review recovery guidance"},"recovery_destination":"/settings#diagnostics-settings"});
        if let Some(operation) = operation_id.filter(|s| !s.is_empty()) {
            value["operation_id"] = operation.into();
        }
        let bytes=match crate::json_bytes(&value) { Ok(bytes)=>bytes,Err(_)=>return Self::http(413,"Assistant validation response exceeds its configured limit; send a smaller request").response(request_id,None) };
        let mut response = Response::builder()
            .status(StatusCode::from_u16(self.status).expect("static HTTP status"))
            .header("content-type", "application/json")
            .body(Body::from(bytes))
            .expect("static response headers");
        if self.status == 401 {
            response
                .headers_mut()
                .insert("www-authenticate", HeaderValue::from_static("Bearer"));
        }
        if self.status == 503 {
            response
                .headers_mut()
                .insert("retry-after", HeaderValue::from_static("1"));
        }
        if let Ok(id) = HeaderValue::from_str(request_id) {
            response.headers_mut().insert("x-request-id", id);
        }
        response
    }
}
fn reason(status: u16, code: &str, exception: &str) -> &'static str {
    let text = format!("{code} {}", exception.chars().take(300).collect::<String>()).to_lowercase();
    let has = |words: &[&str]| words.iter().any(|w| text.contains(w));
    if status == 429 || text.contains("rate") && text.contains("limit") {
        "rate_limited"
    } else if status == 401
        || has(&[
            "authentication",
            "credential",
            "unauthorized",
            "login",
            "not signed in",
        ])
    {
        "authentication_failed"
    } else if status == 403 || has(&["permission", "denied", "privacy", "policy"]) {
        "permission_denied"
    } else if matches!(status, 408 | 504) || has(&["timeout", "timedout"]) {
        "timeout"
    } else if has(&["integrity", "digest", "signature", "checksum"]) {
        "integrity_failed"
    } else if has(&["conflict", "stale", "stateerror", "state_error", "revision"]) {
        "stale_state"
    } else if has(&["transport", "disconnect", "closed", "endofstream"]) {
        "transport_closed"
    } else if text.contains("connecterror") {
        "dependency_unavailable"
    } else if has(&["protocol", "malformed", "decode", "parse"]) {
        "protocol_invalid"
    } else if matches!(status, 502 | 503) || has(&["unavailable", "notavailable", "not_available"])
    {
        "dependency_unavailable"
    } else if has(&["invalid", "validation", "unsupported", "configuration"]) {
        "invalid_input"
    } else if has(&["cancelled", "canceled", "interrupted"]) {
        "cancelled"
    } else {
        "unknown_internal_fault"
    }
}
fn append(out: &mut String, text: &str) {
    let left = 300usize.saturating_sub(out.chars().count());
    out.extend(text.chars().take(left));
}
fn repr(out: &mut String, value: &Value) {
    if out.chars().count() >= 300 {
        return;
    }
    match value {
        Value::Null => append(out, "None"),
        Value::Bool(true) => append(out, "True"),
        Value::Bool(false) => append(out, "False"),
        Value::Number(n) => append(out, &n.to_string()),
        Value::String(s) => {
            let quote = if s.contains('\'') && !s.contains('"') {
                '"'
            } else {
                '\''
            };
            append(out, &quote.to_string());
            for c in s.chars() {
                if out.chars().count() >= 300 {
                    break;
                }
                match c {
                    '\n' => append(out, "\\n"),
                    '\r' => append(out, "\\r"),
                    '\t' => append(out, "\\t"),
                    '\\' => append(out, "\\\\"),
                    c if c == quote => append(out, &format!("\\{c}")),
                    c if c.is_control() => append(out, &format!("\\x{:02x}", c as u32)),
                    c => append(out, &c.to_string()),
                }
            }
            append(out, &quote.to_string());
        }
        Value::Array(items) => {
            append(out, "[");
            for (i, v) in items.iter().enumerate() {
                if i != 0 {
                    append(out, ", ");
                }
                repr(out, v);
                if out.chars().count() >= 300 {
                    break;
                }
            }
            append(out, "]");
        }
        Value::Object(items) => {
            append(out, "{");
            for (i, (k, v)) in items.iter().enumerate() {
                if i != 0 {
                    append(out, ", ");
                }
                repr(out, &json!(k));
                append(out, ": ");
                repr(out, v);
                if out.chars().count() >= 300 {
                    break;
                }
            }
            append(out, "}");
        }
    }
}
