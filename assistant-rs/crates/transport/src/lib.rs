//! Initial Assistant HTTP routes. No listener or shipped entry point is enabled
//! by this library; a host must supply its trusted scheme and existing store.
mod auth;
pub use auth::Authentication;
use auth::{Principal, header};
use axum::{
    Json, Router,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, put},
};
use chrono::{DateTime, Utc};
use futures_util::StreamExt;
use nebula_assistant_services::{
    AssistantRecords, Error as ServiceError,
    context::{CursorWrite, DecisionWrite},
};
use nebula_assistant_storage::entities::{Error as StorageError, SqliteAssistantStore};
use serde_json::{Value, json};
use std::sync::Arc;
use tokio::sync::Semaphore;

const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;
#[derive(Clone)]
pub struct HttpConfig {
    pub authentication: Authentication,
    pub concurrent_requests: usize,
    pub response_budget_bytes: usize,
    pub body_bytes: usize,
    pub request_timeout: std::time::Duration,
    pub clock: fn() -> DateTime<Utc>,
}
impl HttpConfig {
    pub fn new(authentication: Authentication) -> Self {
        Self {
            authentication,
            concurrent_requests: 128,
            response_budget_bytes: 64 * 1024 * 1024,
            body_bytes: 1024 * 1024,
            request_timeout: std::time::Duration::from_secs(30),
            clock: Utc::now,
        }
    }
}
#[derive(Clone)]
struct AppState {
    store: SqliteAssistantStore,
    services: AssistantRecords,
    config: HttpConfig,
    requests: Arc<Semaphore>,
    response_bytes: Arc<Semaphore>,
}
#[derive(Clone)]
struct RequestId(String);

pub fn router(store: SqliteAssistantStore, config: HttpConfig) -> Result<Router, &'static str> {
    if !(1..=2048).contains(&config.concurrent_requests)
        || !(MAX_RESPONSE_BYTES..=1024 * 1024 * 1024).contains(&config.response_budget_bytes)
        || !(1..=16 * 1024 * 1024).contains(&config.body_bytes)
        || config.request_timeout.is_zero()
        || config.request_timeout > std::time::Duration::from_secs(120)
    {
        return Err("Invalid Assistant HTTP resource limits");
    }
    let state = AppState {
        services: AssistantRecords::new(store.clone()),
        store,
        requests: Arc::new(Semaphore::new(config.concurrent_requests)),
        response_bytes: Arc::new(Semaphore::new(config.response_budget_bytes)),
        config,
    };
    Ok(Router::new()
        .route(
            "/api/v1/chat/sessions/{session_id}/decisions",
            get(read_decisions),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/decisions/{decision_id}",
            put(write_decision),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/read-cursor",
            put(advance_cursor),
        )
        .route_layer(middleware::from_fn_with_state(state.clone(), boundary))
        .with_state(state))
}
async fn boundary(State(state): State<AppState>, mut request: Request, next: Next) -> Response {
    let id = format!("req_{}", uuid::Uuid::new_v4().simple());
    let operation = header(request.headers(), "x-nebula-operation-id");
    request.extensions_mut().insert(RequestId(id.clone()));
    let permit = match state.requests.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => return ApiError::capacity().response(&id, operation.as_deref()),
    };
    let budget = match state
        .response_bytes
        .clone()
        .try_acquire_many_owned(MAX_RESPONSE_BYTES as u32)
    {
        Ok(p) => p,
        Err(_) => return ApiError::capacity().response(&id, operation.as_deref()),
    };
    let deadline = state.config.request_timeout;
    let outcome = tokio::time::timeout(deadline, async {
        match state
            .config
            .authentication
            .authenticate(
                &state.store,
                request.headers(),
                request.method(),
                (state.config.clock)(),
            )
            .await
        {
            Ok(principal) => {
                request.extensions_mut().insert(principal);
                next.run(request).await
            }
            Err(error) => error.response(&id, operation.as_deref()),
        }
    })
    .await;
    let mut response = outcome.unwrap_or_else(|_| {
        ApiError::http(
            504,
            "Assistant request deadline exceeded; inspect durable state before retrying a mutation",
        )
        .response(&id, operation.as_deref())
    });
    response.headers_mut().insert(
        "x-request-id",
        HeaderValue::from_str(&id).expect("generated request identity"),
    );
    tracing::info!(request_id=%id,status=response.status().as_u16(),"Assistant request completed");
    let (parts, body) = response.into_parts();
    let stream = futures_util::stream::unfold(
        (body.into_data_stream(), Some((permit, budget))),
        |(mut body, permits)| async move { body.next().await.map(|chunk| (chunk, (body, permits))) },
    );
    Response::from_parts(parts, Body::from_stream(stream))
}

