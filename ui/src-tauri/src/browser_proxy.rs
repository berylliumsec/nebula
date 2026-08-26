use std::{
    collections::{BTreeMap, HashMap},
    fs::{self, OpenOptions},
    future::Future,
    io,
    io::Write,
    net::IpAddr,
    path::{Path, PathBuf},
    pin::Pin,
    sync::atomic::{AtomicU64, Ordering},
    sync::{Arc, Mutex, RwLock},
    task::{Context, Poll},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(not(unix))]
use std::thread;

use base64::Engine as _;
use http_body_util::{BodyExt, Limited};
use hudsucker::hyper_util::client::legacy::connect::{
    Connected, Connection, HttpConnector,
    proxy::{SocksV5, Tunnel},
};
use hudsucker::{
    Body, HttpContext, HttpHandler, Proxy, RequestOrResponse, WebSocketContext, WebSocketHandler,
    certificate_authority::RcgenAuthority,
    hyper::{Request, Response, body::Body as _},
    rcgen::{BasicConstraints, CertificateParams, DnType, IsCa, Issuer, KeyPair, KeyUsagePurpose},
    rustls::crypto::aws_lc_rs,
    tokio_tungstenite::tungstenite::Message,
};
use hyper_rustls::{ConfigBuilderExt, HttpsConnector, HttpsConnectorBuilder};
use ipnet::IpNet;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_opener::OpenerExt;
use tokio::{
    net::TcpListener,
    sync::oneshot,
    time::{sleep, timeout},
};
use tower_service::Service;

use crate::diagnostics::{DiagnosticLevel, DiagnosticsState};

const CA_CERTIFICATE: &str = "nebula-browser-ca.pem";
const CA_PRIVATE_KEY: &str = "nebula-browser-ca-key.pem";
const CA_METADATA: &str = "nebula-browser-ca.json";

pub(crate) struct BrowserProxyHandle {
    pub(crate) url: tauri::Url,
    shutdown: Arc<Mutex<Option<oneshot::Sender<()>>>>,
    rules: Arc<Mutex<Vec<NativeProxyRule>>>,
    scope: Arc<Mutex<Option<NativeProxyScope>>>,
    connector: DynamicConnector,
    capture_bodies: Arc<std::sync::atomic::AtomicBool>,
    interception_enabled: Arc<std::sync::atomic::AtomicBool>,
    pending_intercepts: Arc<Mutex<HashMap<String, oneshot::Sender<NativeInterceptDecision>>>>,
}

impl Clone for BrowserProxyHandle {
    fn clone(&self) -> Self {
        Self {
            url: self.url.clone(),
            shutdown: self.shutdown.clone(),
            rules: self.rules.clone(),
            scope: self.scope.clone(),
            connector: self.connector.clone(),
            capture_bodies: self.capture_bodies.clone(),
            interception_enabled: self.interception_enabled.clone(),
            pending_intercepts: self.pending_intercepts.clone(),
        }
    }
}

const MAX_PROXY_MUTATION_BYTES: usize = 1_048_576;
const MAX_CAPTURE_BODY_BYTES: usize = 1_048_576;

#[derive(Clone, Debug)]
pub(crate) struct NativeUpstreamProxyConfig {
    pub(crate) url: String,
    pub(crate) credential: Option<String>,
}

type DirectConnector = HttpsConnector<HttpConnector>;
type HttpTunnelConnector = HttpsConnector<Tunnel<HttpConnector>>;
type HttpsTunnelConnector = HttpsConnector<Tunnel<DirectConnector>>;
type SocksConnector = HttpsConnector<SocksV5<HttpConnector>>;
type BoxConnectorError = Box<dyn std::error::Error + Send + Sync>;
type BoxConnectorFuture =
    Pin<Box<dyn Future<Output = Result<NativeTransport, BoxConnectorError>> + Send>>;

#[derive(Clone)]
enum NativeConnector {
    Direct(DirectConnector),
    HttpTunnel(HttpTunnelConnector),
    HttpsTunnel(HttpsTunnelConnector),
    Socks5(SocksConnector),
}

#[allow(clippy::large_enum_variant)]
enum NativeTransport {
    Direct(<DirectConnector as Service<hudsucker::hyper::Uri>>::Response),
    HttpTunnel(<HttpTunnelConnector as Service<hudsucker::hyper::Uri>>::Response),
    HttpsTunnel(<HttpsTunnelConnector as Service<hudsucker::hyper::Uri>>::Response),
    Socks5(<SocksConnector as Service<hudsucker::hyper::Uri>>::Response),
}

#[derive(Clone)]
struct DynamicConnector {
    current: Arc<RwLock<NativeConnector>>,
}

impl DynamicConnector {
    fn new(config: Option<NativeUpstreamProxyConfig>) -> Result<Self, String> {
        Ok(Self {
            current: Arc::new(RwLock::new(build_native_connector(config)?)),
        })
    }

    fn configure(&self, config: Option<NativeUpstreamProxyConfig>) -> Result<(), String> {
        let next = build_native_connector(config)?;
        *self
            .current
            .write()
            .map_err(|_| "The native upstream connector registry is unavailable.".to_string())? =
            next;
        Ok(())
    }
}

impl Service<hudsucker::hyper::Uri> for DynamicConnector {
    type Response = NativeTransport;
    type Error = BoxConnectorError;
    type Future = BoxConnectorFuture;

    fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, destination: hudsucker::hyper::Uri) -> Self::Future {
        let connector = match self.current.read() {
            Ok(value) => value.clone(),
            Err(_) => {
                return Box::pin(async {
                    Err::<NativeTransport, BoxConnectorError>(
                        "the native upstream connector registry is unavailable".into(),
                    )
                });
            }
        };
        let http_destination = with_default_http_port(destination.clone());
        Box::pin(async move {
            match connector {
                NativeConnector::Direct(mut value) => {
                    value.call(destination).await.map(NativeTransport::Direct)
                }
                NativeConnector::HttpTunnel(mut value) => value
                    .call(http_destination)
                    .await
                    .map(NativeTransport::HttpTunnel),
                NativeConnector::HttpsTunnel(mut value) => value
                    .call(http_destination)
                    .await
                    .map(NativeTransport::HttpsTunnel),
                NativeConnector::Socks5(mut value) => value
                    .call(http_destination)
                    .await
                    .map(NativeTransport::Socks5),
            }
        })
    }
}

fn with_default_http_port(destination: hudsucker::hyper::Uri) -> hudsucker::hyper::Uri {
    if destination.scheme_str() != Some("http") || destination.port().is_some() {
        return destination;
    }
    let mut parts = destination.into_parts();
    let Some(authority) = parts.authority.take() else {
        return hudsucker::hyper::Uri::from_parts(parts).unwrap_or_default();
    };
    let authority_value = if authority.host().starts_with('[') {
        format!("{}:80", authority.host())
    } else if authority.host().contains(':') {
        format!("[{}]:80", authority.host())
    } else {
        format!("{}:80", authority.host())
    };
    let Ok(next_authority) = authority_value.parse() else {
        return hudsucker::hyper::Uri::from_parts(parts).unwrap_or_default();
    };
    parts.authority = Some(next_authority);
    hudsucker::hyper::Uri::from_parts(parts).unwrap_or_default()
}

impl Connection for NativeTransport {
    fn connected(&self) -> Connected {
        match self {
            Self::Direct(value) => value.connected(),
            Self::HttpTunnel(value) => value.connected(),
            Self::HttpsTunnel(value) => value.connected(),
            Self::Socks5(value) => value.connected(),
        }
    }
}

impl hudsucker::hyper::rt::Read for NativeTransport {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: hudsucker::hyper::rt::ReadBufCursor<'_>,
    ) -> Poll<Result<(), io::Error>> {
        match Pin::get_mut(self) {
            Self::Direct(value) => Pin::new(value).poll_read(cx, buf),
            Self::HttpTunnel(value) => Pin::new(value).poll_read(cx, buf),
            Self::HttpsTunnel(value) => Pin::new(value).poll_read(cx, buf),
            Self::Socks5(value) => Pin::new(value).poll_read(cx, buf),
        }
    }
}

