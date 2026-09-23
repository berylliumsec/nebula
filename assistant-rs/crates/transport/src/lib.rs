//! Initial Assistant HTTP routes. No listener or shipped entry point is enabled
//! by this library; a host must supply its trusted scheme and existing store.
mod auth;
mod errors;
mod validation;
pub use auth::Authentication;
use auth::{Principal, header};
use axum::{
    Router,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::HeaderValue,
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, put},
};
use chrono::{DateTime, Utc};
use errors::ApiError;
use futures_util::StreamExt;
use nebula_assistant_services::{
    AssistantRecords, Error as ServiceError,
    artifact_preview::ArtifactPreview,
    context::{CursorWrite, DecisionWrite},
    generated::CatalogKind,
    navigation::BookmarkWrite,
};
use nebula_assistant_storage::entities::SqliteAssistantStore;
use serde_json::Value;
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
    pub artifacts: Option<ArtifactPreview>,
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
            artifacts: None,
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
        .route("/api/v1/chat-sessions", get(catalog_sessions))
        .route("/api/v1/chat-sessions/{entity_id}", get(catalog_session))
        .route("/api/v1/chat-messages", get(catalog_messages))
        .route("/api/v1/chat-messages/{entity_id}", get(catalog_message))
        .route("/api/v1/chat/sessions/{session_id}/catch-up", get(catch_up))
        .route(
            "/api/v1/chat/sessions/{session_id}/results",
            get(read_results),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/context-sources",
            get(read_context_sources),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/turns/{turn_id}/summary",
            get(turn_summary),
        )
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
        .route(
            "/api/v1/chat/projects/{project_id}/search",
            get(search_messages),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/messages",
            get(read_messages),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/bookmarks",
            get(read_bookmarks),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/bookmarks/{message_id}",
            put(write_bookmark),
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
            .advance_cursor_at(&session, value, device, (state.config.clock)())
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn search_messages(
    State(state): State<AppState>,
    Path(project): Path<String>,
    request: Request,
) -> Response {
    let (parts, _) = request.into_parts();
    let result = match validation::search(parts.uri.query()) {
        Ok(query) => state
            .services
            .search_messages(&project, query)
            .await
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn read_messages(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, _) = request.into_parts();
    let result = match validation::include_replaced(parts.uri.query()) {
        Ok(include) => state
            .services
            .session_messages(&session, include)
            .await
            .map(|rows| Value::Array(rows.into_iter().map(|r| r.into_payload()).collect()))
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn read_bookmarks(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = state
        .services
        .bookmarks(&session)
        .await
        .map(|rows| Value::Array(rows.into_iter().map(|r| r.into_payload()).collect()));
    reply(request, result)
}
async fn write_bookmark(
    State(state): State<AppState>,
    Path((session, message)): Path<(String, String)>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let result = match decode::<BookmarkWrite>(body, state.config.body_bytes).await {
        Ok(value) => state
            .services
            .set_bookmark(&session, &message, value)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn catalog_sessions(State(state): State<AppState>, request: Request) -> Response {
    catalog(state, CatalogKind::Sessions, request).await
}
async fn catalog_messages(State(state): State<AppState>, request: Request) -> Response {
    catalog(state, CatalogKind::Messages, request).await
}
async fn catalog(state: AppState, kind: CatalogKind, request: Request) -> Response {
    let (parts, _) = request.into_parts();
    let result = match validation::catalog(parts.uri.query()) {
        Ok(query) => state
            .services
            .catalog(kind, query)
            .await
            .map(|rows| Value::Array(rows.into_iter().map(|r| r.into_payload()).collect()))
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn catalog_session(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    let result = state
        .services
        .catalog_record(CatalogKind::Sessions, &id)
        .await
        .map(|r| r.into_payload());
    reply(request, result)
}
async fn catalog_message(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    let result = state
        .services
        .catalog_record(CatalogKind::Messages, &id)
        .await
        .map(|r| r.into_payload());
    reply(request, result)
}
async fn catch_up(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, _) = request.into_parts();
    let device = parts
        .extensions
        .get::<Principal>()
        .and_then(|p| p.device_id.as_deref());
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match validation::catchup_device(parts.uri.query()) {
        Ok(supplied) => records
            .catch_up(&session, &supplied, device)
            .await
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn turn_summary(
    State(state): State<AppState>,
    Path((session, turn)): Path<(String, String)>,
    request: Request,
) -> Response {
    let result = state.services.turn_summary(&session, &turn).await;
    reply(request, result)
}
async fn read_results(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, _) = request.into_parts();
    let result = match validation::results(parts.uri.query()) {
        Ok(query) => state
            .services
            .results(&session, query, state.config.artifacts.as_ref())
            .await
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn read_context_sources(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, _) = request.into_parts();
    let result = match validation::context_offset(parts.uri.query()) {
        Ok(offset) => state
            .services
            .context_sources(&session, offset)
            .await
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn decode<T: validation::RequestModel>(body: Body, limit: usize) -> Result<T, ApiError> {
    let bytes = to_bytes(body, limit)
        .await
        .map_err(|_| ApiError::http(413, "Assistant request body exceeds its configured limit"))?;
    let value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).map_err(|_| {
            ApiError::http(
                422,
                "Assistant request does not match its expected JSON fields",
            )
        })?
    };
    validation::validate(value)
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
        Ok(value)=>match json_bytes(&value) {
            Ok(bytes) if bytes.len()<=MAX_RESPONSE_BYTES => ([("content-type","application/json")],bytes).into_response(),
            _=>ApiError::http(413,"Assistant response exceeds its configured limit; inspect retained records with pagination").response(id,operation.as_deref()),
        },
        Err(error)=>error.response(id,operation.as_deref()),
    }
}

/// Bound serialization while writing, not after allocating an oversized body.
fn json_bytes(value: &Value) -> Result<Vec<u8>, serde_json::Error> {
    struct Bounded(Vec<u8>);
    impl std::io::Write for Bounded {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if bytes.len() > MAX_RESPONSE_BYTES.saturating_sub(self.0.len()) {
                return Err(std::io::Error::other(
                    "Assistant response byte limit exceeded",
                ));
            }
            self.0.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut writer = Bounded(Vec::new());
    serde_json::to_writer(&mut writer, value)?;
    Ok(writer.0)
}
