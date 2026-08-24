use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
    time::Instant,
};

use hudsucker::{
    Body, HttpContext, HttpHandler, Proxy, RequestOrResponse, WebSocketContext, WebSocketHandler,
    certificate_authority::RcgenAuthority,
    hyper::{Request, Response, body::Body as _},
    rcgen::{BasicConstraints, CertificateParams, DnType, IsCa, Issuer, KeyPair, KeyUsagePurpose},
    rustls::crypto::aws_lc_rs,
    tokio_tungstenite::tungstenite::Message,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_opener::OpenerExt;
use tokio::{net::TcpListener, sync::oneshot};

const CA_CERTIFICATE: &str = "nebula-browser-ca.pem";
const CA_PRIVATE_KEY: &str = "nebula-browser-ca-key.pem";

pub(crate) struct BrowserProxyHandle {
    pub(crate) url: tauri::Url,
    shutdown: Option<oneshot::Sender<()>>,
}

impl BrowserProxyHandle {
    pub(crate) fn stop(mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
    }
}

impl Drop for BrowserProxyHandle {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
    }
}

#[derive(Clone)]
struct PendingRequest {
    method: String,
    url: String,
    protocol: &'static str,
    request_headers: BTreeMap<String, String>,
    request_bytes: Option<u64>,
    started: Instant,
}

struct CaptureHandler {
    app: AppHandle,
    session_id: String,
    tab_id: String,
    pending: Mutex<Option<PendingRequest>>,
}

impl Clone for CaptureHandler {
    fn clone(&self) -> Self {
        Self {
            app: self.app.clone(),
            session_id: self.session_id.clone(),
            tab_id: self.tab_id.clone(),
            // Hudsucker clones one handler for a request/response pair. Never
            // share a pending request across concurrent connections.
            pending: Mutex::new(None),
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

impl HttpHandler for CaptureHandler {
    async fn handle_request(
        &mut self,
        _context: &HttpContext,
        request: Request<Body>,
    ) -> RequestOrResponse {
        let pending = PendingRequest {
            method: request.method().to_string(),
            url: request.uri().to_string(),
            protocol: protocol(request.version()),
            request_headers: redacted_headers(request.headers()),
            request_bytes: request.body().size_hint().exact(),
            started: Instant::now(),
        };
        if let Ok(slot) = self.pending.get_mut() {
            *slot = Some(pending);
        }
        request.into()
    }

    async fn handle_response(
        &mut self,
        _context: &HttpContext,
        response: Response<Body>,
    ) -> Response<Body> {
        let pending = self.pending.get_mut().ok().and_then(Option::take);
        if let Some(request) = pending {
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
                error: None,
            };
            let _ = self.app.emit("nebula-browser-traffic", event);
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
        let preview_bytes = &bytes[..bytes.len().min(2_000)];
        let event = WebSocketFrameEvent {
            session_id: self.session_id.clone(),
            tab_id: self.tab_id.clone(),
            url,
            direction,
            opcode,
            payload_preview: String::from_utf8_lossy(preview_bytes).to_string(),
            payload_sha256: format!("{:x}", Sha256::digest(bytes)),
            payload_bytes: bytes.len(),
            truncated: bytes.len() > preview_bytes.len(),
        };
        let _ = self.app.emit("nebula-browser-websocket-frame", event);
        Some(message)
    }
}

fn ca_directory(app: &AppHandle, project_key: &str) -> Result<PathBuf, String> {
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate browser CA storage: {error}"))?
        .join("browser-ca")
        .join(project_key))
}

fn write_private(path: &Path, value: &str) -> Result<(), String> {
    fs::write(path, value).map_err(|error| format!("cannot write browser CA key: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("cannot protect browser CA key: {error}"))?;
    }
    Ok(())
}

fn ensure_ca(app: &AppHandle, project_key: &str) -> Result<(String, String, PathBuf), String> {
    let directory = ca_directory(app, project_key)?;
    fs::create_dir_all(&directory)
        .map_err(|error| format!("cannot prepare browser CA storage: {error}"))?;
    let certificate_path = directory.join(CA_CERTIFICATE);
    let key_path = directory.join(CA_PRIVATE_KEY);
    if !certificate_path.is_file() || !key_path.is_file() {
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
        let key = KeyPair::generate()
            .map_err(|error| format!("cannot generate browser CA key: {error}"))?;
        let certificate = parameters
            .self_signed(&key)
            .map_err(|error| format!("cannot sign browser CA certificate: {error}"))?;
        fs::write(&certificate_path, certificate.pem())
            .map_err(|error| format!("cannot write browser CA certificate: {error}"))?;
        write_private(&key_path, &key.serialize_pem())?;
    }
    let certificate = fs::read_to_string(&certificate_path)
        .map_err(|error| format!("cannot read browser CA certificate: {error}"))?;
    let key = fs::read_to_string(&key_path)
        .map_err(|error| format!("cannot read browser CA key: {error}"))?;
    Ok((certificate, key, certificate_path))
}

pub(crate) fn start(
    app: &AppHandle,
    project_key: &str,
    session_id: &str,
    tab_id: &str,
) -> Result<BrowserProxyHandle, String> {
    let (certificate, key, _) = ensure_ca(app, project_key)?;
    let key =
        KeyPair::from_pem(&key).map_err(|error| format!("cannot parse browser CA key: {error}"))?;
    let issuer = Issuer::from_ca_cert_pem(&certificate, key)
        .map_err(|error| format!("cannot parse browser CA certificate: {error}"))?;
    let authority = RcgenAuthority::new(issuer, 1_000, aws_lc_rs::default_provider());
    let listener = tauri::async_runtime::block_on(TcpListener::bind(("127.0.0.1", 0)))
        .map_err(|error| format!("cannot bind the local browser proxy: {error}"))?;
    let address = listener
        .local_addr()
        .map_err(|error| format!("cannot read the local browser proxy address: {error}"))?;
    let handler = CaptureHandler {
        app: app.clone(),
        session_id: session_id.to_string(),
        tab_id: tab_id.to_string(),
        pending: Mutex::new(None),
    };
    let (shutdown_tx, shutdown_rx) = oneshot::channel();
    let proxy = Proxy::builder()
        .with_listener(listener)
        .with_ca(authority)
        .with_rustls_connector(aws_lc_rs::default_provider())
        .with_http_handler(handler.clone())
        .with_websocket_handler(handler)
        .with_graceful_shutdown(async move {
            let _ = shutdown_rx.await;
        })
        .build()
        .map_err(|error| format!("cannot configure the local browser proxy: {error}"))?;
    tauri::async_runtime::spawn(async move {
        let _ = proxy.start().await;
    });
    Ok(BrowserProxyHandle {
        url: tauri::Url::parse(&format!("http://{address}"))
            .map_err(|error| format!("cannot prepare browser proxy URL: {error}"))?,
        shutdown: Some(shutdown_tx),
    })
}

pub(crate) fn reveal_ca(app: &AppHandle, project_key: &str) -> Result<PathBuf, String> {
    let (_, _, path) = ensure_ca(app, project_key)?;
    app.opener()
        .reveal_item_in_dir(&path)
        .map_err(|error| format!("cannot reveal the browser CA certificate: {error}"))?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use hudsucker::hyper::{HeaderMap, Version, header::HeaderValue};

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
}