impl hudsucker::hyper::rt::Write for NativeTransport {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<Result<usize, io::Error>> {
        match Pin::get_mut(self) {
            Self::Direct(value) => Pin::new(value).poll_write(cx, buf),
            Self::HttpTunnel(value) => Pin::new(value).poll_write(cx, buf),
            Self::HttpsTunnel(value) => Pin::new(value).poll_write(cx, buf),
            Self::Socks5(value) => Pin::new(value).poll_write(cx, buf),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<(), io::Error>> {
        match Pin::get_mut(self) {
            Self::Direct(value) => Pin::new(value).poll_flush(cx),
            Self::HttpTunnel(value) => Pin::new(value).poll_flush(cx),
            Self::HttpsTunnel(value) => Pin::new(value).poll_flush(cx),
            Self::Socks5(value) => Pin::new(value).poll_flush(cx),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Result<(), io::Error>> {
        match Pin::get_mut(self) {
            Self::Direct(value) => Pin::new(value).poll_shutdown(cx),
            Self::HttpTunnel(value) => Pin::new(value).poll_shutdown(cx),
            Self::HttpsTunnel(value) => Pin::new(value).poll_shutdown(cx),
            Self::Socks5(value) => Pin::new(value).poll_shutdown(cx),
        }
    }
}

fn upstream_uri(value: &str) -> Result<hudsucker::hyper::Uri, String> {
    let uri = value
        .parse::<hudsucker::hyper::Uri>()
        .map_err(|_| "the upstream proxy URL is invalid".to_string())?;
    let scheme = uri
        .scheme_str()
        .map(str::to_ascii_lowercase)
        .ok_or_else(|| "the upstream proxy URL requires a scheme".to_string())?;
    if !matches!(scheme.as_str(), "http" | "https" | "socks5") {
        return Err("upstream proxy must use http, https, or socks5".to_string());
    }
    let authority = uri
        .authority()
        .ok_or_else(|| "the upstream proxy URL requires a host and port".to_string())?;
    if authority.as_str().contains('@')
        || (!uri.path().is_empty() && uri.path() != "/")
        || uri.query().is_some()
    {
        return Err(
            "upstream proxy URLs cannot contain credentials, paths, or queries".to_string(),
        );
    }
    if uri.host().is_none() || uri.port_u16().is_none() {
        return Err("upstream proxy URLs must include an explicit port".to_string());
    }
    Ok(uri)
}

fn credential_pair(value: Option<&str>) -> Result<Option<(String, String)>, String> {
    let Some(value) = value else {
        return Ok(None);
    };
    let (username, password) = value.split_once(':').ok_or_else(|| {
        "the upstream credential must contain username:password in protected storage".to_string()
    })?;
    if username.is_empty() || password.is_empty() || username.len() > 255 || password.len() > 255 {
        return Err(
            "the upstream credential has an invalid username or password length".to_string(),
        );
    }
    Ok(Some((username.to_string(), password.to_string())))
}

fn direct_connector() -> Result<DirectConnector, String> {
    let builder = HttpsConnectorBuilder::new()
        .with_provider_and_webpki_roots(aws_lc_rs::default_provider())
        .map_err(|error| format!("cannot initialize upstream TLS roots: {error}"))?
        .https_or_http()
        .enable_http1()
        .enable_http2();
    Ok(builder.build())
}

fn proxy_tls_config() -> Result<hudsucker::rustls::ClientConfig, String> {
    hudsucker::rustls::ClientConfig::builder_with_provider(Arc::new(aws_lc_rs::default_provider()))
        .with_safe_default_protocol_versions()
        .map_err(|error| format!("cannot initialize upstream WebSocket TLS: {error}"))
        .map(|builder| builder.with_webpki_roots().with_no_client_auth())
}

fn basic_auth_header(
    value: Option<&str>,
) -> Result<Option<hudsucker::hyper::header::HeaderValue>, String> {
    let Some((username, password)) = credential_pair(value)? else {
        return Ok(None);
    };
    let encoded =
        base64::engine::general_purpose::STANDARD.encode(format!("{username}:{password}"));
    hudsucker::hyper::header::HeaderValue::from_str(&format!("Basic {encoded}"))
        .map(Some)
        .map_err(|_| "the upstream credential could not be encoded safely".to_string())
}

fn build_native_connector(
    config: Option<NativeUpstreamProxyConfig>,
) -> Result<NativeConnector, String> {
    let Some(config) = config else {
        return direct_connector().map(NativeConnector::Direct);
    };
    let uri = upstream_uri(&config.url)?;
    let scheme = uri.scheme_str().unwrap_or_default();
    let credentials = config.credential.as_deref();
    match scheme {
        "http" => {
            let mut tunnel = Tunnel::new(uri, HttpConnector::new());
            if let Some(auth) = basic_auth_header(credentials)? {
                tunnel = tunnel.with_auth(auth);
            }
            let connector = HttpsConnectorBuilder::new()
                .with_provider_and_webpki_roots(aws_lc_rs::default_provider())
                .map_err(|error| format!("cannot initialize upstream TLS roots: {error}"))?
                .https_or_http()
                .enable_http1()
                .enable_http2()
                .wrap_connector(tunnel);
            Ok(NativeConnector::HttpTunnel(connector))
        }
        "https" => {
            let lower = direct_connector()?;
            let mut tunnel = Tunnel::new(uri, lower);
            if let Some(auth) = basic_auth_header(credentials)? {
                tunnel = tunnel.with_auth(auth);
            }
            let connector = HttpsConnectorBuilder::new()
                .with_provider_and_webpki_roots(aws_lc_rs::default_provider())
                .map_err(|error| format!("cannot initialize upstream TLS roots: {error}"))?
                .https_or_http()
                .enable_http1()
                .enable_http2()
                .wrap_connector(tunnel);
            Ok(NativeConnector::HttpsTunnel(connector))
        }
        "socks5" => {
            let proxy_tcp_uri = format!(
                "http://{}:{}",
                uri.host()
                    .ok_or_else(|| "upstream SOCKS5 proxy host is missing".to_string())?,
                uri.port_u16()
                    .ok_or_else(|| "upstream SOCKS5 proxy port is missing".to_string())?,
            )
            .parse::<hudsucker::hyper::Uri>()
            .map_err(|_| "upstream SOCKS5 proxy address is invalid".to_string())?;
            let mut socks = SocksV5::new(proxy_tcp_uri, HttpConnector::new());
            if let Some((username, password)) = credential_pair(credentials)? {
                socks = socks.with_auth(username, password);
            }
            let connector = HttpsConnectorBuilder::new()
                .with_provider_and_webpki_roots(aws_lc_rs::default_provider())
                .map_err(|error| format!("cannot initialize upstream TLS roots: {error}"))?
                .https_or_http()
                .enable_http1()
                .enable_http2()
                .wrap_connector(socks);
            Ok(NativeConnector::Socks5(connector))
        }
        _ => Err("unsupported upstream proxy scheme".to_string()),
    }
}

#[derive(Clone, Debug)]
pub(crate) struct NativeProxyRule {
    pub(crate) id: String,
    pub(crate) match_criteria: serde_json::Map<String, serde_json::Value>,
    pub(crate) action: serde_json::Map<String, serde_json::Value>,
    pub(crate) priority: i32,
    expires_at: Option<time::OffsetDateTime>,
}

#[derive(Clone, Debug)]
struct NativeProxyUrl {
    scheme: String,
    host: String,
    port: u16,
    path: String,
}

#[derive(Clone, Debug)]
struct NativeProxyScope {
    revision: u64,
    allowed_cidrs: Vec<IpNet>,
    allowed_domains: Vec<String>,
    allowed_urls: Vec<NativeProxyUrl>,
    allowed_ports: Vec<u16>,
    allow_all_targets: bool,
    not_before: Option<time::OffsetDateTime>,
    not_after: Option<time::OffsetDateTime>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct NativeProxyScopeInput {
    pub(crate) revision: u64,
    pub(crate) allowed_cidrs: Vec<String>,
    pub(crate) allowed_domains: Vec<String>,
    pub(crate) allowed_urls: Vec<String>,
    pub(crate) allowed_ports: Vec<u16>,
    pub(crate) allow_all_targets: bool,
    pub(crate) not_before: Option<String>,
    pub(crate) not_after: Option<String>,
}

impl BrowserProxyHandle {
    pub(crate) fn configure_rule(
        &self,
        id: String,
        match_criteria: serde_json::Map<String, serde_json::Value>,
        action: serde_json::Map<String, serde_json::Value>,
        priority: i32,
        expires_at: Option<String>,
        enabled: bool,
    ) -> Result<(), String> {
        validate_native_rule(&match_criteria, &action)?;
        let expires_at = expires_at
            .map(|value| {
                time::OffsetDateTime::parse(&value, &time::format_description::well_known::Rfc3339)
                    .map_err(|error| format!("invalid proxy rule expiry: {error}"))
            })
            .transpose()?;
        let mut rules = self
            .rules
            .lock()
            .map_err(|_| "The browser proxy rule registry is unavailable.".to_string())?;
        rules.retain(|rule| rule.id != id);
        if enabled {
            rules.push(NativeProxyRule {
                id,
                match_criteria,
                action,
                priority,
                expires_at,
            });
            rules.sort_by_key(|rule| rule.priority);
        }
        Ok(())
    }

    pub(crate) fn configure_scope(&self, input: NativeProxyScopeInput) -> Result<(), String> {
        let compiled = match compile_scope(input) {
            Ok(scope) => scope,
            Err(error) => {
                if let Ok(mut scope) = self.scope.lock() {
                    // A malformed or stale scope must not leave the last scope
                    // active while the caller believes this update failed.
                    *scope = None;
                }
                return Err(error);
            }
        };
        self.scope
            .lock()
            .map_err(|_| "The browser proxy scope registry is unavailable.".to_string())?
            .replace(compiled);
        Ok(())
    }

    pub(crate) fn clear_scope(&self) -> Result<(), String> {
        self.scope
            .lock()
            .map_err(|_| "The browser proxy scope registry is unavailable.".to_string())?
            .take();
        Ok(())
    }

    pub(crate) fn configure_upstream(
        &self,
        config: Option<NativeUpstreamProxyConfig>,
        capture_bodies: bool,
        interception_enabled: bool,
    ) -> Result<(), String> {
        self.connector.configure(config)?;
        self.capture_bodies.store(capture_bodies, Ordering::Relaxed);
        self.interception_enabled
            .store(interception_enabled, Ordering::Relaxed);
        Ok(())
    }

    pub(crate) fn decide_intercept(
        &self,
        transaction_id: &str,
        decision: NativeInterceptDecision,
    ) -> Result<(), String> {
        resolve_pending_intercept(&self.pending_intercepts, transaction_id, decision)
    }
}

#[derive(Clone, Copy, Debug)]
pub(crate) enum NativeInterceptDecision {
    Forward,
    Drop,
}

fn resolve_pending_intercept(
    pending: &Arc<Mutex<HashMap<String, oneshot::Sender<NativeInterceptDecision>>>>,
    transaction_id: &str,
    decision: NativeInterceptDecision,
) -> Result<(), String> {
    let sender = pending
        .lock()
        .map_err(|_| "The native intercept registry is unavailable.".to_string())?
        .remove(transaction_id)
        .ok_or_else(|| "The native transaction is no longer paused.".to_string())?;
    sender
        .send(decision)
        .map_err(|_| "The native transaction expired before the decision arrived.".to_string())
}

impl BrowserProxyHandle {
    pub(crate) fn shutdown_now(&self) {
        if let Some(shutdown) = self.shutdown.lock().ok().and_then(|mut value| value.take()) {
            let _ = shutdown.send(()); // diagnostic-expected: the proxy task may already have stopped
        }
    }
}

impl Drop for BrowserProxyHandle {
    fn drop(&mut self) {
        if Arc::strong_count(&self.shutdown) > 1 {
            return;
        }
        self.shutdown_now();
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BrowserCaStatus {
    pub(crate) certificate_path: String,
    pub(crate) fingerprint: String,
    pub(crate) generated_at: Option<String>,
    pub(crate) expires_at: Option<String>,
    pub(crate) state: &'static str,
    pub(crate) trust_instructions: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
struct CaMetadata {
    generated_at: String,
    expires_at: String,
    fingerprint: String,
}

#[derive(Clone)]
struct PendingRequest {
    request_id: u64,
    method: String,
    url: String,
    protocol: &'static str,
    request_headers: BTreeMap<String, String>,
    request_bytes: Option<u64>,
    started: Instant,
    rules: Vec<NativeProxyRule>,
    request_body: Option<CapturedBody>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct CapturedBody {
    base64: String,
    media_type: Option<String>,
    bytes: usize,
    truncated: bool,
}

struct CaptureHandler {
    app: AppHandle,
    project_id: String,
    session_id: String,
    tab_id: String,
    pending: Mutex<HashMap<u64, PendingRequest>>,
    next_request_id: Arc<AtomicU64>,
    rules: Arc<Mutex<Vec<NativeProxyRule>>>,
    scope: Arc<Mutex<Option<NativeProxyScope>>>,
    capture_bodies: Arc<std::sync::atomic::AtomicBool>,
    interception_enabled: Arc<std::sync::atomic::AtomicBool>,
    pending_intercepts: Arc<Mutex<HashMap<String, oneshot::Sender<NativeInterceptDecision>>>>,
}

impl Clone for CaptureHandler {
    fn clone(&self) -> Self {
        Self {
            app: self.app.clone(),
            project_id: self.project_id.clone(),
            session_id: self.session_id.clone(),
            tab_id: self.tab_id.clone(),
            // Hudsucker clones a handler for each proxied request. Keep the
            // correlation state per request handler while sharing only session
            // rules, so concurrent HTTP/2 streams never share a response slot.
            pending: Mutex::new(HashMap::new()),
            next_request_id: self.next_request_id.clone(),
            rules: self.rules.clone(),
            scope: self.scope.clone(),
            capture_bodies: self.capture_bodies.clone(),
            interception_enabled: self.interception_enabled.clone(),
            pending_intercepts: self.pending_intercepts.clone(),
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct TrafficEvent {
    session_id: String,
    tab_id: String,
    method: String,
    url: String,
    protocol: &'static str,
    status_code: Option<u16>,
    request_headers: BTreeMap<String, String>,
    response_headers: BTreeMap<String, String>,
    request_bytes: Option<u64>,
    response_bytes: Option<u64>,
    duration_ms: u64,
    error: Option<String>,
    request_id: u64,
    blocked: bool,
    request_body: Option<CapturedBody>,
    response_body: Option<CapturedBody>,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct InterceptEvent {
    project_id: String,
    session_id: String,
    tab_id: String,
    transaction_id: String,
    phase: &'static str,
    method: String,
    url: String,
    headers: Vec<(String, String)>,
    status_code: Option<u16>,
    timeout_seconds: u64,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct WebSocketFrameEvent {
    session_id: String,
    tab_id: String,
    url: String,
    direction: &'static str,
    opcode: &'static str,
    payload_preview: String,
    payload_sha256: String,
    payload_bytes: usize,
    truncated: bool,
}

fn record_proxy_failure(app: &AppHandle, event_code: &str, message: &str, stage: &str) {
    drop(app.state::<DiagnosticsState>().record_desktop(
        DiagnosticLevel::Error,
        event_code,
        message,
        Some("failure"),
        Some(stage),
        Some(true),
        serde_json::Map::new(),
    ));
}

fn protocol(version: hudsucker::hyper::Version) -> &'static str {
    use hudsucker::hyper::Version;
    match version {
        Version::HTTP_10 => "http/1.0",
        Version::HTTP_11 => "http/1.1",
        Version::HTTP_2 => "h2",
        Version::HTTP_3 => "h3",
        _ => "unknown",
    }
}

fn sensitive_header(name: &str) -> bool {
    let normalized = name.to_ascii_lowercase();
    [
        "authorization",
        "cookie",
        "csrf",
        "xsrf",
        "api-key",
        "apikey",
        "token",
    ]
    .iter()
    .any(|fragment| normalized.contains(fragment))
}

fn redacted_headers(headers: &hudsucker::hyper::HeaderMap) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    for (name, value) in headers {
        let visible = if sensitive_header(name.as_str()) {
            format!("<redacted:sha256:{:x}>", Sha256::digest(value.as_bytes()))
        } else {
            value
                .to_str()
                .map(|item| item.chars().take(8_192).collect())
                .unwrap_or_else(|_| "<non-utf8>".to_string())
        };
        values.insert(name.to_string(), visible);
        if values.len() == 200 {
            break;
        }
    }
    values
}

fn secret_header(name: &str) -> bool {
    let normalized = name.to_ascii_lowercase();
    [
        "authorization",
        "cookie",
        "set-cookie",
        "csrf",
        "xsrf",
        "api-key",
        "apikey",
        "token",
    ]
    .iter()
    .any(|fragment| normalized.contains(fragment))
}

fn validate_native_rule(
    match_criteria: &serde_json::Map<String, serde_json::Value>,
    action: &serde_json::Map<String, serde_json::Value>,
) -> Result<(), String> {
    let action_type = action
        .get("type")
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "proxy rules require a declarative action type".to_string())?;
    if !matches!(
        action_type,
        "pass"
            | "block"
            | "redirect"
            | "delay"
            | "set_header"
            | "remove_header"
            | "replace_body"
            | "ws_drop"
            | "ws_replace"
    ) {
        return Err("proxy rules use an unsupported declarative action".to_string());
    }
    let encoded = serde_json::to_vec(&serde_json::json!({
        "match": match_criteria,
        "action": action,
    }))
    .map_err(|error| format!("cannot encode proxy rule: {error}"))?;
    if encoded.len() > 32_000 {
        return Err("proxy rule payload is too large".to_string());
    }
    if encoded
        .windows("javascript".len())
        .any(|window| window.eq_ignore_ascii_case(b"javascript"))
    {
        return Err("proxy rules cannot contain executable scripts".to_string());
    }
    if matches!(action_type, "set_header" | "remove_header") {
        let name = action
            .get("name")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| "header rules require a header name".to_string())?;
        if secret_header(name) {
            return Err("proxy rules cannot set or remove secret-bearing headers".to_string());
        }
    }
    if matches!(action_type, "replace_body" | "ws_replace") {
        let replacement = action
            .get("body")
            .or_else(|| action.get("value"))
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| "body replacement rules require a bounded string body".to_string())?;
        if replacement.len() > MAX_PROXY_MUTATION_BYTES {
            return Err("proxy body replacement exceeds the 1 MiB limit".to_string());
        }
    }
    Ok(())
}

fn rule_matches(rule: &NativeProxyRule, uri: &hudsucker::hyper::Uri, method: &str) -> bool {
    let criteria = &rule.match_criteria;
    let string = |key: &str| criteria.get(key).and_then(serde_json::Value::as_str);
    if let Some(expected) = string("method") {
        if !method.eq_ignore_ascii_case(expected) {
            return false;
        }
    }
    if let Some(expected) = string("host") {
        if uri.host().map(|value| value.to_ascii_lowercase()) != Some(expected.to_ascii_lowercase())
        {
            return false;
        }
    }
    if let Some(expected) = string("path") {
        if expected != "/"
            && uri.path() != expected
            && !uri.path().starts_with(&format!("{expected}/"))
        {
            return false;
        }
    }
    if let Some(expected) = string("url_prefix") {
        if !uri.to_string().starts_with(expected) {
            return false;
        }
    }
    if let Some(expected) = string("protocol") {
        let actual = if uri.scheme_str() == Some("https") {
            "https"
        } else {
            "http"
        };
        if !actual.eq_ignore_ascii_case(expected) {
            return false;
        }
    }
    true
}

fn snapshot_rules(rules: &Arc<Mutex<Vec<NativeProxyRule>>>) -> Vec<NativeProxyRule> {
    let now = time::OffsetDateTime::now_utc();
    rules
        .lock()
        .map(|value| {
            value
                .iter()
                .filter(|rule| rule.expires_at.is_none_or(|expires_at| expires_at > now))
                .cloned()
                .collect()
        })
        .unwrap_or_default()
}

const MAX_SCOPE_ENTRIES: usize = 512;

fn parse_scope_time(
    value: Option<String>,
    field: &str,
) -> Result<Option<time::OffsetDateTime>, String> {
    value
        .map(|value| {
            time::OffsetDateTime::parse(&value, &time::format_description::well_known::Rfc3339)
                .map_err(|error| format!("invalid Project scope {field}: {error}"))
        })
        .transpose()
}

fn normalize_scope_domain(value: &str) -> Result<String, String> {
    let domain = value.trim().trim_end_matches('.').to_ascii_lowercase();
    let suffix = domain.strip_prefix("*.").unwrap_or(&domain);
    if domain.is_empty()
        || suffix.is_empty()
        || suffix.contains('*')
        || suffix.contains('/')
        || suffix.contains(':')
        || suffix.chars().any(char::is_whitespace)
    {
        return Err("Project scope contains an invalid domain pattern".to_string());
    }
    Ok(domain)
}

fn compile_scope_url(value: &str) -> Result<NativeProxyUrl, String> {
    let uri = value
        .parse::<hudsucker::hyper::Uri>()
        .map_err(|_| "Project scope contains an invalid URL".to_string())?;
    let scheme = uri
        .scheme_str()
        .map(str::to_ascii_lowercase)
        .ok_or_else(|| "Project scope URLs require an HTTP or HTTPS scheme".to_string())?;
    if !matches!(scheme.as_str(), "http" | "https") {
        return Err("Project scope URLs require an HTTP or HTTPS scheme".to_string());
    }
    let authority = uri
        .authority()
        .ok_or_else(|| "Project scope URLs require a host".to_string())?;
    if authority.as_str().contains('@') {
        return Err("Project scope URLs cannot contain credentials".to_string());
    }
    let host = uri
        .host()
        .map(str::to_ascii_lowercase)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "Project scope URLs require a host".to_string())?;
    let port = uri
        .port_u16()
        .unwrap_or(if scheme == "https" { 443 } else { 80 });
    Ok(NativeProxyUrl {
        scheme,
        host,
        port,
        path: if uri.path().is_empty() {
            "/".to_string()
        } else {
            uri.path().to_string()
        },
    })
}

fn compile_scope(input: NativeProxyScopeInput) -> Result<NativeProxyScope, String> {
    if input.revision == 0 {
        return Err("Project scope revision must be positive".to_string());
    }
    if input.allowed_cidrs.len() > MAX_SCOPE_ENTRIES
        || input.allowed_domains.len() > MAX_SCOPE_ENTRIES
        || input.allowed_urls.len() > MAX_SCOPE_ENTRIES
        || input.allowed_ports.len() > MAX_SCOPE_ENTRIES
    {
        return Err("Project scope contains too many entries".to_string());
    }
    let allowed_cidrs = input
        .allowed_cidrs
        .iter()
        .map(|value| {
            value
                .parse::<IpNet>()
                .map_err(|_| "Project scope contains an invalid CIDR".to_string())
        })
        .collect::<Result<Vec<_>, _>>()?;
    let allowed_domains = input
        .allowed_domains
        .iter()
        .map(|value| normalize_scope_domain(value))
        .collect::<Result<Vec<_>, _>>()?;
    let allowed_urls = input
        .allowed_urls
        .iter()
        .map(|value| compile_scope_url(value))
        .collect::<Result<Vec<_>, _>>()?;
    let mut allowed_ports = input.allowed_ports;
    if allowed_ports.contains(&0) {
        return Err("Project scope ports must be between 1 and 65535".to_string());
    }
    allowed_ports.sort_unstable();
    allowed_ports.dedup();
    let not_before = parse_scope_time(input.not_before, "start time")?;
    let not_after = parse_scope_time(input.not_after, "expiry time")?;
    if let (Some(start), Some(end)) = (not_before, not_after) {
        if end <= start {
            return Err("Project scope expiry must be after its start time".to_string());
        }
    }
    Ok(NativeProxyScope {
        revision: input.revision,
        allowed_cidrs,
        allowed_domains,
        allowed_urls,
        allowed_ports,
        allow_all_targets: input.allow_all_targets,
        not_before,
        not_after,
    })
}

fn normalized_request_scheme(uri: &hudsucker::hyper::Uri) -> Option<String> {
    match uri.scheme_str()?.to_ascii_lowercase().as_str() {
        "http" | "ws" => Some("http".to_string()),
        "https" | "wss" => Some("https".to_string()),
        _ => None,
    }
}

fn request_port(uri: &hudsucker::hyper::Uri, scheme: &str) -> u16 {
    uri.port_u16()
        .unwrap_or(if scheme == "https" { 443 } else { 80 })
}

fn domain_matches(host: &str, pattern: &str) -> bool {
    let host = host.trim_end_matches('.').to_ascii_lowercase();
    let pattern = pattern.trim_end_matches('.').to_ascii_lowercase();
    if let Some(suffix) = pattern.strip_prefix("*.") {
        host.ends_with(&format!(".{suffix}")) && host != suffix
    } else {
        host == pattern
    }
}

fn scope_allows_uri(scope: &NativeProxyScope, uri: &hudsucker::hyper::Uri) -> bool {
    let Some(scheme) = normalized_request_scheme(uri) else {
        return false;
    };
    let Some(host) = uri.host().map(str::to_ascii_lowercase) else {
        return false;
    };
    let port = request_port(uri, &scheme);
    if !scope.allowed_ports.is_empty() && !scope.allowed_ports.contains(&port) {
        return false;
    }
    let now = time::OffsetDateTime::now_utc();
    if scope.not_before.is_some_and(|value| now < value)
        || scope.not_after.is_some_and(|value| now >= value)
    {
        return false;
    }
    if scope.allow_all_targets {
        return true;
    }
    let host_allowed = host
        .parse::<IpAddr>()
        .map(|address| {
            scope
                .allowed_cidrs
                .iter()
                .any(|network| network.contains(&address))
        })
        .unwrap_or_else(|_| {
            scope
                .allowed_domains
                .iter()
                .any(|pattern| domain_matches(&host, pattern))
        });
    if host_allowed {
        return true;
    }
    scope.allowed_urls.iter().any(|allowed| {
        let allowed_path = allowed.path.trim_end_matches('/');
        let request_path = uri.path().trim_end_matches('/');
        allowed.scheme == scheme
            && allowed.host == host
            && allowed.port == port
            && (allowed_path.is_empty()
                || request_path == allowed_path
                || request_path.starts_with(&format!("{allowed_path}/")))
    })
}

fn snapshot_scope(scope: &Arc<Mutex<Option<NativeProxyScope>>>) -> Option<NativeProxyScope> {
    scope.lock().ok().and_then(|value| value.clone())
}

fn scope_uri_for_request(request: &Request<Body>) -> Option<hudsucker::hyper::Uri> {
    if request.method() == hudsucker::hyper::Method::CONNECT && request.uri().scheme().is_none() {
        let authority = request.uri().authority()?.as_str();
        return format!("https://{authority}/").parse().ok(); // diagnostic-expected: malformed CONNECT authorities fail closed
    }
    Some(request.uri().clone())
}

fn local_response(status: hudsucker::hyper::StatusCode, detail: &str) -> Response<Body> {
    Response::builder()
        .status(status)
        .header("content-type", "text/plain; charset=utf-8")
        .header("cache-control", "no-store")
        .body(Body::from(detail.chars().take(2_000).collect::<String>()))
        .unwrap_or_else(|_| Response::new(Body::empty()))
}

fn replace_body_bytes(
    headers: &mut hudsucker::hyper::HeaderMap,
    replacement: &[u8],
) -> Result<(), String> {
    if replacement.len() > MAX_PROXY_MUTATION_BYTES {
        return Err("proxy body replacement exceeds the 1 MiB limit".to_string());
    }
    if headers
        .get("content-encoding")
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| !value.eq_ignore_ascii_case("identity"))
    {
        return Err("compressed bodies cannot be replaced without an explicit decoder".to_string());
    }
    headers.remove("content-encoding");
    headers.insert(
        "content-length",
        hudsucker::hyper::header::HeaderValue::from_str(&replacement.len().to_string())
            .map_err(|error| format!("cannot set mutated content length: {error}"))?,
    );
    Ok(())
}

fn replacement_value(
    action: &serde_json::Map<String, serde_json::Value>,
) -> Result<Vec<u8>, String> {
    let value = action
        .get("body")
        .or_else(|| action.get("value"))
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| "body replacement rules require a bounded string body".to_string())?;
    if value.len() > MAX_PROXY_MUTATION_BYTES {
        return Err("proxy body replacement exceeds the 1 MiB limit".to_string());
    }
    Ok(value.as_bytes().to_vec())
}

async fn collect_bounded(body: Body) -> Result<Vec<u8>, ()> {
    Limited::new(body, MAX_CAPTURE_BODY_BYTES + 1)
        .collect()
        .await
        .map(|value| value.to_bytes().to_vec())
        .map_err(|_| ())
}

fn capturable_media_type(headers: &hudsucker::hyper::HeaderMap) -> Option<String> {
    let media_type = headers
        .get(hudsucker::hyper::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .map(|value| {
            value
                .split_once(';')
                .map_or(value, |(head, _)| head)
                .trim()
                .to_ascii_lowercase()
        })
        .filter(|value| {
            value.starts_with("text/")
                || value.contains("json")
                || value == "application/x-www-form-urlencoded"
                || value == "application/graphql"
                || value == "application/xml"
        })?;
    if headers
        .get(hudsucker::hyper::header::CONTENT_ENCODING)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| !value.eq_ignore_ascii_case("identity"))
    {
        return None;
    }
    Some(media_type)
}

async fn capture_body(
    body: Body,
    headers: &hudsucker::hyper::HeaderMap,
) -> Result<(Body, Option<CapturedBody>), ()> {
    let Some(media_type) = capturable_media_type(headers) else {
        return Ok((body, None));
    };
    let bytes = collect_bounded(body).await?;
    if bytes.is_empty() {
        return Ok((Body::from(bytes), None));
    }
    let truncated = bytes.len() > MAX_CAPTURE_BODY_BYTES;
    if truncated {
        return Err(());
    }
    let capture = CapturedBody {
        base64: base64::engine::general_purpose::STANDARD.encode(&bytes),
        media_type: Some(media_type),
        bytes: bytes.len(),
        truncated: false,
    };
    Ok((Body::from(bytes), Some(capture)))
}

#[allow(clippy::result_large_err)]
async fn mutate_request(
    mut request: Request<Body>,
    rules: &[NativeProxyRule],
) -> Result<(Request<Body>, Vec<NativeProxyRule>), Response<Body>> {
    let mut matched_rules = Vec::new();
    for rule in rules {
        if !rule_matches(rule, request.uri(), request.method().as_str()) {
            continue;
        }
        let action_type = rule
            .action
            .get("type")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("pass");
        match action_type {
            "pass" => {}
            "block" => {
                return Err(local_response(
                    hudsucker::hyper::StatusCode::FORBIDDEN,
                    "Nebula blocked this request by an active Project proxy rule.",
                ));
            }
            "delay" => {
                let millis = rule
                    .action
                    .get("milliseconds")
                    .or_else(|| rule.action.get("ms"))
                    .and_then(serde_json::Value::as_u64)
                    .unwrap_or(0)
                    .min(30_000);
                sleep(Duration::from_millis(millis)).await;
            }
            "redirect" => {
                let target = rule
                    .action
                    .get("url")
                    .or_else(|| rule.action.get("target"))
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| {
                        local_response(
                            hudsucker::hyper::StatusCode::BAD_GATEWAY,
                            "Nebula rejected an invalid proxy redirect rule.",
                        )
                    })?;
                let parsed = target.parse::<hudsucker::hyper::Uri>().map_err(|_| {
                    local_response(
                        hudsucker::hyper::StatusCode::BAD_GATEWAY,
                        "Nebula rejected an invalid proxy redirect rule.",
                    )
                })?;
                if !matches!(parsed.scheme_str(), Some("http") | Some("https"))
                    || parsed.authority().is_none()
                    || parsed
                        .authority()
                        .is_some_and(|authority| authority.as_str().contains('@'))
                    || target.contains('#')
                {
                    return Err(local_response(
                        hudsucker::hyper::StatusCode::BAD_GATEWAY,
                        "Nebula rejected an invalid proxy redirect rule.",
                    ));
                }
                *request.uri_mut() = parsed;
            }
            "set_header" => {
                let name = rule
                    .action
                    .get("name")
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| {
                        local_response(
                            hudsucker::hyper::StatusCode::BAD_GATEWAY,
                            "Nebula rejected an invalid header rule.",
                        )
                    })?;
                let value = rule
                    .action
                    .get("value")
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| {
                        local_response(
                            hudsucker::hyper::StatusCode::BAD_GATEWAY,
                            "Nebula rejected an invalid header rule.",
                        )
                    })?;
                let name = hudsucker::hyper::header::HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| {
                        local_response(
                            hudsucker::hyper::StatusCode::BAD_GATEWAY,
                            "Nebula rejected an invalid header name.",
                        )
                    })?;
                let value =
                    hudsucker::hyper::header::HeaderValue::from_str(value).map_err(|_| {
                        local_response(
                            hudsucker::hyper::StatusCode::BAD_GATEWAY,
                            "Nebula rejected an invalid header value.",
                        )
                    })?;
                request.headers_mut().insert(name, value);
            }
            "remove_header" => {
                let name = rule
                    .action
                    .get("name")
                    .and_then(serde_json::Value::as_str)
                    .ok_or_else(|| {
                        local_response(
                            hudsucker::hyper::StatusCode::BAD_GATEWAY,
                            "Nebula rejected an invalid header rule.",
                        )
                    })?;
                let name = hudsucker::hyper::header::HeaderName::from_bytes(name.as_bytes())
                    .map_err(|_| {
                        local_response(
                            hudsucker::hyper::StatusCode::BAD_GATEWAY,
                            "Nebula rejected an invalid header name.",
                        )
                    })?;
                request.headers_mut().remove(name);
            }
            "replace_body" => {
                let replacement = replacement_value(&rule.action).map_err(|_| {
                    local_response(
                        hudsucker::hyper::StatusCode::BAD_GATEWAY,
                        "Nebula rejected an invalid body replacement rule.",
                    )
                })?;
                let (mut parts, body) = request.into_parts();
                let original = collect_bounded(body).await.map_err(|_| {
                    local_response(
                        hudsucker::hyper::StatusCode::BAD_GATEWAY,
                        "Nebula could not read the bounded request body for mutation.",
                    )
                })?;
                if original.len() > MAX_PROXY_MUTATION_BYTES
                    || replace_body_bytes(&mut parts.headers, &replacement).is_err()
                {
                    return Err(local_response(
                        hudsucker::hyper::StatusCode::PAYLOAD_TOO_LARGE,
                        "Nebula refused an oversized or encoded request body mutation.",
                    ));
                }
                request = Request::from_parts(parts, Body::from(replacement));
            }
            "ws_drop" | "ws_replace" => {}
            _ => {
                return Err(local_response(
                    hudsucker::hyper::StatusCode::BAD_GATEWAY,
                    "Nebula rejected a proxy rule error.",
                ));
            }
        }
        matched_rules.push(rule.clone());
    }
    Ok((request, matched_rules))
}

impl CaptureHandler {
    fn blocked_request(
        &self,
        request_id: u64,
        request: &Request<Body>,
        detail: String,
    ) -> Response<Body> {
        let response = local_response(
            hudsucker::hyper::StatusCode::FORBIDDEN,
            "Nebula blocked this browser request because it is outside the active Project scope or proxy policy.",
        );
        let event = TrafficEvent {
            session_id: self.session_id.clone(),
            tab_id: self.tab_id.clone(),
            method: request.method().to_string(),
            url: scope_uri_for_request(request)
                .unwrap_or_else(|| request.uri().clone())
                .to_string(),
            protocol: protocol(request.version()),
            status_code: Some(response.status().as_u16()),
            request_headers: redacted_headers(request.headers()),
            response_headers: redacted_headers(response.headers()),
            request_bytes: request.body().size_hint().exact(),
            response_bytes: response.body().size_hint().exact(),
            duration_ms: 0,
            error: Some(detail),
            request_id,
            blocked: true,
            request_body: None,
            response_body: None,
        };
        if self.app.emit("nebula-browser-traffic", event).is_err() {
            record_proxy_failure(
                &self.app,
                "desktop.browser.proxy_block_delivery_failed",
                "A blocked browser request could not be delivered to the interface.",
                "scope-enforcement",
            );
        }
        response
    }
}

impl HttpHandler for CaptureHandler {
    async fn handle_request(
        &mut self,
        _context: &HttpContext,
        request: Request<Body>,
    ) -> RequestOrResponse {
        let request_id = self.next_request_id.fetch_add(1, Ordering::Relaxed);
        let original_method = request.method().to_string();
        let original_url = request.uri().to_string();
        let original_protocol = protocol(request.version());
        let original_headers = redacted_headers(request.headers());
        let original_bytes = request.body().size_hint().exact();
        let scope = snapshot_scope(&self.scope);
        let scope_uri = scope_uri_for_request(&request);
        if let Some(scope) = scope.as_ref() {
            if scope_uri
                .as_ref()
                .is_none_or(|uri| !scope_allows_uri(scope, uri))
            {
                return self
                    .blocked_request(
                        request_id,
                        &request,
                        format!(
                            "request rejected by Project scope revision {}",
                            scope.revision
                        ),
                    )
                    .into();
            }
        } else {
            return self
                .blocked_request(
                    request_id,
                    &request,
                    "no compiled Project scope is active for this browser session".to_string(),
                )
                .into();
        }
        if request.method() == hudsucker::hyper::Method::CONNECT {
            // Hudsucker invokes handle_request for the CONNECT envelope and
            // then performs the TLS interception itself. The inner HTTPS
            // requests are checked and captured by their own handler clones.
            return request.into();
        }
        if self.interception_enabled.load(Ordering::Relaxed) {
            let transaction_id = format!("{}-{}-{request_id}", self.session_id, self.tab_id);
            let (sender, receiver) = oneshot::channel();
            let registered = match self.pending_intercepts.lock() {
                Ok(mut pending) => {
                    pending.insert(transaction_id.clone(), sender);
                    true
                }
                Err(_) => false,
            };
            if !registered {
                return self
                    .blocked_request(
                        request_id,
                        &request,
                        "the native intercept registry is unavailable".to_string(),
                    )
                    .into();
            }
            let event = InterceptEvent {
                project_id: self.project_id.clone(),
                session_id: self.session_id.clone(),
                tab_id: self.tab_id.clone(),
                transaction_id: transaction_id.clone(),
                phase: "request",
                method: original_method.clone(),
                url: scope_uri.as_ref().unwrap_or(request.uri()).to_string(),
                headers: original_headers
                    .iter()
                    .map(|(name, value)| (name.clone(), value.clone()))
                    .collect(),
                status_code: None,
                timeout_seconds: 60,
            };
            if self.app.emit("nebula-browser-intercept", event).is_err() {
                if let Ok(mut pending) = self.pending_intercepts.lock() {
                    pending.remove(&transaction_id);
                }
                return self
                    .blocked_request(
                        request_id,
                        &request,
                        "the paused transaction could not be delivered to Core".to_string(),
                    )
                    .into();
            }
            match timeout(Duration::from_secs(60), receiver).await {
                Ok(Ok(NativeInterceptDecision::Forward)) => {}
                Ok(Ok(NativeInterceptDecision::Drop)) => {
                    return local_response(
                        hudsucker::hyper::StatusCode::FORBIDDEN,
                        "Nebula dropped this intercepted browser request.",
                    )
                    .into();
                }
                _ => {
                    if let Ok(mut pending) = self.pending_intercepts.lock() {
                        pending.remove(&transaction_id);
                    }
                    return self
                        .blocked_request(
                            request_id,
                            &request,
                            "the paused browser transaction expired without a decision".to_string(),
                        )
                        .into();
                }
            }
        }
        let rules = snapshot_rules(&self.rules);
        let (mut request, matched_rules) = match mutate_request(request, &rules).await {
            Ok(value) => value,
            Err(response) => {
                if let Err(error) = self.app.emit(
                    "nebula-browser-traffic",
                    TrafficEvent {
                        session_id: self.session_id.clone(),
                        tab_id: self.tab_id.clone(),
                        method: original_method,
                        url: original_url,
                        protocol: original_protocol,
                        status_code: Some(response.status().as_u16()),
                        request_headers: original_headers,
                        response_headers: redacted_headers(response.headers()),
                        request_bytes: original_bytes,
                        response_bytes: response.body().size_hint().exact(),
                        duration_ms: 0,
                        error: Some("request blocked or rejected by proxy rule".to_string()),
                        request_id,
                        blocked: true,
                        request_body: None,
                        response_body: None,
                    },
                ) {
                    eprintln!("Nebula could not emit a blocked browser traffic event: {error}");
                }
                return response.into();
            }
        };
        let request_body = if self.capture_bodies.load(Ordering::Relaxed) {
            let (parts, body) = request.into_parts();
            match capture_body(body, &parts.headers).await {
                Ok((body, capture)) => {
                    request = Request::from_parts(parts, body);
                    capture
                }
                Err(_) => {
                    let response = local_response(
                        hudsucker::hyper::StatusCode::PAYLOAD_TOO_LARGE,
                        "Nebula refused an oversized or unreadable request body capture.",
                    );
                    if let Err(error) = self.app.emit(
                        "nebula-browser-traffic",
                        TrafficEvent {
                            session_id: self.session_id.clone(),
                            tab_id: self.tab_id.clone(),
                            method: original_method,
                            url: original_url,
                            protocol: original_protocol,
                            status_code: Some(response.status().as_u16()),
                            request_headers: original_headers,
                            response_headers: redacted_headers(response.headers()),
                            request_bytes: original_bytes,
                            response_bytes: response.body().size_hint().exact(),
                            duration_ms: 0,
                            error: Some(
                                "request body capture rejected by the 1 MiB limit".to_string(),
                            ),
                            request_id,
                            blocked: true,
                            request_body: None,
                            response_body: None,
                        },
                    ) {
                        eprintln!("Nebula could not emit a rejected body-capture event: {error}");
                    }
                    return response.into();
                }
            }
        } else {
            None
        };
        if let Some(scope) = scope.as_ref() {
            if scope_uri_for_request(&request)
                .as_ref()
                .is_none_or(|uri| !scope_allows_uri(scope, uri))
            {
                return self
                    .blocked_request(
                        request_id,
                        &request,
                        format!(
                            "proxy mutation moved the request outside Project scope revision {}",
                            scope.revision
                        ),
                    )
                    .into();
            }
        }
        let pending = PendingRequest {
            request_id,
            method: request.method().to_string(),
            url: request.uri().to_string(),
            protocol: protocol(request.version()),
            request_headers: redacted_headers(request.headers()),
            request_bytes: request.body().size_hint().exact(),
            started: Instant::now(),
            rules: matched_rules,
            request_body,
        };
        match self.pending.get_mut() {
            Ok(map) => {
                map.insert(request_id, pending);
                if map.len() > 4_096 {
                    if let Some(oldest) = map.keys().min().copied() {
                        map.remove(&oldest);
                    }
                }
            }
            Err(_) => record_proxy_failure(
                &self.app,
                "desktop.browser.proxy_request_state_failed",
                "The browser capture proxy could not retain request state.",
                "request-capture",
            ),
        }
        request.into()
    }