async fn read_decisions(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = state
        .services
        .decisions(&session)
        .await
        .map(|rows| Value::Array(rows.into_iter().map(|r| r.into_payload()).collect()));
    reply(request, result)
}
async fn write_decision(
    State(state): State<AppState>,
    Path((session, decision)): Path<(String, String)>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let result = match decode::<DecisionWrite>(body, state.config.body_bytes).await {
        Ok(value) => state
            .services
            .write_decision(&session, &decision, value)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn advance_cursor(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let device = parts
        .extensions
        .get::<Principal>()
        .and_then(|p| p.device_id.as_deref());
    let result = match decode::<CursorWrite>(body, state.config.body_bytes).await {
        Ok(value) => state
            .services
            .advance_cursor(&session, value, device)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn decode<T: serde::de::DeserializeOwned>(body: Body, limit: usize) -> Result<T, ApiError> {
    let bytes = to_bytes(body, limit)
        .await
        .map_err(|_| ApiError::http(413, "Assistant request body exceeds its configured limit"))?;
    serde_json::from_slice(&bytes).map_err(|_| {
        ApiError::http(
            422,
            "Assistant request does not match its expected JSON fields",
        )
    })
}
fn reply(request: Request, result: Result<Value, ServiceError>) -> Response {
    let (parts, _) = request.into_parts();
    api_reply(&parts, result.map_err(ApiError::service))
}
fn api_reply(parts: &axum::http::request::Parts, result: Result<Value, ApiError>) -> Response {
    let id = parts
        .extensions
        .get::<RequestId>()
        .map_or("", |r| r.0.as_str());
    let operation = header(&parts.headers, "x-nebula-operation-id");
    match result {
        Ok(value)=>match serde_json::to_vec(&value) {
            Ok(bytes) if bytes.len()<=MAX_RESPONSE_BYTES => ([("content-type","application/json")],bytes).into_response(),
            _=>ApiError::http(413,"Assistant response exceeds its configured limit; inspect retained records with pagination").response(id,operation.as_deref()),
        },
        Err(error)=>error.response(id,operation.as_deref()),
    }
}

pub(crate) struct ApiError {
    status: u16,
    detail: String,
    code: Option<&'static str>,
}
impl ApiError {
    fn http(status: u16, detail: impl Into<String>) -> Self {
        Self {
            status,
            detail: detail.into(),
            code: None,
        }
    }
    fn unauthorized() -> Self {
        Self::http(401, "valid bearer token required")
    }
    fn capacity() -> Self {
        Self::http(
            503,
            "Assistant request capacity is full; retry after capacity becomes available",
        )
    }
    fn storage(error: StorageError) -> Self {
        match error {
            StorageError::Capacity => Self::capacity(),
            StorageError::Conflict => Self {
                status: 409,
                detail: error.to_string(),
                code: Some("chat.conflict_error"),
            },
            StorageError::NotFound => Self {
                status: 404,
                detail: error.to_string(),
                code: Some("chat.not_found_error"),
            },
            StorageError::ReadLimit => Self::http(413, error.to_string()),
            _ => Self::http(
                503,
                "Assistant storage is unavailable; inspect durable state before retrying a mutation",
            ),
        }
    }
    fn service(error: ServiceError) -> Self {
        match error {
            ServiceError::Invalid(detail) => Self::http(422, detail),
            ServiceError::NotFound(detail) => Self::http(404, detail),
            ServiceError::Conflict(detail) => Self {
                status: 409,
                detail: detail.into(),
                code: Some("chat.conflict_error"),
            },
            ServiceError::Storage(error) => Self::storage(error),
            _ => Self::http(422, "Assistant record does not match its storage contract"),
        }
    }
    fn response(self, request_id: &str, operation_id: Option<&str>) -> Response {
        let (reason, impact) = match self.status {
            401 => (
                "authentication_failed",
                "Requests that require this credential cannot complete. The credential value was not added to diagnostics.",
            ),
            403 => (
                "permission_denied",
                "The denied operation was not performed.",
            ),
            400 | 413 | 422 => ("invalid_input", "The operation was not applied."),
            404 => ("not_found", "The requested resource is unavailable."),
            409 => ("state_conflict", "The operation was not applied."),
            _ => (
                "service_unavailable",
                "The request could not complete. Inspect saved state before retrying a mutation.",
            ),
        };
        let retryable = self.status >= 500;
        let mut value = json!({"detail":self.detail,"code":self.code.map(str::to_owned).unwrap_or_else(||format!("api.http_{}",self.status)),"feature":"chat","request_id":request_id,"error_id":format!("err_{}",uuid::Uuid::new_v4().simple()),"retryable":retryable,"help_article":"provider-model","reason_code":reason,"operator_detail":self.detail,"impact":impact,"remediation_id":format!("chat.{reason}"),"recovery_action":if retryable {"Retry this operation"} else {"Review recovery guidance"},"recovery_destination":"/settings#diagnostics-settings"});
        if let Some(operation) = operation_id.filter(|s| !s.is_empty()) {
            value["operation_id"] = operation.into();
        }
        let mut response = (
            StatusCode::from_u16(self.status).expect("static HTTP status"),
            Json(value),
        )
            .into_response();
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
