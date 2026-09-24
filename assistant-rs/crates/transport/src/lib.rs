//! Initial Assistant HTTP routes. No listener or shipped entry point is enabled
//! by this library; a host must supply its trusted scheme and existing store.
mod auth;
#[cfg(test)]
mod completion_tests;
mod errors;
mod validation;
pub use auth::Authentication;
use auth::{Principal, header};
use axum::{
    Router,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    http::{HeaderValue, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post, put},
};
use chrono::{DateTime, Utc};
use errors::ApiError;
use futures_util::StreamExt;
use nebula_assistant_services::{
    AssistantRecords, Error as ServiceError,
    artifact_preview::ArtifactPreview,
    context::{CursorWrite, DecisionWrite},
    fork::ForkRequest,
    generated::CatalogKind,
    goal_conversations::GoalConversationCreate,
    goal_drafts::{GoalDraft, GoalDraftUpdate},
    navigation::BookmarkWrite,
    settings::{ScheduleCreate, ScheduleWrite, SettingsWrite},
};
use nebula_assistant_storage::entities::{
    ConnectionObserver, ConversationDependencies, SqliteAssistantStore, StateObservations,
};
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
    /// Trusted host identity source; never accepted from request fields.
    pub new_schedule_id: fn() -> String,
    pub new_goal_id: fn() -> String,
    pub new_fork_id: fn() -> String,
    /// Trusted host home expansion and bounded dependency hydration.
    pub conversation_dependencies: ConversationDependencies,
    pub artifacts: Option<ArtifactPreview>,
    /// Trusted, synchronous observation of retained harness transport liveness.
    /// Must never start a transport or wait for network/process activity.
    pub harness_connection: Option<Arc<ConnectionObserver>>,
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
            new_schedule_id: || uuid::Uuid::new_v4().to_string(),
            new_goal_id: || uuid::Uuid::new_v4().to_string(),
            new_fork_id: || uuid::Uuid::new_v4().to_string(),
            conversation_dependencies: ConversationDependencies::default(),
            artifacts: None,
            harness_connection: None,
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
        .route(
            "/api/v1/chat-sessions/{entity_id}",
            get(catalog_session).patch(update_session_settings),
        )
        .route("/api/v1/chat-messages", get(catalog_messages))
        .route("/api/v1/chat-messages/{entity_id}", get(catalog_message))
        .route("/api/v1/chat-goals", get(other_catalog))
        .route("/api/v1/chat-goals/{entity_id}", get(other_catalog_record))
        .route("/api/v1/chat-goal-usage-charges", get(other_catalog))
        .route(
            "/api/v1/chat-goal-usage-charges/{entity_id}",
            get(other_catalog_record),
        )
        .route("/api/v1/chat-schedules", get(other_catalog))
        .route(
            "/api/v1/chat-schedules/{entity_id}",
            get(other_catalog_record),
        )
        .route("/api/v1/chat-subagents", get(other_catalog))
        .route(
            "/api/v1/chat-subagents/{entity_id}",
            get(other_catalog_record),
        )
        .route(
            "/api/v1/chat/goal-conversations",
            post(create_goal_conversation),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/fork",
            post(fork_conversation),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/goal",
            get(session_goal).post(create_goal).patch(update_goal),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/goal/children",
            get(goal_children),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/schedule",
            get(session_schedule).post(create_schedule),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/schedule/actions",
            post(write_schedule),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/subagents",
            get(session_subagents),
        )
        .route("/api/v1/chat/sessions/{session_id}/catch-up", get(catch_up))
        .route(
            "/api/v1/chat/sessions/{session_id}/state",
            get(session_state),
        )
        .route("/api/v1/chat/session-activity", get(session_activity))
        .route("/api/v1/chat/sessions/{session_id}/queue", get(saved_queue))
        .route("/api/v1/chat/turns/{turn_id}/hooks", get(turn_hooks))
        .route(
            "/api/v1/chat/sessions/{session_id}/pending-turn",
            get(pending_turn),
        )
        .route(
            "/api/v1/chat/sessions/{session_id}/hooks",
            get(session_hooks),
        )
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
    let feature = route_feature(request.uri().path());
    request.extensions_mut().insert(RequestId(id.clone()));
    let permit = match state.requests.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => {
            return ApiError::capacity()
                .for_feature(feature)
                .response(&id, operation.as_deref());
        }
    };
    let budget = match state
        .response_bytes
        .clone()
        .try_acquire_many_owned(MAX_RESPONSE_BYTES as u32)
    {
        Ok(p) => p,
        Err(_) => {
            return ApiError::capacity()
                .for_feature(feature)
                .response(&id, operation.as_deref());
        }
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
            Err(error) => error
                .for_feature(feature)
                .response(&id, operation.as_deref()),
        }
    })
    .await;
    let mut response = outcome.unwrap_or_else(|_| {
        ApiError::http(
            504,
            "Assistant request deadline exceeded; inspect durable state before retrying a mutation",
        )
        .for_feature(feature)
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
fn other_catalog_kind(path: &str) -> Option<CatalogKind> {
    match path.strip_prefix("/api/v1/")?.split('/').next()? {
        "chat-goals" => Some(CatalogKind::Goals),
        "chat-goal-usage-charges" => Some(CatalogKind::GoalUsageCharges),
        "chat-schedules" => Some(CatalogKind::Schedules),
        "chat-subagents" => Some(CatalogKind::Subagents),
        _ => None,
    }
}
fn route_feature(path: &str) -> &'static str {
    // These generated catalogs have no chat feature tag in the Python API.
    if other_catalog_kind(path).is_some() {
        "api"
    } else {
        "chat"
    }
}
async fn other_catalog(State(state): State<AppState>, request: Request) -> Response {
    let kind = other_catalog_kind(request.uri().path()).expect("mounted catalog route");
    catalog(state, kind, request).await
}
async fn other_catalog_record(
    State(state): State<AppState>,
    Path(id): Path<String>,
    request: Request,
) -> Response {
    let kind = other_catalog_kind(request.uri().path()).expect("mounted catalog route");
    let result = state
        .services
        .catalog_record(kind, &id)
        .await
        .map(|r| r.into_payload());
    reply(request, result)
}
async fn session_goal(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = AssistantRecords::with_clock(state.store, state.config.clock)
        .session_goal_with_clock(&session)
        .await;
    reply(request, result)
}
async fn fork_conversation(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match decode::<ForkRequest>(body, state.config.body_bytes).await {
        Ok(body) => records
            .fork_conversation(&session, body, state.config.new_fork_id)
            .await
            .map(|record| record.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply_status(&parts, result, StatusCode::CREATED)
}
async fn create_goal_conversation(State(state): State<AppState>, request: Request) -> Response {
    let (parts, body) = request.into_parts();
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match decode::<GoalConversationCreate>(body, state.config.body_bytes).await {
        Ok(body) => records
            .create_goal_conversation(
                body,
                &state.config.conversation_dependencies,
                state.config.new_goal_id,
            )
            .await
            .map(|created| created.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply_status(&parts, result, StatusCode::CREATED)
}
async fn create_goal(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match decode::<GoalDraft>(body, state.config.body_bytes).await {
        Ok(body) => records
            .create_goal(&session, body, state.config.new_goal_id)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn update_goal(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match decode::<GoalDraftUpdate>(body, state.config.body_bytes).await {
        Ok(body) => records
            .update_goal(&session, body)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn goal_children(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = state.services.goal_children(&session).await;
    reply(request, result)
}
async fn session_schedule(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = state.services.session_schedule(&session).await;
    reply(request, result)
}
async fn create_schedule(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match decode::<ScheduleCreate>(body, state.config.body_bytes).await {
        Ok(body) => records
            .create_schedule(&session, body, state.config.new_schedule_id)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn write_schedule(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match decode::<ScheduleWrite>(body, state.config.body_bytes).await {
        Ok(body) => records
            .write_schedule(&session, body)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn update_session_settings(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let (parts, body) = request.into_parts();
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    let result = match decode::<SettingsWrite>(body, state.config.body_bytes).await {
        Ok(body) => records
            .update_session_settings(&session, body)
            .await
            .map(|r| r.into_payload())
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn session_subagents(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = state
        .services
        .subagents(&session, (state.config.clock)())
        .await;
    let mut response = reply(request, result);
    if response.status().is_success() {
        response
            .headers_mut()
            .insert("cache-control", HeaderValue::from_static("no-store"));
    }
    response
}
async fn session_state(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = state
        .services
        .session_state(
            &session,
            StateObservations {
                clock: Arc::new(state.config.clock),
                connection: state.config.harness_connection,
            },
        )
        .await;
    let mut response = reply(request, result);
    if response.status().is_success() {
        response
            .headers_mut()
            .insert("cache-control", HeaderValue::from_static("no-store"));
    }
    response
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
async fn session_activity(State(state): State<AppState>, request: Request) -> Response {
    let (parts, _) = request.into_parts();
    let result = match validation::activity_project(parts.uri.query()) {
        Ok(project) => state
            .services
            .session_activity(&project)
            .await
            .map_err(ApiError::service),
        Err(error) => Err(error),
    };
    api_reply(&parts, result)
}
async fn saved_queue(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let result = state
        .services
        .saved_queue(&session, (state.config.clock)())
        .await;
    reply(request, result)
}
async fn turn_hooks(
    State(state): State<AppState>,
    Path(turn): Path<String>,
    request: Request,
) -> Response {
    let result = state.services.turn_hooks(&turn).await;
    reply(request, result)
}
async fn pending_turn(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    reply(request, records.pending_turn(&session).await)
}
async fn session_hooks(
    State(state): State<AppState>,
    Path(session): Path<String>,
    request: Request,
) -> Response {
    let records = AssistantRecords::with_clock(state.store, state.config.clock);
    reply(request, records.session_hooks(&session).await)
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
    let value = if T::MODEL == validation::BodyModel::Completion && !bytes.is_empty() {
        // Preserve the raw input order for nested validation diagnostics.
        // Root null/non-object handling still follows the common HTTP boundary.
        let root: Value = serde_json::from_slice(&bytes).map_err(|_| {
            ApiError::http(
                422,
                "Assistant request does not match its expected JSON fields",
            )
        })?;
        if root.is_object() {
            validation::completion(&bytes)?
        } else {
            root
        }
    } else if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).map_err(|_| {
            ApiError::http(
                422,
                "Assistant request does not match its expected JSON fields",
            )
        })?
    };
    let order = if matches!(
        T::MODEL,
        validation::BodyModel::Settings | validation::BodyModel::Fork
    ) {
        validation::key_order(&bytes)
    } else {
        Vec::new()
    };
    validation::validate(value, &order)
}
fn reply(request: Request, result: Result<Value, ServiceError>) -> Response {
    let (parts, _) = request.into_parts();
    api_reply(&parts, result.map_err(ApiError::service))
}
fn api_reply(parts: &axum::http::request::Parts, result: Result<Value, ApiError>) -> Response {
    api_reply_status(parts, result, StatusCode::OK)
}
fn api_reply_status(
    parts: &axum::http::request::Parts,
    result: Result<Value, ApiError>,
    success: StatusCode,
) -> Response {
    let id = parts
        .extensions
        .get::<RequestId>()
        .map_or("", |r| r.0.as_str());
    let operation = header(&parts.headers, "x-nebula-operation-id");
    let feature = route_feature(parts.uri.path());
    match result {
        Ok(value)=>match json_bytes(&value) {
            Ok(bytes) if bytes.len()<=MAX_RESPONSE_BYTES => (success,[("content-type","application/json")],bytes).into_response(),
            _=>ApiError::http(413,"Assistant response exceeds its configured limit; inspect retained records with pagination").for_feature(feature).response(id,operation.as_deref()),
        },
        Err(error)=>error.for_feature(feature).response(id,operation.as_deref()),
    }
}

/// Bound serialization while writing, not after allocating an oversized body.
fn json_bytes(value: &(impl serde::Serialize + ?Sized)) -> Result<Vec<u8>, serde_json::Error> {
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