    async fn handle_response(
        &mut self,
        _context: &HttpContext,
        response: Response<Body>,
    ) -> Response<Body> {
        let pending = match self.pending.get_mut() {
            Ok(map) => map.keys().min().copied().and_then(|id| map.remove(&id)),
            Err(_) => {
                record_proxy_failure(
                    &self.app,
                    "desktop.browser.proxy_response_state_failed",
                    "The browser capture proxy could not recover request state for a response.",
                    "response-capture",
                );
                None
            }
        };
        if let Some(request) = pending {
            let request_id = request.request_id;
            if self.interception_enabled.load(Ordering::Relaxed) {
                let transaction_id =
                    format!("{}-{}-{request_id}-response", self.session_id, self.tab_id);
                let (sender, receiver) = oneshot::channel();
                if let Ok(mut pending) = self.pending_intercepts.lock() {
                    pending.insert(transaction_id.clone(), sender);
                } else {
                    return local_response(
                        hudsucker::hyper::StatusCode::BAD_GATEWAY,
                        "Nebula failed closed because the response intercept registry is unavailable.",
                    );
                }
                let event = InterceptEvent {
                    project_id: self.project_id.clone(),
                    session_id: self.session_id.clone(),
                    tab_id: self.tab_id.clone(),
                    transaction_id: transaction_id.clone(),
                    phase: "response",
                    method: request.method.clone(),
                    url: request.url.clone(),
                    headers: redacted_headers(response.headers()).into_iter().collect(),
                    status_code: Some(response.status().as_u16()),
                    timeout_seconds: 60,
                };
                if self.app.emit("nebula-browser-intercept", event).is_err() {
                    if let Ok(mut pending) = self.pending_intercepts.lock() {
                        pending.remove(&transaction_id);
                    }
                    return local_response(
                        hudsucker::hyper::StatusCode::BAD_GATEWAY,
                        "Nebula failed closed because the paused response could not be persisted.",
                    );
                }
                match timeout(Duration::from_secs(60), receiver).await {
                    Ok(Ok(NativeInterceptDecision::Forward)) => {}
                    Ok(Ok(NativeInterceptDecision::Drop)) => {
                        return local_response(
                            hudsucker::hyper::StatusCode::FORBIDDEN,
                            "Nebula dropped this intercepted browser response.",
                        );
                    }
                    _ => {
                        if let Ok(mut pending) = self.pending_intercepts.lock() {
                            pending.remove(&transaction_id);
                        }
                        return local_response(
                            hudsucker::hyper::StatusCode::GATEWAY_TIMEOUT,
                            "Nebula failed closed because the paused response expired.",
                        );
                    }
                }
            }
            let response = match mutate_response(response, &request.rules).await {
                Ok(value) => value,
                Err(value) => value,
            };
            let (response, response_body, capture_error) =
                if self.capture_bodies.load(Ordering::Relaxed) {
                    let (parts, body) = response.into_parts();
                    match capture_body(body, &parts.headers).await {
                        Ok((body, capture)) => (Response::from_parts(parts, body), capture, None),
                        Err(_) => (
                            local_response(
                                hudsucker::hyper::StatusCode::PAYLOAD_TOO_LARGE,
                                "Nebula refused an oversized or unreadable response body capture.",
                            ),
                            None,
                            Some("response body capture rejected by the 1 MiB limit".to_string()),
                        ),
                    }
                } else {
                    (response, None, None)
                };
            let event = TrafficEvent {
                session_id: self.session_id.clone(),
                tab_id: self.tab_id.clone(),
                method: request.method,
                url: request.url,
                protocol: request.protocol,
                status_code: Some(response.status().as_u16()),
                request_headers: request.request_headers,
                response_headers: redacted_headers(response.headers()),
                request_bytes: request.request_bytes,
                response_bytes: response.body().size_hint().exact(),
                duration_ms: request
                    .started
                    .elapsed()
                    .as_millis()
                    .min(u128::from(u64::MAX)) as u64,
                error: capture_error,
                request_id,
                blocked: false,
                request_body: request.request_body,
                response_body,
            };
            if self.app.emit("nebula-browser-traffic", event).is_err() {
                record_proxy_failure(
                    &self.app,
                    "desktop.browser.proxy_traffic_delivery_failed",
                    "Captured browser traffic could not be delivered to the interface.",
                    "event-delivery",
                );
            }
            response
        } else {
            response
        }
    }

    async fn handle_error(
        &mut self,
        _context: &HttpContext,
        _error: hudsucker::hyper_util::client::legacy::Error,
    ) -> Response<Body> {
        let pending = self
            .pending
            .get_mut()
            .ok()
            .and_then(|map| map.keys().min().copied().and_then(|id| map.remove(&id)));
        let response = local_response(
            hudsucker::hyper::StatusCode::BAD_GATEWAY,
            "Nebula could not reach the in-scope upstream browser target.",
        );
        if let Some(request) = pending {
            if let Err(error) = self.app.emit(
                "nebula-browser-traffic",
                TrafficEvent {
                    session_id: self.session_id.clone(),
                    tab_id: self.tab_id.clone(),
                    method: request.method,
                    url: request.url,
                    protocol: request.protocol,
                    status_code: Some(response.status().as_u16()),
                    request_headers: request.request_headers,
                    response_headers: redacted_headers(response.headers()),
                    request_bytes: request.request_bytes,
                    response_bytes: response.body().size_hint().exact(),
                    duration_ms: request
                        .started
                        .elapsed()
                        .as_millis()
                        .min(u128::from(u64::MAX)) as u64,
                    error: Some("upstream request failed".to_string()),
                    request_id: request.request_id,
                    blocked: false,
                    request_body: request.request_body,
                    response_body: None,
                },
            ) {
                eprintln!("Nebula could not emit an upstream browser failure event: {error}");
            }
        }
        response
    }
}

impl WebSocketHandler for CaptureHandler {
    async fn handle_message(
        &mut self,
        context: &WebSocketContext,
        message: Message,
    ) -> Option<Message> {
        let target = match context {
            WebSocketContext::ClientToServer { dst, .. } => dst,
            WebSocketContext::ServerToClient { src, .. } => src,
        };
        let request_id = self.next_request_id.fetch_add(1, Ordering::Relaxed);
        let scope = snapshot_scope(&self.scope);
        if scope
            .as_ref()
            .is_none_or(|scope| !scope_allows_uri(scope, target))
        {
            let response = local_response(
                hudsucker::hyper::StatusCode::FORBIDDEN,
                "Nebula blocked this WebSocket because it is outside the active Project scope.",
            );
            if let Err(error) = self.app.emit(
                "nebula-browser-traffic",
                TrafficEvent {
                    session_id: self.session_id.clone(),
                    tab_id: self.tab_id.clone(),
                    method: "WEBSOCKET".to_string(),
                    url: target.to_string(),
                    protocol: "unknown",
                    status_code: Some(response.status().as_u16()),
                    request_headers: BTreeMap::new(),
                    response_headers: BTreeMap::new(),
                    request_bytes: None,
                    response_bytes: response.body().size_hint().exact(),
                    duration_ms: 0,
                    error: Some("WebSocket rejected by Project scope".to_string()),
                    request_id,
                    blocked: true,
                    request_body: None,
                    response_body: None,
                },
            ) {
                eprintln!("Nebula could not emit a blocked WebSocket event: {error}");
            }
            return None;
        }
        let (opcode, bytes): (&'static str, &[u8]) = match &message {
            Message::Text(text) => ("text", text.as_bytes()),
            Message::Binary(value) => ("binary", value.as_ref()),
            Message::Ping(value) => ("ping", value.as_ref()),
            Message::Pong(value) => ("pong", value.as_ref()),
            Message::Close(_) => ("close", &[]),
            _ => ("binary", &[]),
        };
        let direction = match context {
            WebSocketContext::ClientToServer { .. } => "client",
            WebSocketContext::ServerToClient { .. } => "server",
        };
        let url = match context {
            WebSocketContext::ClientToServer { dst, .. } => dst.to_string(),
            WebSocketContext::ServerToClient { src, .. } => src.to_string(),
        };
        let oversized = bytes.len() > MAX_PROXY_MUTATION_BYTES;
        let event = WebSocketFrameEvent {
            session_id: self.session_id.clone(),
            tab_id: self.tab_id.clone(),
            url,
            direction,
            opcode,
            // WebSocket frames are untrusted message data.  Keep only a digest
            // and bounded length in the default headers/metadata capture mode;
            // the native runtime has no body-artifact path to hold a preview.
            payload_preview: String::new(),
            payload_sha256: format!("{:x}", Sha256::digest(bytes)),
            payload_bytes: bytes.len(),
            truncated: true,
        };
        if self
            .app
            .emit("nebula-browser-websocket-frame", event)
            .is_err()
        {
            record_proxy_failure(
                &self.app,
                "desktop.browser.proxy_websocket_delivery_failed",
                "A captured browser WebSocket frame could not be delivered to the interface.",
                "event-delivery",
            );
        }
        if oversized {
            return None;
        }
        let rules = snapshot_rules(&self.rules);
        let Some(rule) = rules.iter().find(|rule| {
            let uri = match context {
                WebSocketContext::ClientToServer { dst, .. } => dst,
                WebSocketContext::ServerToClient { src, .. } => src,
            };
            rule_matches(rule, uri, "WEBSOCKET")
                && matches!(
                    rule.action.get("type").and_then(serde_json::Value::as_str),
                    Some("ws_drop") | Some("ws_replace")
                )
        }) else {
            return Some(message);
        };
        match rule.action.get("type").and_then(serde_json::Value::as_str) {
            Some("ws_drop") => None,
            Some("ws_replace") => replacement_value(&rule.action).ok().map(|value| {
                if matches!(message, Message::Binary(_)) {
                    Message::Binary(value.into())
                } else {
                    Message::Text(String::from_utf8_lossy(&value).to_string().into())
                }
            }),
            _ => Some(message),
        }
    }
}

#[allow(clippy::result_large_err)]
async fn mutate_response(
    response: Response<Body>,
    rules: &[NativeProxyRule],
) -> Result<Response<Body>, Response<Body>> {
    let mut response = response;
    for rule in rules {
        if rule.action.get("type").and_then(serde_json::Value::as_str) != Some("replace_body") {
            continue;
        }
        let replacement = match replacement_value(&rule.action) {
            Ok(value) => value,
            Err(_) => {
                return Err(local_response(
                    hudsucker::hyper::StatusCode::BAD_GATEWAY,
                    "Nebula rejected an invalid response body replacement rule.",
                ));
            }
        };
        let (mut parts, body) = response.into_parts();
        let original = match collect_bounded(body).await {
            Ok(value) => value,
            Err(_) => {
                return Err(local_response(
                    hudsucker::hyper::StatusCode::BAD_GATEWAY,
                    "Nebula could not read the bounded response body for mutation.",
                ));
            }
        };
        if original.len() > MAX_PROXY_MUTATION_BYTES
            || replace_body_bytes(&mut parts.headers, &replacement).is_err()
        {
            return Err(local_response(
                hudsucker::hyper::StatusCode::PAYLOAD_TOO_LARGE,
                "Nebula refused an oversized or encoded response body mutation.",
            ));
        }
        response = Response::from_parts(parts, Body::from(replacement));
    }
    Ok(response)
}

fn ca_directory(app: &AppHandle, project_key: &str) -> Result<PathBuf, String> {
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate browser CA storage: {error}"))?
        .join("browser-ca")
        .join(project_key))
}

fn atomic_write(path: &Path, value: &str, private: bool) -> Result<(), String> {
    let suffix = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = path.with_file_name(format!(
        "{}.tmp-{}-{}",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("nebula-ca"),
        std::process::id(),
        suffix
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|error| format!("cannot create atomic browser CA file: {error}"))?;
    if let Err(error) = file
        .write_all(value.as_bytes())
        .and_then(|_| file.sync_all())
    {
        let _ = fs::remove_file(&temporary); // diagnostic-expected: the primary write error is returned below
        return Err(format!("cannot write browser CA file: {error}"));
    }
    if private {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))
                .map_err(|error| format!("cannot protect browser CA key: {error}"))?;
        }
    }
    fs::rename(&temporary, path)
        .map_err(|error| format!("cannot publish browser CA file: {error}"))?;
    Ok(())
}

fn write_private(path: &Path, value: &str) -> Result<(), String> {
    atomic_write(path, value, true)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("cannot protect browser CA key: {error}"))?;
    }
    Ok(())
}

fn ca_fingerprint(certificate: &str) -> String {
    let encoded = certificate
        .lines()
        .filter(|line| !line.starts_with("---"))
        .collect::<String>();
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded)
        .unwrap_or_else(|_| certificate.as_bytes().to_vec());
    format!("{:x}", Sha256::digest(bytes))
}

fn ca_lock_path(directory: &Path) -> PathBuf {
    directory.join("nebula-browser-ca.lock")
}

fn with_ca_lock<T>(
    directory: &Path,
    operation: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};

        let lock_path = ca_lock_path(directory);
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(&lock_path)
            .map_err(|error| format!("cannot open browser CA lock: {error}"))?;
        fs::set_permissions(&lock_path, fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("cannot protect browser CA lock: {error}"))?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(format!(
                "cannot acquire browser CA lock: {}",
                std::io::Error::last_os_error()
            ));
        }
        let result = operation();
        let unlock_result = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_UN) };
        if unlock_result != 0 && result.is_ok() {
            return Err(format!(
                "cannot release browser CA lock: {}",
                std::io::Error::last_os_error()
            ));
        }
        result
    }

    #[cfg(not(unix))]
    {
        let lock_path = ca_lock_path(directory);
        let mut lock = None;
        for _ in 0..200 {
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&lock_path)
            {
                Ok(file) => {
                    lock = Some(file);
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    if fs::metadata(&lock_path)
                        .and_then(|metadata| metadata.modified())
                        .ok()
                        .and_then(|modified| modified.elapsed().ok())
                        .is_some_and(|age| age > Duration::from_secs(30))
                    {
                        // A crashed creator must not permanently prevent a later
                        // desktop startup from recovering the Project CA.
                        let _ = fs::remove_file(&lock_path); // diagnostic-expected: a raced stale lock is retried
                    }
                    thread::sleep(Duration::from_millis(10));
                }
                Err(error) => return Err(format!("cannot lock browser CA storage: {error}")),
            }
        }
        if lock.is_none() {
            return Err("timed out waiting for browser CA storage lock".to_string());
        }
        let result = operation();
        drop(lock);
        let _ = fs::remove_file(lock_path); // diagnostic-expected: dropping the lock is authoritative
        result
    }
}

#[cfg(unix)]
fn private_key_is_protected(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;

    fs::metadata(path)
        .map(|metadata| metadata.permissions().mode() & 0o077 == 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn private_key_is_protected(_path: &Path) -> bool {
    // The platform key store/ACL is not represented by Unix mode bits.  The
    // file is still created with the platform's private default permissions.
    true
}

fn valid_ca_pair(certificate: &str, key: &str) -> bool {
    let Ok(key) = KeyPair::from_pem(key) else {
        return false;
    };
    Issuer::from_ca_cert_pem(certificate, key).is_ok()
}

fn valid_ca_metadata(path: &Path, certificate: &str) -> bool {
    let Ok(value) = fs::read_to_string(path) else {
        return false;
    };
    let Ok(metadata) = serde_json::from_str::<CaMetadata>(&value) else {
        return false;
    };
    let Ok(expires_at) = time::OffsetDateTime::parse(
        &metadata.expires_at,
        &time::format_description::well_known::Rfc3339,
    ) else {
        return false;
    };
    let Ok(generated_at) = time::OffsetDateTime::parse(
        &metadata.generated_at,
        &time::format_description::well_known::Rfc3339,
    ) else {
        return false;
    };
    metadata.fingerprint == ca_fingerprint(certificate)
        && generated_at <= time::OffsetDateTime::now_utc()
        && expires_at > time::OffsetDateTime::now_utc()
}

fn ensure_ca(app: &AppHandle, project_key: &str) -> Result<(String, String, PathBuf), String> {
    let directory = ca_directory(app, project_key)?;
    fs::create_dir_all(&directory)
        .map_err(|error| format!("cannot prepare browser CA storage: {error}"))?;
    with_ca_lock(&directory, || {
        let certificate_path = directory.join(CA_CERTIFICATE);
        let key_path = directory.join(CA_PRIVATE_KEY);
        let metadata_path = directory.join(CA_METADATA);
        let existing = fs::read_to_string(&certificate_path).ok(); // diagnostic-expected: absence triggers atomic CA creation
        let existing_key = fs::read_to_string(&key_path).ok(); // diagnostic-expected: absence triggers atomic CA creation
        if !matches!((&existing, &existing_key), (Some(certificate), Some(key)) if valid_ca_pair(certificate, key) && valid_ca_metadata(&metadata_path, certificate) && private_key_is_protected(&key_path))
        {
            let mut parameters = CertificateParams::new(Vec::<String>::new())
                .map_err(|error| format!("cannot create browser CA parameters: {error}"))?;
            parameters.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
            parameters
                .distinguished_name
                .push(DnType::CommonName, "Nebula Project Browser CA");
            parameters.key_usages = vec![
                KeyUsagePurpose::DigitalSignature,
                KeyUsagePurpose::KeyCertSign,
                KeyUsagePurpose::CrlSign,
            ];
            let now = time::OffsetDateTime::now_utc();
            parameters.not_before = now - time::Duration::minutes(5);
            parameters.not_after = now + time::Duration::days(365);
            let key = KeyPair::generate()
                .map_err(|error| format!("cannot generate browser CA key: {error}"))?;
            let certificate = parameters
                .self_signed(&key)
                .map_err(|error| format!("cannot sign browser CA certificate: {error}"))?;
            let certificate_pem = certificate.pem();
            let fingerprint = ca_fingerprint(&certificate_pem);
            let metadata = CaMetadata {
                generated_at: now
                    .format(&time::format_description::well_known::Rfc3339)
                    .map_err(|error| format!("cannot format browser CA creation time: {error}"))?,
                expires_at: parameters
                    .not_after
                    .format(&time::format_description::well_known::Rfc3339)
                    .map_err(|error| format!("cannot format browser CA expiry: {error}"))?,
                fingerprint,
            };
            atomic_write(&certificate_path, &certificate_pem, false)?;
            write_private(&key_path, &key.serialize_pem())?;
            atomic_write(
                &metadata_path,
                &serde_json::to_string_pretty(&metadata)
                    .map_err(|error| format!("cannot encode browser CA metadata: {error}"))?,
                false,
            )?;
        }
        let certificate = fs::read_to_string(&certificate_path)
            .map_err(|error| format!("cannot read browser CA certificate: {error}"))?;
        let key = fs::read_to_string(&key_path)
            .map_err(|error| format!("cannot read browser CA key: {error}"))?;
        if !valid_ca_pair(&certificate, &key) {
            return Err("the Project browser CA certificate and key do not match".to_string());
        }
        Ok((certificate, key, certificate_path))
    })
}

pub(crate) fn start(
    app: &AppHandle,
    project_key: &str,
    project_id: &str,
    session_id: &str,
    tab_id: &str,
    upstream: Option<NativeUpstreamProxyConfig>,
    capture_bodies: bool,
    interception_enabled: bool,
) -> Result<BrowserProxyHandle, String> {
    let (certificate, key, _) = ensure_ca(app, project_key)?;
    let key =
        KeyPair::from_pem(&key).map_err(|error| format!("cannot parse browser CA key: {error}"))?;
    let issuer = Issuer::from_ca_cert_pem(&certificate, key)
        .map_err(|error| format!("cannot parse browser CA certificate: {error}"))?;
    let authority = RcgenAuthority::new(issuer, 1_000, aws_lc_rs::default_provider());
    let connector = DynamicConnector::new(upstream)?;
    let capture_bodies = Arc::new(std::sync::atomic::AtomicBool::new(capture_bodies));
    let interception_enabled = Arc::new(std::sync::atomic::AtomicBool::new(interception_enabled));
    let pending_intercepts = Arc::new(Mutex::new(HashMap::new()));
    let listener = tauri::async_runtime::block_on(TcpListener::bind(("127.0.0.1", 0)))
        .map_err(|error| format!("cannot bind the local browser proxy: {error}"))?;
    let address = listener
        .local_addr()
        .map_err(|error| format!("cannot read the local browser proxy address: {error}"))?;
    let handler = CaptureHandler {
        app: app.clone(),
        project_id: project_id.to_string(),
        session_id: session_id.to_string(),
        tab_id: tab_id.to_string(),
        pending: Mutex::new(HashMap::new()),
        next_request_id: Arc::new(AtomicU64::new(1)),
        rules: Arc::new(Mutex::new(Vec::new())),
        scope: Arc::new(Mutex::new(None)),
        capture_bodies: capture_bodies.clone(),
        interception_enabled: interception_enabled.clone(),
        pending_intercepts: pending_intercepts.clone(),
    };
    let rules = handler.rules.clone();
    let scope = handler.scope.clone();
    let (shutdown_tx, shutdown_rx) = oneshot::channel();
    let websocket_tls = proxy_tls_config()?;
    let proxy = Proxy::builder()
        .with_listener(listener)
        .with_ca(authority)
        .with_http_connector(connector.clone())
        .with_websocket_connector(hudsucker::tokio_tungstenite::Connector::Rustls(Arc::new(
            websocket_tls,
        )))
        .with_http_handler(handler.clone())
        .with_websocket_handler(handler)
        .with_graceful_shutdown(async move {
            let _ = shutdown_rx.await; // diagnostic-expected: dropping the handle also ends the proxy
        })
        .build()
        .map_err(|error| format!("cannot configure the local browser proxy: {error}"))?;
    let proxy_app = app.clone();
    tauri::async_runtime::spawn(async move {
        if proxy.start().await.is_err() {
            record_proxy_failure(
                &proxy_app,
                "desktop.browser.proxy_runtime_failed",
                "The browser capture proxy stopped unexpectedly.",
                "proxy-runtime",
            );
        }
    });
    Ok(BrowserProxyHandle {
        url: tauri::Url::parse(&format!("http://{address}"))
            .map_err(|error| format!("cannot prepare browser proxy URL: {error}"))?,
        shutdown: Arc::new(Mutex::new(Some(shutdown_tx))),
        rules,
        scope,
        connector,
        capture_bodies,
        interception_enabled,
        pending_intercepts,
    })
}

pub(crate) fn reveal_ca(app: &AppHandle, project_key: &str) -> Result<PathBuf, String> {
    let (_, _, path) = ensure_ca(app, project_key)?;
    app.opener()
        .reveal_item_in_dir(&path)
        .map_err(|error| format!("cannot reveal the browser CA certificate: {error}"))?;
    Ok(path)
}

pub(crate) fn ca_status(app: &AppHandle, project_key: &str) -> Result<BrowserCaStatus, String> {
    let (_, _, certificate_path) = ensure_ca(app, project_key)?;
    let metadata_path = certificate_path
        .parent()
        .ok_or_else(|| "browser CA storage has no parent directory".to_string())?
        .join(CA_METADATA);
    let certificate = fs::read_to_string(&certificate_path)
        .map_err(|error| format!("cannot read browser CA certificate: {error}"))?;
    let metadata = fs::read_to_string(metadata_path)
        .ok()
        .and_then(|value| serde_json::from_str::<CaMetadata>(&value).ok());
    Ok(BrowserCaStatus {
        certificate_path: certificate_path.to_string_lossy().to_string(),
        fingerprint: metadata
            .as_ref()
            .map(|value| value.fingerprint.clone())
            .unwrap_or_else(|| ca_fingerprint(&certificate)),
        generated_at: metadata.as_ref().map(|value| value.generated_at.clone()),
        expires_at: metadata.as_ref().map(|value| value.expires_at.clone()),
        state: "generated",
        trust_instructions: if cfg!(target_os = "macos") {
            "Open the certificate in Keychain Access, add it to the login keychain, and explicitly set trust for SSL before enabling capture.".to_string()
        } else if cfg!(target_os = "windows") {
            "Import the certificate into Current User → Trusted Root Certification Authorities, then explicitly confirm trust before enabling capture.".to_string()
        } else {
            "Import the certificate into the desktop/browser trusted CA store (for example, the system CA manager or Chromium Authorities), then explicitly confirm trust before enabling capture.".to_string()
        },
    })
}

pub(crate) fn rotate_ca(app: &AppHandle, project_key: &str) -> Result<BrowserCaStatus, String> {
    let directory = ca_directory(app, project_key)?;
    fs::create_dir_all(&directory)
        .map_err(|error| format!("cannot prepare browser CA storage: {error}"))?;
    with_ca_lock(&directory, || {
        for name in [CA_CERTIFICATE, CA_PRIVATE_KEY, CA_METADATA] {
            let path = directory.join(name);
            if path.exists() {
                fs::remove_file(path)
                    .map_err(|error| format!("cannot rotate browser CA material: {error}"))?;
            }
        }
        Ok(())
    })?;
    ca_status(app, project_key)
}

pub(crate) fn revoke_ca(app: &AppHandle, project_key: &str) -> Result<(), String> {
    let directory = ca_directory(app, project_key)?;
    if !directory.exists() {
        return Ok(());
    }
    with_ca_lock(&directory, || {
        for name in [CA_CERTIFICATE, CA_PRIVATE_KEY, CA_METADATA] {
            let path = directory.join(name);
            if path.exists() {
                fs::remove_file(path)
                    .map_err(|error| format!("cannot revoke browser CA material: {error}"))?;
            }
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use http_body_util::{BodyExt, Full};
    use hudsucker::hyper::{HeaderMap, Request, Version, body::Bytes, header::HeaderValue};
    use hudsucker::hyper_util::{client::legacy::Client, rt::TokioExecutor};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
    };

    async fn read_headers(stream: &mut TcpStream) -> Vec<u8> {
        let mut request = Vec::new();
        let mut byte = [0_u8; 1];
        while request.len() < 16_384 && !request.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).await.expect("proxy request");
            request.push(byte[0]);
        }
        request
    }

    async fn target_server() -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("target listener");
        let address = listener.local_addr().expect("target address");
        let task = tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.expect("target connection");
            let _ = read_headers(&mut stream).await; // diagnostic-expected: the test server only drains the bounded request preface
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
                .await
                .expect("target response");
        });
        (address, task)
    }

    async fn http_connect_proxy() -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("HTTP proxy listener");
        let address = listener.local_addr().expect("HTTP proxy address");
        let task = tokio::spawn(async move {
            let (mut client, _) = listener.accept().await.expect("HTTP proxy connection");
            let request = read_headers(&mut client).await;
            let first_line = request
                .split(|byte| *byte == b'\n')
                .next()
                .and_then(|line| std::str::from_utf8(line).ok())
                .unwrap_or_default()
                .trim();
            let authority = first_line
                .strip_prefix("CONNECT ")
                .and_then(|value| value.strip_suffix(" HTTP/1.1"))
                .expect("CONNECT authority");
            let mut target = TcpStream::connect(authority)
                .await
                .expect("HTTP proxy target");
            client
                .write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                .await
                .expect("HTTP proxy response");
            tokio::io::copy_bidirectional(&mut client, &mut target)
                .await
                .expect("HTTP proxy tunnel");
        });
        (address, task)
    }

    async fn socks5_proxy() -> (std::net::SocketAddr, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind(("127.0.0.1", 0))
            .await
            .expect("SOCKS5 listener");
        let address = listener.local_addr().expect("SOCKS5 address");
        let task = tokio::spawn(async move {
            let (mut client, _) = listener.accept().await.expect("SOCKS5 connection");
            let mut greeting = [0_u8; 2];
            client
                .read_exact(&mut greeting)
                .await
                .expect("SOCKS5 greeting");
            let mut methods = vec![0_u8; usize::from(greeting[1])];
            client
                .read_exact(&mut methods)
                .await
                .expect("SOCKS5 methods");
            assert!(methods.contains(&0));
            client
                .write_all(&[5, 0])
                .await
                .expect("SOCKS5 method response");

            let mut header = [0_u8; 4];
            client
                .read_exact(&mut header)
                .await
                .expect("SOCKS5 request");
            assert_eq!(header, [5, 1, 0, 1]);
            let mut host = [0_u8; 4];
            client
                .read_exact(&mut host)
                .await
                .expect("SOCKS5 IPv4 host");
            let mut port = [0_u8; 2];
            client.read_exact(&mut port).await.expect("SOCKS5 port");
            let target_address = std::net::SocketAddr::from((host, u16::from_be_bytes(port)));
            let mut target = TcpStream::connect(target_address)
                .await
                .expect("SOCKS5 target");
            client
                .write_all(&[5, 0, 0, 1, 0, 0, 0, 0, 0, 0])
                .await
                .expect("SOCKS5 response");
            tokio::io::copy_bidirectional(&mut client, &mut target)
                .await
                .expect("SOCKS5 tunnel");
        });
        (address, task)
    }

    async fn request_through(
        proxy_url: String,
        target: std::net::SocketAddr,
    ) -> hudsucker::hyper::Response<hudsucker::hyper::body::Incoming> {
        let connector = DynamicConnector::new(Some(NativeUpstreamProxyConfig {
            url: proxy_url,
            credential: None,
        }))
        .expect("native upstream connector");
        let client: Client<DynamicConnector, Full<Bytes>> =
            Client::builder(TokioExecutor::new()).build(connector);
        client
            .request(
                Request::builder()
                    .uri(format!("http://{target}/"))
                    .body(Full::new(Bytes::new()))
                    .expect("request"),
            )
            .await
            .expect("proxied request")
    }

    #[test]
    fn browser_proxy_redacts_reusable_secrets_but_keeps_comparable_hashes() {
        let mut headers = HeaderMap::new();
        headers.insert("authorization", HeaderValue::from_static("Bearer secret"));
        headers.insert("set-cookie", HeaderValue::from_static("sid=secret"));
        headers.insert("content-type", HeaderValue::from_static("application/json"));
        let redacted = redacted_headers(&headers);
        assert!(redacted["authorization"].starts_with("<redacted:sha256:"));
        assert!(redacted["set-cookie"].starts_with("<redacted:sha256:"));
        assert_eq!(redacted["content-type"], "application/json");
        assert!(!format!("{redacted:?}").contains("Bearer secret"));
        assert!(!format!("{redacted:?}").contains("sid=secret"));
    }

    #[test]
    fn browser_proxy_reports_http2_without_flattening_it_to_http1() {
        assert_eq!(protocol(Version::HTTP_2), "h2");
        assert_eq!(protocol(Version::HTTP_11), "http/1.1");
    }

    #[tokio::test]
    async fn native_intercepts_are_single_use_and_deliver_the_operator_decision() {
        let pending = Arc::new(Mutex::new(HashMap::new()));
        let (sender, receiver) = oneshot::channel();
        pending
            .lock()
            .unwrap()
            .insert("transaction-1".to_string(), sender);

        resolve_pending_intercept(&pending, "transaction-1", NativeInterceptDecision::Drop)
            .expect("decision resolves");
        assert!(matches!(receiver.await, Ok(NativeInterceptDecision::Drop)));
        assert!(
            resolve_pending_intercept(&pending, "transaction-1", NativeInterceptDecision::Forward,)
                .is_err()
        );
    }

    #[test]
    fn native_scope_matches_domains_cidrs_paths_and_ports_fail_closed() {
        let scope = compile_scope(NativeProxyScopeInput {
            revision: 7,
            allowed_cidrs: vec!["192.0.2.0/24".to_string()],
            allowed_domains: vec!["*.example.test".to_string()],
            allowed_urls: vec!["https://specific.test/app".to_string()],
            allowed_ports: vec![443],
            allow_all_targets: false,
            not_before: None,
            not_after: None,
        })
        .expect("scope compiles");
        assert!(scope_allows_uri(
            &scope,
            &"https://api.example.test/v1".parse().unwrap()
        ));
        assert!(scope_allows_uri(
            &scope,
            &"https://192.0.2.42/login".parse().unwrap()
        ));
        assert!(scope_allows_uri(
            &scope,
            &"https://specific.test/app/health".parse().unwrap()
        ));
        assert!(!scope_allows_uri(
            &scope,
            &"https://example.test/v1".parse().unwrap()
        ));
        assert!(!scope_allows_uri(
            &scope,
            &"https://api.example.test:8443/v1".parse().unwrap()
        ));
    }

    #[test]
    fn native_scope_rejects_credential_bearing_urls() {
        assert!(compile_scope_url("https://user:pass@example.test/").is_err());
        assert!(
            compile_scope(NativeProxyScopeInput {
                revision: 1,
                allowed_cidrs: vec![],
                allowed_domains: vec!["*.example.test".to_string()],
                allowed_urls: vec![],
                allowed_ports: vec![],
                allow_all_targets: false,
                not_before: None,
                not_after: None,
            })
            .is_ok()
        );
    }

    #[test]
    fn upstream_connectors_require_safe_explicit_proxy_endpoints() {
        assert!(upstream_uri("http://proxy.example.test:8080/").is_ok());
        assert!(upstream_uri("socks5://proxy.example.test:1080").is_ok());
        assert!(upstream_uri("http://user:pass@proxy.example.test:8080").is_err());
        assert!(upstream_uri("http://proxy.example.test:8080/path").is_err());
        assert!(credential_pair(Some("operator:secret")).is_ok());
        assert!(credential_pair(Some("operator")).is_err());
    }

    #[test]
    fn proxy_connector_adds_http_default_port_for_connect_tunnels() {
        let uri = with_default_http_port("http://target.example.test/path".parse().unwrap());
        assert_eq!(uri.port_u16(), Some(80));
        assert_eq!(uri.path(), "/path");
        let secure = with_default_http_port("https://target.example.test/path".parse().unwrap());
        assert_eq!(secure.port_u16(), None);
        let ipv6 = with_default_http_port("http://[::1]/path".parse().unwrap());
        assert_eq!(ipv6.port_u16(), Some(80));
    }

    #[tokio::test]
    async fn http_connect_upstream_forwards_a_real_request() {
        let (target, target_task) = target_server().await;
        let (proxy, proxy_task) = http_connect_proxy().await;
        let response = request_through(format!("http://{proxy}"), target).await;
        assert_eq!(response.status(), hudsucker::hyper::StatusCode::OK);
        assert_eq!(
            response
                .into_body()
                .collect()
                .await
                .expect("body")
                .to_bytes(),
            "ok"
        );
        target_task.await.expect("target task");
        proxy_task.await.expect("proxy task");
    }

    #[tokio::test]
    async fn socks5_upstream_forwards_a_real_request_without_local_dns() {
        let (target, target_task) = target_server().await;
        let (proxy, proxy_task) = socks5_proxy().await;
        let response = request_through(format!("socks5://{proxy}"), target).await;
        assert_eq!(response.status(), hudsucker::hyper::StatusCode::OK);
        assert_eq!(
            response
                .into_body()
                .collect()
                .await
                .expect("body")
                .to_bytes(),
            "ok"
        );
        target_task.await.expect("target task");
        proxy_task.await.expect("proxy task");
    }
}
