use std::{
    collections::HashMap,
    fs::{self, File},
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpStream},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use getrandom::fill as random_fill;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use tauri::{
    AppHandle, Emitter, LogicalPosition, LogicalSize, Manager, State, Url, WebviewUrl,
    webview::{DownloadEvent, NewWindowResponse, PageLoadEvent, WebviewBuilder},
};

#[cfg(target_os = "macos")]
use objc2::{MainThreadMarker, MainThreadOnly, rc::Retained};
#[cfg(target_os = "macos")]
use objc2_app_kit::NSView;
#[cfg(target_os = "macos")]
use objc2_foundation::{NSPoint, NSRect, NSSize};

use crate::{
    diagnostics::{DiagnosticLevel, DiagnosticsState},
    sidecar::BackendState,
};

const MAX_TABS_PER_PROJECT: usize = 16;
const MAX_DOWNLOAD_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_RESPONSE_BYTES: u64 = 1024 * 1024;
const MAX_CAPTURE_TEXT_CHARS: usize = 16_000;
const MAX_CAPTURE_SELECTION_CHARS: usize = 4_000;
const MAX_CAPTURE_FORMS: usize = 40;
const MAX_CAPTURE_LINKS: usize = 80;
const MAX_CAPTURE_RAW_BYTES: usize = 2 * 1024 * 1024;

const BROWSER_CONTEXT_SCRIPT: &str = r#"(() => {
  const text = String(document.body?.innerText ?? "");
  const selectedText = String(window.getSelection?.()?.toString() ?? "");
  const forms = Array.from(document.forms ?? []).slice(0, 40).map((form) => ({
    method: String(form.method || "GET").toUpperCase().slice(0, 16),
    action: String(form.action || location.href).slice(0, 2048),
    fields: Array.from(form.elements ?? []).slice(0, 40).map((element) => ({
      name: String(element.getAttribute?.("name") ?? "").slice(0, 200),
      id: String(element.id ?? "").slice(0, 200),
      type: String(element.getAttribute?.("type") ?? element.tagName ?? "").toLowerCase().slice(0, 40),
      autocomplete: String(element.getAttribute?.("autocomplete") ?? "").slice(0, 100),
      required: Boolean(element.required),
    })),
  }));
  const links = Array.from(document.links ?? []).slice(0, 80).map((link) => ({
    text: String(link.innerText ?? link.textContent ?? "").trim().slice(0, 300),
    href: String(link.href ?? "").slice(0, 2048),
  }));
  return {
    url: String(location.href).slice(0, 4096),
    title: String(document.title ?? "").slice(0, 500),
    selectedText: selectedText.slice(0, 6000),
    text: text.slice(0, 24000),
    truncated: text.length > 24000 || selectedText.length > 6000,
    forms,
    links,
  };
})()"#;

#[derive(Default)]
pub(crate) struct BrowserState {
    tabs: Mutex<HashMap<String, BrowserTab>>,
    downloads: Mutex<HashMap<PathBuf, PendingDownload>>,
}

struct BrowserTab {
    project_id: String,
    identity_partition: String,
    label: String,
    proxy: Option<crate::browser_proxy::BrowserProxyHandle>,
}

struct PendingDownload {
    id: String,
    project_id: String,
    tab_id: String,
    filename: String,
    path: PathBuf,
    finished: Arc<AtomicBool>,
}

#[derive(Debug, Deserialize, Clone, Copy)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BrowserBounds {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct BrowserPageEvent {
    tab_id: String,
    url: String,
    state: &'static str,
    title: Option<String>,
    detail: Option<String>,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct BrowserDownloadEvent {
    tab_id: String,
    download_id: Option<String>,
    filename: Option<String>,
    size: Option<u64>,
    state: &'static str,
    detail: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawBrowserPageContext {
    url: String,
    #[serde(default)]
    title: String,
    #[serde(default)]
    selected_text: String,
    #[serde(default)]
    text: String,
    #[serde(default)]
    truncated: bool,
    #[serde(default)]
    forms: Vec<RawBrowserPageForm>,
    #[serde(default)]
    links: Vec<RawBrowserPageLink>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawBrowserPageForm {
    #[serde(default)]
    method: String,
    #[serde(default)]
    action: String,
    #[serde(default)]
    fields: Vec<RawBrowserPageFormField>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RawBrowserPageFormField {
    #[serde(default)]
    name: String,
    #[serde(default)]
    id: String,
    #[serde(default)]
    r#type: String,
    #[serde(default)]
    autocomplete: String,
    #[serde(default)]
    required: bool,
}

#[derive(Debug, Deserialize)]
struct RawBrowserPageLink {
    #[serde(default)]
    text: String,
    #[serde(default)]
    href: String,
}

#[derive(Debug, Serialize, PartialEq, Clone)]
#[serde(rename_all = "camelCase")]
struct BrowserPageContext {
    url: String,
    title: String,
    selected_text: String,
    text: String,
    truncated: bool,
    forms: Vec<BrowserPageForm>,
    links: Vec<BrowserPageLink>,
}

#[derive(Debug, Serialize, PartialEq, Clone)]
#[serde(rename_all = "camelCase")]
struct BrowserPageForm {
    method: String,
    action: String,
    fields: Vec<BrowserPageFormField>,
}

#[derive(Debug, Serialize, PartialEq, Clone)]
#[serde(rename_all = "camelCase")]
struct BrowserPageFormField {
    name: String,
    id: String,
    r#type: String,
    autocomplete: String,
    required: bool,
}

#[derive(Debug, Serialize, PartialEq, Clone)]
struct BrowserPageLink {
    text: String,
    href: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct BrowserContextEvent {
    request_id: String,
    tab_id: String,
    state: &'static str,
    context: Option<BrowserPageContext>,
    detail: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BrowserActionRequest {
    action_id: String,
    kind: String,
    locator: serde_json::Map<String, serde_json::Value>,
    arguments: serde_json::Map<String, serde_json::Value>,
    page_url: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct BrowserActionEvent {
    action_id: String,
    tab_id: String,
    state: &'static str,
    result: serde_json::Value,
    detail: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BrowserImportResult {
    state: &'static str,
    path: String,
    size: u64,
    sha256: Option<String>,
    overwritten: bool,
    detail: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BrowserCapabilities {
    engine: &'static str,
    project_storage: &'static str,
    identity_partitions: bool,
    devtools: bool,
    interception_proxy: bool,
    http2_capture: bool,
    websocket_capture: bool,
}

#[tauri::command]
pub(crate) fn browser_capabilities() -> BrowserCapabilities {
    #[cfg(target_os = "macos")]
    let engine = "WKWebView";
    #[cfg(target_os = "linux")]
    let engine = "WebKitGTK";
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    let engine = "system webview";
    BrowserCapabilities {
        engine,
        project_storage: if macos_supports_project_store() {
            "persistent"
        } else {
            "ephemeral"
        },
        identity_partitions: true,
        devtools: true,
        interception_proxy: true,
        http2_capture: true,
        websocket_capture: true,
    }
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn validated_url(value: &str) -> Result<Url, String> {
    let url = Url::parse(value).map_err(|_| "Enter a valid HTTP or HTTPS address.".to_string())?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err("Nebula Browser permits only HTTP and HTTPS addresses.".to_string());
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err("Addresses containing embedded credentials are not accepted.".to_string());
    }
    Ok(url)
}

fn bounded_chars(value: String, limit: usize) -> (String, bool) {
    let mut characters = value.chars();
    let bounded: String = characters.by_ref().take(limit).collect();
    (bounded, characters.next().is_some())
}

fn decode_browser_context(raw: &str) -> Result<BrowserPageContext, String> {
    if raw.len() > MAX_CAPTURE_RAW_BYTES {
        return Err("The live page context snapshot exceeded the safe size limit.".to_string());
    }
    let raw: RawBrowserPageContext = serde_json::from_str(raw)
        .map_err(|_| "The live page returned an invalid context snapshot.".to_string())?;
    let url = validated_url(&raw.url)?.to_string();
    let (title, title_truncated) = bounded_chars(raw.title, 500);
    let (selected_text, selection_truncated) =
        bounded_chars(raw.selected_text, MAX_CAPTURE_SELECTION_CHARS);
    let (text, text_truncated) = bounded_chars(raw.text, MAX_CAPTURE_TEXT_CHARS);
    let forms = raw
        .forms
        .into_iter()
        .take(MAX_CAPTURE_FORMS)
        .map(|form| BrowserPageForm {
            method: bounded_chars(form.method.to_uppercase(), 16).0,
            action: bounded_chars(form.action, 2_048).0,
            fields: form
                .fields
                .into_iter()
                .take(40)
                .map(|field| BrowserPageFormField {
                    name: bounded_chars(field.name, 200).0,
                    id: bounded_chars(field.id, 200).0,
                    r#type: bounded_chars(field.r#type.to_lowercase(), 40).0,
                    autocomplete: bounded_chars(field.autocomplete, 100).0,
                    required: field.required,
                })
                .collect(),
        })
        .collect();
    let links = raw
        .links
        .into_iter()
        .take(MAX_CAPTURE_LINKS)
        .map(|link| BrowserPageLink {
            text: bounded_chars(link.text, 300).0,
            href: bounded_chars(link.href, 2_048).0,
        })
        .collect();
    Ok(BrowserPageContext {
        url,
        title,
        selected_text,
        text,
        truncated: raw.truncated || title_truncated || selection_truncated || text_truncated,
        forms,
        links,
    })
}

fn checked_bounds(bounds: BrowserBounds) -> Result<BrowserBounds, String> {
    let values = [bounds.x, bounds.y, bounds.width, bounds.height];
    if values.iter().any(|value| !value.is_finite())
        || bounds.x < 0.0
        || bounds.y < 0.0
        || bounds.width < 1.0
        || bounds.height < 1.0
        || bounds.width > 16_384.0
        || bounds.height > 16_384.0
    {
        return Err("The browser surface has invalid bounds.".to_string());
    }
    Ok(bounds)
}

fn child_position(bounds: &BrowserBounds) -> LogicalPosition<f64> {
    LogicalPosition::new(bounds.x, bounds.y)
}

fn child_size(bounds: &BrowserBounds) -> LogicalSize<f64> {
    LogicalSize::new(bounds.width, bounds.height)
}

#[cfg(any(target_os = "macos", test))]
fn inset_browser_bounds(mut bounds: BrowserBounds, top_inset: f64) -> BrowserBounds {
    let inset = top_inset
        .clamp(0.0, 96.0)
        .min((bounds.height - 1.0).max(0.0));
    bounds.y += inset;
    bounds.height -= inset;
    bounds
}

#[cfg(any(target_os = "macos", test))]
fn appkit_child_y(
    parent_origin_y: f64,
    parent_height: f64,
    y: f64,
    height: f64,
    parent_is_flipped: bool,
) -> f64 {
    if parent_is_flipped {
        parent_origin_y + y
    } else {
        parent_origin_y + parent_height - y - height
    }
}

#[cfg(target_os = "macos")]
fn appkit_browser_frame(parent: &NSView, bounds: &BrowserBounds) -> NSRect {
    let parent_bounds = parent.bounds();
    // Full-size-content windows place the WKWebView beneath the title bar while WebKit's DOM
    // viewport begins at the unobscured safe-area origin. Window inner/outer positions do not
    // describe this offset and are commonly identical in Tauri.
    let bounds = inset_browser_bounds(*bounds, parent.safeAreaInsets().top);
    NSRect::new(
        NSPoint::new(
            parent_bounds.origin.x + bounds.x,
            appkit_child_y(
                parent_bounds.origin.y,
                parent_bounds.size.height,
                bounds.y,
                bounds.height,
                parent.isFlipped(),
            ),
        ),
        NSSize::new(bounds.width, bounds.height),
    )
}

#[cfg(target_os = "macos")]
fn wait_for_native_browser_result(
    receiver: std::sync::mpsc::Receiver<Result<(), String>>,
) -> Result<(), String> {
    receiver
        .recv_timeout(Duration::from_secs(2))
        .map_err(|_| "The macOS browser surface did not respond.".to_string())?
}

// Child WKWebViews created by Tauri are siblings of the main application WKWebView. AppKit
// composites those native siblings above the DOM, so CSS overflow cannot stop a page from
// covering Nebula's address bar. Reparent the page beneath a layer-backed NSView attached to the
// main WKWebView. Its coordinates now match getBoundingClientRect(), and masksToBounds provides a
// real native clip at every edge of the Browser surface.
#[cfg(target_os = "macos")]
fn install_macos_browser_container<R: tauri::Runtime>(
    app: &AppHandle<R>,
    browser: &tauri::Webview<R>,
    bounds: BrowserBounds,
) -> Result<(), String> {
    let main = app
        .get_webview("main")
        .ok_or_else(|| "The Nebula webview is unavailable.".to_string())?;
    let browser = browser.clone();
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);

    main.with_webview(move |main_native| {
        let main_address = main_native.inner() as usize;
        let callback_sender = sender.clone();
        if let Err(error) = browser.with_webview(move |browser_native| {
            let result = (|| {
                let marker = MainThreadMarker::new().ok_or_else(|| {
                    "The browser container must be created on the main thread.".to_string()
                })?;
                // SAFETY: Tauri supplies live WKWebView pointers to this main-thread callback.
                // WKWebView inherits from NSView, and both views remain retained by Tauri.
                let (parent, child): (&NSView, &NSView) = unsafe {
                    (
                        &*((main_address as *mut std::ffi::c_void).cast::<NSView>()),
                        &*browser_native.inner().cast::<NSView>(),
                    )
                };
                if std::ptr::eq(parent, child) {
                    return Err(
                        "The embedded browser cannot use Nebula's root webview.".to_string()
                    );
                }

                let container = NSView::initWithFrame(
                    NSView::alloc(marker),
                    appkit_browser_frame(parent, &bounds),
                );
                container.setWantsLayer(true);
                let layer = container
                    .layer()
                    .ok_or_else(|| "The browser clipping layer is unavailable.".to_string())?;
                layer.setMasksToBounds(true);
                container.setAutoresizesSubviews(true);
                container.setHidden(true);

                parent.addSubview(&container);
                // The existing superview remains alive, and AppKit retains the child when it is
                // immediately added to the new container.
                child.removeFromSuperview();
                container.addSubview(child);
                child.setFrame(container.bounds());
                child.setHidden(true);
                Ok(())
            })();
            if callback_sender.send(result).is_err() {
                // The caller already timed out and dropped its one-shot receiver.
            }
        }) {
            if sender
                .send(Err(format!(
                    "cannot access the embedded macOS browser view: {error}"
                )))
                .is_err()
            {
                // The caller already timed out and dropped its one-shot receiver.
            }
        }
    })
    .map_err(|error| format!("cannot access the Nebula macOS webview: {error}"))?;

    wait_for_native_browser_result(receiver)
}

#[cfg(target_os = "macos")]
fn with_macos_browser_container<R, F>(
    browser: &tauri::Webview<R>,
    operation: F,
) -> Result<(), String>
where
    R: tauri::Runtime,
    F: FnOnce(&NSView, &NSView) -> Result<(), String> + Send + 'static,
{
    let (sender, receiver) = std::sync::mpsc::sync_channel(1);
    browser
        .with_webview(move |native| {
            let result = (|| {
                // SAFETY: Tauri supplies a live WKWebView pointer on the AppKit main thread.
                let child: &NSView = unsafe { &*native.inner().cast::<NSView>() };
                // SAFETY: The container is retained by its parent and remains attached for the
                // lifetime of the browser tab.
                let container: Retained<NSView> = unsafe { child.superview() }
                    .ok_or_else(|| "The browser clipping container is unavailable.".to_string())?;
                operation(&container, child)
            })();
            if sender.send(result).is_err() {
                // The caller already timed out and dropped its one-shot receiver.
            }
        })
        .map_err(|error| format!("cannot access the embedded macOS browser view: {error}"))?;
    wait_for_native_browser_result(receiver)
}

#[cfg(target_os = "macos")]
fn resize_macos_browser_container<R: tauri::Runtime>(
    browser: &tauri::Webview<R>,
    bounds: BrowserBounds,
) -> Result<(), String> {
    with_macos_browser_container(browser, move |container, child| {
        // SAFETY: The clipping container remains attached to Nebula's main WKWebView.
        let parent = unsafe { container.superview() }
            .ok_or_else(|| "The Nebula browser surface is unavailable.".to_string())?;
        container.setFrame(appkit_browser_frame(&parent, &bounds));
        child.setFrame(container.bounds());
        Ok(())
    })
}

#[cfg(target_os = "macos")]
fn set_macos_browser_container_visible<R: tauri::Runtime>(
    browser: &tauri::Webview<R>,
    visible: bool,
) -> Result<(), String> {
    with_macos_browser_container(browser, move |container, child| {
        container.setHidden(!visible);
        child.setHidden(!visible);
        Ok(())
    })
}

#[cfg(target_os = "macos")]
fn remove_macos_browser_container<R: tauri::Runtime>(
    browser: &tauri::Webview<R>,
) -> Result<(), String> {
    with_macos_browser_container(browser, |container, child| {
        // Both views are retained for this callback. Removing the child first prevents an orphaned
        // native view from intercepting input while Tauri closes it.
        child.removeFromSuperview();
        container.removeFromSuperview();
        Ok(())
    })
}

fn project_key(project_id: &str) -> [u8; 16] {
    let digest = Sha256::digest(format!("nebula-browser-profile-v1:{project_id}").as_bytes());
    let mut key = [0_u8; 16];
    key.copy_from_slice(&digest[..16]);
    key
}

fn project_key_hex(project_id: &str) -> String {
    project_key(project_id)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

const DEFAULT_IDENTITY_PARTITION: &str = "browser-00000000-0000-0000-0000-000000000000";

fn identity_key(project_id: &str, identity_partition: &str) -> [u8; 16] {
    if identity_partition == DEFAULT_IDENTITY_PARTITION {
        return project_key(project_id);
    }
    let digest = Sha256::digest(
        format!("nebula-browser-profile-v2:{project_id}:{identity_partition}").as_bytes(),
    );
    let mut key = [0_u8; 16];
    key.copy_from_slice(&digest[..16]);
    key
}

fn identity_key_hex(project_id: &str, identity_partition: &str) -> String {
    identity_key(project_id, identity_partition)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[cfg(target_os = "macos")]
fn macos_supports_project_store() -> bool {
    use objc2_foundation::NSProcessInfo;
    NSProcessInfo::processInfo()
        .operatingSystemVersion()
        .majorVersion
        >= 14
}

#[cfg(not(target_os = "macos"))]
fn macos_supports_project_store() -> bool {
    true
}

fn random_id(prefix: &str) -> Result<String, String> {
    let mut bytes = [0_u8; 16];
    random_fill(&mut bytes)
        .map_err(|error| format!("cannot create a browser identifier: {error}"))?;
    Ok(format!(
        "{prefix}-{}",
        bytes
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>()
    ))
}

fn safe_filename(value: &str) -> String {
    let candidate = Path::new(value)
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("download.bin");
    let mut cleaned: String = candidate
        .chars()
        .map(|character| {
            if character.is_control() || matches!(character, '/' | '\\' | ':' | '\0') {
                '_'
            } else {
                character
            }
        })
        .take(180)
        .collect();
    cleaned = cleaned.trim_matches(['.', ' ']).to_string();
    if cleaned.is_empty() {
        "download.bin".to_string()
    } else {
        cleaned
    }
}

fn emit_page(app: &AppHandle, event: BrowserPageEvent) {
    if app.emit_to("main", "nebula-browser-page", event).is_err() {
        record_browser_failure(
            app,
            "desktop.browser.page_event_delivery_failed",
            "A browser page update could not be delivered to the interface.",
            "event-delivery",
        );
    }
}

fn emit_download(app: &AppHandle, event: BrowserDownloadEvent) {
    if app
        .emit_to("main", "nebula-browser-download", event)
        .is_err()
    {
        record_browser_failure(
            app,
            "desktop.browser.download_event_delivery_failed",
            "A browser download update could not be delivered to the interface.",
            "event-delivery",
        );
    }
}

fn emit_context(app: &AppHandle, event: BrowserContextEvent) {
    if app
        .emit_to("main", "nebula-browser-context", event)
        .is_err()
    {
        record_browser_failure(
            app,
            "desktop.browser.context_event_delivery_failed",
            "A live-page context update could not be delivered to the interface.",
            "event-delivery",
        );
    }
}

fn record_browser_failure(app: &AppHandle, event_code: &str, message: &str, stage: &str) {
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

fn find_tab(state: &BrowserState, tab_id: &str, project_id: &str) -> Result<String, String> {
    let tabs = state
        .tabs
        .lock()
        .map_err(|_| "Browser state is unavailable.".to_string())?;
    let tab = tabs
        .get(tab_id)
        .ok_or_else(|| "This browser tab is no longer open.".to_string())?;
    if tab.project_id != project_id {
        return Err("The browser tab belongs to another Project.".to_string());
    }
    Ok(tab.label.clone())
}

fn close_tab_internal(app: &AppHandle, state: &BrowserState, tab_id: &str) -> Result<(), String> {
    let tab = state
        .tabs
        .lock()
        .map_err(|_| "Browser state is unavailable.".to_string())?
        .remove(tab_id);
    if let Some(tab) = tab {
        if let Some(webview) = app.get_webview(&tab.label) {
            #[cfg(target_os = "macos")]
            if remove_macos_browser_container(&webview).is_err() {
                record_browser_failure(
                    app,
                    "desktop.browser.native_container_cleanup_failed",
                    "A macOS browser clipping container could not be removed cleanly.",
                    "tab-cleanup",
                );
            }
            webview
                .close()
                .map_err(|error| format!("cannot close browser tab: {error}"))?;
        }
        if let Some(proxy) = tab.proxy {
            proxy.stop();
        }
    }
    Ok(())
}

#[tauri::command]
pub(crate) fn browser_create_tab(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    identity_partition: String,
    session_id: String,
    proxy_enabled: bool,
    url: String,
    bounds: BrowserBounds,
) -> Result<(), String> {
    if !valid_identifier(&tab_id)
        || !valid_identifier(&project_id)
        || !valid_identifier(&identity_partition)
        || !valid_identifier(&session_id)
        || !identity_partition.starts_with("browser-")
    {
        return Err("The browser tab, Project, or identity identifier is invalid.".to_string());
    }
    let url = validated_url(&url)?;
    let bounds = checked_bounds(bounds)?;
    {
        let tabs = state
            .tabs
            .lock()
            .map_err(|_| "Browser state is unavailable.".to_string())?;
        if tabs.contains_key(&tab_id) {
            return Ok(());
        }
        if tabs
            .values()
            .filter(|tab| tab.project_id == project_id)
            .count()
            >= MAX_TABS_PER_PROJECT
        {
            return Err(format!(
                "A Project may have at most {MAX_TABS_PER_PROJECT} browser tabs."
            ));
        }
    }

    let label = format!("browser-{tab_id}");
    let profile_root = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate browser storage: {error}"))?
        .join("browser-profiles");
    let profile_dir = if identity_partition == DEFAULT_IDENTITY_PARTITION {
        profile_root.join(project_key_hex(&project_id))
    } else {
        profile_root
            .join("identities")
            .join(project_key_hex(&project_id))
            .join(identity_key_hex(&project_id, &identity_partition))
    };
    fs::create_dir_all(&profile_dir)
        .map_err(|error| format!("cannot prepare browser storage: {error}"))?;

    let navigation_app = app.clone();
    let navigation_tab = tab_id.clone();
    let popup_app = app.clone();
    let popup_tab = tab_id.clone();
    let load_app = app.clone();
    let load_tab = tab_id.clone();
    let title_app = app.clone();
    let title_tab = tab_id.clone();
    let download_app = app.clone();
    let download_tab = tab_id.clone();
    let download_project = project_id.clone();

    let proxy = if proxy_enabled {
        Some(crate::browser_proxy::start(
            &app,
            &project_key_hex(&project_id),
            &session_id,
            &tab_id,
        )?)
    } else {
        None
    };

    let mut builder = WebviewBuilder::new(&label, WebviewUrl::External(url))
        .on_navigation(move |next| {
            let allowed = validated_url(next.as_str()).is_ok();
            if !allowed {
                emit_page(&navigation_app, BrowserPageEvent { tab_id: navigation_tab.clone(), url: next.to_string(), state: "blocked", title: None, detail: Some("Nebula Browser blocked a non-HTTP navigation.".to_string()) });
            }
            allowed
        })
        .on_new_window(move |next, _features| {
            if validated_url(next.as_str()).is_ok() {
                emit_page(&popup_app, BrowserPageEvent { tab_id: popup_tab.clone(), url: next.to_string(), state: "new_tab", title: None, detail: None });
            } else {
                emit_page(&popup_app, BrowserPageEvent { tab_id: popup_tab.clone(), url: next.to_string(), state: "blocked", title: None, detail: Some("Nebula Browser blocked a pop-up with an unsupported address.".to_string()) });
            }
            NewWindowResponse::Deny
        })
        .on_page_load(move |_webview, payload| {
            let state = match payload.event() { PageLoadEvent::Started => "loading", PageLoadEvent::Finished => "loaded" };
            emit_page(&load_app, BrowserPageEvent { tab_id: load_tab.clone(), url: payload.url().to_string(), state, title: None, detail: None });
        })
        .on_document_title_changed(move |_webview, title| {
            emit_page(&title_app, BrowserPageEvent { tab_id: title_tab.clone(), url: String::new(), state: "title", title: Some(title.chars().take(300).collect()), detail: None });
        })
        .on_download(move |_webview, event| {
            match event {
                DownloadEvent::Requested { url: _, destination } => {
                    let original_name = destination.file_name().and_then(|name| name.to_str()).unwrap_or("download.bin");
                    let filename = safe_filename(original_name);
                    let Ok(download_id) = random_id("download") else { return false; };
                    let Ok(cache_dir) = download_app.path().app_cache_dir() else { return false; };
                    let staging = cache_dir.join("browser-downloads");
                    if fs::create_dir_all(&staging).is_err() { return false; }
                    let path = staging.join(format!("{download_id}.part"));
                    *destination = path.clone();
                    let finished = Arc::new(AtomicBool::new(false));
                    let pending = PendingDownload { id: download_id.clone(), project_id: download_project.clone(), tab_id: download_tab.clone(), filename: filename.clone(), path: path.clone(), finished: finished.clone() };
                    if let Ok(mut downloads) = download_app.state::<BrowserState>().downloads.lock() {
                        downloads.insert(path.clone(), pending);
                    } else { return false; }

                    let monitor_app = download_app.clone();
                    let monitor_tab = download_tab.clone();
                    std::thread::spawn(move || {
                        while !finished.load(Ordering::Relaxed) {
                            std::thread::sleep(Duration::from_millis(250));
                            if fs::metadata(&path).map(|meta| meta.len() > MAX_DOWNLOAD_BYTES).unwrap_or(false) {
                                finished.store(true, Ordering::Relaxed);
                                if let Err(error) = fs::remove_file(&path)
                                    && error.kind() != std::io::ErrorKind::NotFound
                                {
                                    record_browser_failure(
                                        &monitor_app,
                                        "desktop.browser.staged_download_cleanup_failed",
                                        "An oversized staged browser download could not be removed.",
                                        "download-cleanup",
                                    );
                                }
                                if let Ok(mut downloads) = monitor_app.state::<BrowserState>().downloads.lock() { downloads.remove(&path); }
                                if close_tab_internal(&monitor_app, &monitor_app.state::<BrowserState>(), &monitor_tab).is_err() {
                                    record_browser_failure(
                                        &monitor_app,
                                        "desktop.browser.oversized_download_tab_close_failed",
                                        "The browser tab for an oversized download could not be closed.",
                                        "download-cleanup",
                                    );
                                }
                                emit_download(&monitor_app, BrowserDownloadEvent { tab_id: monitor_tab.clone(), download_id: None, filename: None, size: None, state: "rejected", detail: Some("The download exceeded the 1 GiB Project file limit. Reload the tab to continue browsing.".to_string()) });
                                break;
                            }
                        }
                    });
                    true
                }
                DownloadEvent::Finished { url: _, path, success } => {
                    let pending = download_app.state::<BrowserState>().downloads.lock().ok().and_then(|mut downloads| {
                        let key = path.or_else(|| downloads.iter().find(|(_, item)| item.tab_id == download_tab).map(|(key, _)| key.clone()));
                        key.and_then(|key| downloads.remove(&key))
                    });
                    if let Some(pending) = pending {
                        pending.finished.store(true, Ordering::Relaxed);
                        if success {
                            let size = fs::metadata(&pending.path).map(|meta| meta.len()).unwrap_or(0);
                            if size <= MAX_DOWNLOAD_BYTES {
                                let download_id = pending.id.clone();
                                let filename = pending.filename.clone();
                                let tab_id = pending.tab_id.clone();
                                if let Ok(mut downloads) = download_app.state::<BrowserState>().downloads.lock() { downloads.insert(pending.path.clone(), pending); }
                                emit_download(&download_app, BrowserDownloadEvent { tab_id, download_id: Some(download_id), filename: Some(filename), size: Some(size), state: "ready", detail: None });
                            } else {
                                if let Err(error) = fs::remove_file(&pending.path)
                                    && error.kind() != std::io::ErrorKind::NotFound
                                {
                                    record_browser_failure(
                                        &download_app,
                                        "desktop.browser.staged_download_cleanup_failed",
                                        "An oversized staged browser download could not be removed.",
                                        "download-cleanup",
                                    );
                                }
                                emit_download(&download_app, BrowserDownloadEvent { tab_id: pending.tab_id, download_id: Some(pending.id), filename: Some(pending.filename), size: Some(size), state: "rejected", detail: Some("The download exceeded the 1 GiB Project file limit.".to_string()) });
                            }
                        } else {
                            if let Err(error) = fs::remove_file(&pending.path)
                                && error.kind() != std::io::ErrorKind::NotFound
                            {
                                record_browser_failure(
                                    &download_app,
                                    "desktop.browser.staged_download_cleanup_failed",
                                    "A failed staged browser download could not be removed.",
                                    "download-cleanup",
                                );
                            }
                            emit_download(&download_app, BrowserDownloadEvent { tab_id: pending.tab_id, download_id: Some(pending.id), filename: Some(pending.filename), size: None, state: "failed", detail: Some("The website download did not complete.".to_string()) });
                        }
                    }
                    true
                }
                _ => true,
            }
        })
        .enable_clipboard_access()
        .focused(false)
        .zoom_hotkeys_enabled(true)
        .devtools(true);

    if let Some(proxy) = &proxy {
        builder = builder.proxy_url(proxy.url.clone());
    }

    #[cfg(target_os = "macos")]
    {
        if macos_supports_project_store() {
            builder = builder.data_store_identifier(identity_key(&project_id, &identity_partition));
        } else {
            builder = builder.incognito(true);
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        builder = builder.data_directory(profile_dir);
    }

    let window = app
        .get_window("main")
        .ok_or_else(|| "The Nebula window is unavailable.".to_string())?;
    let webview = window
        .add_child(builder, child_position(&bounds), child_size(&bounds))
        .map_err(|error| format!("cannot create browser tab: {error}"))?;
    if let Err(error) = webview.hide() {
        if webview.close().is_err() {
            record_browser_failure(
                &app,
                "desktop.browser.failed_tab_cleanup_failed",
                "A browser tab that failed to initialize could not be closed.",
                "tab-cleanup",
            );
        }
        return Err(format!("cannot initialize browser tab visibility: {error}"));
    }
    #[cfg(target_os = "macos")]
    if let Err(error) = install_macos_browser_container(&app, &webview, bounds) {
        if webview.close().is_err() {
            record_browser_failure(
                &app,
                "desktop.browser.failed_tab_cleanup_failed",
                "A browser tab with a failed clipping container could not be closed.",
                "tab-cleanup",
            );
        }
        return Err(format!(
            "cannot initialize the macOS browser surface: {error}"
        ));
    }
    state
        .tabs
        .lock()
        .map_err(|_| "Browser state is unavailable.".to_string())?
        .insert(
            tab_id,
            BrowserTab {
                project_id,
                identity_partition,
                label,
                proxy,
            },
        );
    Ok(())
}

#[tauri::command]
pub(crate) fn browser_reveal_proxy_ca(
    app: AppHandle,
    project_id: String,
) -> Result<String, String> {
    if !valid_identifier(&project_id) {
        return Err("The Project identifier is invalid.".to_string());
    }
    crate::browser_proxy::reveal_ca(&app, &project_key_hex(&project_id))
        .map(|path| path.to_string_lossy().to_string())
}

#[tauri::command]
pub(crate) fn browser_navigate(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    url: String,
) -> Result<(), String> {
    let label = find_tab(&state, &tab_id, &project_id)?;
    app.get_webview(&label)
        .ok_or_else(|| "This browser tab is unavailable.".to_string())?
        .navigate(validated_url(&url)?)
        .map_err(|error| format!("cannot navigate browser tab: {error}"))
}

#[tauri::command]
pub(crate) fn browser_capture_context(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    request_id: String,
) -> Result<(), String> {
    if !valid_identifier(&request_id) {
        return Err("The browser capture identifier is invalid.".to_string());
    }
    let label = find_tab(&state, &tab_id, &project_id)?;
    let webview = app
        .get_webview(&label)
        .ok_or_else(|| "This browser tab is unavailable.".to_string())?;
    let callback_app = app.clone();
    let callback_tab = tab_id.clone();
    webview
        .eval_with_callback(BROWSER_CONTEXT_SCRIPT, move |raw| {
            let event = match decode_browser_context(&raw) {
                Ok(context) => BrowserContextEvent {
                    request_id: request_id.clone(),
                    tab_id: callback_tab.clone(),
                    state: "ready",
                    context: Some(context),
                    detail: None,
                },
                Err(detail) => BrowserContextEvent {
                    request_id: request_id.clone(),
                    tab_id: callback_tab.clone(),
                    state: "failed",
                    context: None,
                    detail: Some(detail),
                },
            };
            emit_context(&callback_app, event);
        })
        .map_err(|error| format!("cannot capture live page context: {error}"))
}

#[tauri::command]
pub(crate) fn browser_execute_action(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    request: BrowserActionRequest,
) -> Result<(), String> {
    if !valid_identifier(&request.action_id) {
        return Err("The browser action identifier is invalid.".to_string());
    }
    validated_url(&request.page_url)?;
    let allowed_kinds = [
        "navigate", "click", "fill", "select", "press", "extract", "replay",
    ];
    if !allowed_kinds.contains(&request.kind.as_str()) {
        return Err(
            "This browser action kind is not executable by the native browser.".to_string(),
        );
    }
    if request.kind == "navigate" {
        let target = request
            .arguments
            .get("url")
            .and_then(|value| value.as_str())
            .ok_or_else(|| "Navigate actions require a URL argument.".to_string())?;
        validated_url(target)?;
    }
    if request.kind == "replay" {
        let target = request
            .arguments
            .get("url")
            .and_then(|value| value.as_str())
            .ok_or_else(|| "Replay actions require a URL argument.".to_string())?;
        validated_url(target)?;
    }
    let locator = serde_json::to_string(&request.locator)
        .map_err(|error| format!("cannot encode browser action locator: {error}"))?;
    let arguments = serde_json::to_string(&request.arguments)
        .map_err(|error| format!("cannot encode browser action arguments: {error}"))?;
    let kind = serde_json::to_string(&request.kind)
        .map_err(|error| format!("cannot encode browser action kind: {error}"))?;
    let expected_url = serde_json::to_string(&request.page_url)
        .map_err(|error| format!("cannot encode browser action page: {error}"))?;
    let script = format!(
        r#"(() => {{
      const kind = {kind};
      const locator = {locator};
      const args = {arguments};
      const expectedUrl = {expected_url};
      const fail = (error) => ({{ ok: false, error: String(error).slice(0, 2000), pageUrl: String(location.href).slice(0, 4096) }});
      try {{
        if (String(location.href) !== expectedUrl) return fail("The page changed after approval.");
        if (kind === "navigate") {{ location.assign(String(args.url)); return {{ ok: true, kind, pageUrl: expectedUrl, navigation: String(args.url).slice(0, 4096) }}; }}
        if (kind === "replay") {{
          const target = new URL(String(args.url), location.href);
          const requestHeaders = args.headers && typeof args.headers === "object" ? args.headers : {{}};
          const forbidden = /authorization|cookie|csrf|xsrf|api[-_]?key|token/i;
          if (Object.keys(requestHeaders).some((name) => forbidden.test(name))) return fail("Replay headers cannot contain reusable secrets.");
          const xhr = new XMLHttpRequest();
          xhr.open(String(args.method || "GET").toUpperCase(), target.href, false);
          xhr.withCredentials = true;
          for (const [name, value] of Object.entries(requestHeaders)) xhr.setRequestHeader(String(name), String(value));
          xhr.send(args.body === undefined || args.body === "" ? null : String(args.body).slice(0, 65536));
          const responseHeaders = Object.fromEntries(String(xhr.getAllResponseHeaders()).trim().split(/[\r\n]+/).filter(Boolean).map((line) => {{ const index = line.indexOf(":"); return index > 0 ? [line.slice(0, index).trim(), line.slice(index + 1).trim()] : [line, ""]; }}));
          return {{ ok: true, kind, pageUrl: expectedUrl, requestUrl: target.href.slice(0, 4096), method: String(args.method || "GET").toUpperCase(), status: xhr.status, responseHeaders, responsePreview: String(xhr.responseText || "").slice(0, 16000), responseBytes: String(xhr.responseText || "").length }};
        }}
        let candidates = [];
        if (locator.css) candidates = Array.from(document.querySelectorAll(String(locator.css)));
        else if (locator.label) {{
          const wanted = String(locator.label).trim().toLocaleLowerCase();
          candidates = Array.from(document.querySelectorAll("label")).filter((label) => String(label.innerText || label.textContent || "").trim().toLocaleLowerCase() === wanted).map((label) => label.control).filter(Boolean);
        }} else {{
          candidates = Array.from(document.querySelectorAll("button,a,input,select,textarea,[role],[tabindex]"));
          if (locator.role) candidates = candidates.filter((element) => String(element.getAttribute("role") || element.tagName).toLocaleLowerCase() === String(locator.role).toLocaleLowerCase());
          if (locator.name) {{ const wanted = String(locator.name).trim().toLocaleLowerCase(); candidates = candidates.filter((element) => String(element.getAttribute("aria-label") || element.innerText || element.textContent || element.getAttribute("name") || "").trim().toLocaleLowerCase() === wanted); }}
          if (locator.text) {{ const wanted = String(locator.text).trim().toLocaleLowerCase(); candidates = candidates.filter((element) => String(element.innerText || element.textContent || "").trim().toLocaleLowerCase() === wanted); }}
        }}
        candidates = Array.from(new Set(candidates)).filter((element) => element instanceof HTMLElement && element.isConnected);
        if (candidates.length !== 1) return fail(`Semantic locator matched ${{candidates.length}} elements; exactly one is required.`);
        const element = candidates[0];
        if (kind === "click") element.click();
        else if (kind === "fill") {{
          if (!("non_secret_text" in args)) return fail("Fill requires an explicit non_secret_text argument.");
          if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement)) return fail("Fill target is not a text control.");
          const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), "value")?.set;
          if (!setter) return fail("Fill target has no value setter.");
          setter.call(element, String(args.non_secret_text).slice(0, 4000));
          element.dispatchEvent(new Event("input", {{ bubbles: true }}));
          element.dispatchEvent(new Event("change", {{ bubbles: true }}));
        }} else if (kind === "select") {{
          if (!(element instanceof HTMLSelectElement)) return fail("Select target is not a select control.");
          element.value = String(args.value || ""); element.dispatchEvent(new Event("change", {{ bubbles: true }}));
        }} else if (kind === "press") {{
          const key = String(args.key || "").slice(0, 80); if (!key) return fail("Press requires a key.");
          element.dispatchEvent(new KeyboardEvent("keydown", {{ key, bubbles: true }})); element.dispatchEvent(new KeyboardEvent("keyup", {{ key, bubbles: true }}));
        }}
        const extracted = kind === "extract" ? String(element.innerText || element.textContent || "").slice(0, 16000) : undefined;
        return {{ ok: true, kind, pageUrl: expectedUrl, matched: 1, extracted, tag: element.tagName.toLocaleLowerCase() }};
      }} catch (error) {{ return fail(error instanceof Error ? error.message : error); }}
    }})()"#
    );
    let label = find_tab(&state, &tab_id, &project_id)?;
    let webview = app
        .get_webview(&label)
        .ok_or_else(|| "This browser tab is unavailable.".to_string())?;
    let callback_app = app.clone();
    let callback_tab = tab_id.clone();
    let action_id = request.action_id.clone();
    webview
        .eval_with_callback(&script, move |raw| {
            let decoded = serde_json::from_str::<serde_json::Value>(&raw);
            let (state, result, detail) = match decoded {
                Ok(result) if result.get("ok").and_then(|value| value.as_bool()) == Some(true) => {
                    ("complete", result, None)
                }
                Ok(result) => {
                    let detail = result
                        .get("error")
                        .and_then(|value| value.as_str())
                        .unwrap_or("The browser action failed.")
                        .to_string();
                    ("failed", result, Some(detail))
                }
                Err(_) => (
                    "failed",
                    serde_json::json!({}),
                    Some("The browser returned an invalid action receipt.".to_string()),
                ),
            };
            let _ = callback_app.emit(
                "nebula-browser-action",
                BrowserActionEvent {
                    action_id: action_id.clone(),
                    tab_id: callback_tab.clone(),
                    state,
                    result,
                    detail,
                },
            );
        })
        .map_err(|error| format!("cannot execute the approved browser action: {error}"))
}

#[tauri::command]
pub(crate) fn browser_control(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    action: String,
) -> Result<(), String> {
    let label = find_tab(&state, &tab_id, &project_id)?;
    let webview = app
        .get_webview(&label)
        .ok_or_else(|| "This browser tab is unavailable.".to_string())?;
    match action.as_str() {
        "back" => webview.eval("history.back()"),
        "forward" => webview.eval("history.forward()"),
        "stop" => webview.eval("window.stop()"),
        "reload" => webview.reload(),
        _ => return Err("The browser control is invalid.".to_string()),
    }
    .map_err(|error| format!("cannot control browser tab: {error}"))
}

#[tauri::command]
pub(crate) fn browser_set_bounds(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    bounds: BrowserBounds,
) -> Result<(), String> {
    let label = find_tab(&state, &tab_id, &project_id)?;
    let bounds = checked_bounds(bounds)?;
    let webview = app
        .get_webview(&label)
        .ok_or_else(|| "This browser tab is unavailable.".to_string())?;
    #[cfg(target_os = "macos")]
    {
        resize_macos_browser_container(&webview, bounds)
            .map_err(|error| format!("cannot resize browser tab: {error}"))
    }
    #[cfg(not(target_os = "macos"))]
    {
        webview
            .set_position(child_position(&bounds))
            .and_then(|_| webview.set_size(child_size(&bounds)))
            .map_err(|error| format!("cannot resize browser tab: {error}"))
    }
}

#[tauri::command]
pub(crate) fn browser_set_visible(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    visible: bool,
) -> Result<(), String> {
    let label = find_tab(&state, &tab_id, &project_id)?;
    let webview = app
        .get_webview(&label)
        .ok_or_else(|| "This browser tab is unavailable.".to_string())?;
    #[cfg(target_os = "macos")]
    {
        set_macos_browser_container_visible(&webview, visible)
            .map_err(|error| format!("cannot change browser visibility: {error}"))
    }
    #[cfg(not(target_os = "macos"))]
    {
        if visible {
            webview.show()
        } else {
            webview.hide()
        }
        .map_err(|error| format!("cannot change browser visibility: {error}"))
    }
}

#[tauri::command]
pub(crate) fn browser_close_tab(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
) -> Result<(), String> {
    find_tab(&state, &tab_id, &project_id)?;
    close_tab_internal(&app, &state, &tab_id)
}

#[tauri::command]
pub(crate) fn browser_open_devtools(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
) -> Result<(), String> {
    let label = find_tab(&state, &tab_id, &project_id)?;
    let webview = app
        .get_webview(&label)
        .ok_or_else(|| "This browser tab is unavailable.".to_string())?;
    webview.open_devtools();
    Ok(())
}

#[tauri::command]
pub(crate) fn browser_clear_identity_data(
    app: AppHandle,
    state: State<'_, BrowserState>,
    project_id: String,
    identity_partition: String,
) -> Result<(), String> {
    if !valid_identifier(&project_id)
        || !valid_identifier(&identity_partition)
        || !identity_partition.starts_with("browser-")
    {
        return Err("The Project or identity identifier is invalid.".to_string());
    }
    let tabs: Vec<String> = state
        .tabs
        .lock()
        .map_err(|_| "Browser state is unavailable.".to_string())?
        .iter()
        .filter(|(_, tab)| {
            tab.project_id == project_id && tab.identity_partition == identity_partition
        })
        .map(|(id, _)| id.clone())
        .collect();
    for id in tabs {
        let label = find_tab(&state, &id, &project_id)?;
        if let Some(webview) = app.get_webview(&label) {
            webview
                .clear_all_browsing_data()
                .map_err(|error| format!("cannot clear browser identity storage: {error}"))?;
        }
        close_tab_internal(&app, &state, &id)?;
    }
    #[cfg(not(target_os = "macos"))]
    {
        let profile_root = app
            .path()
            .app_data_dir()
            .map_err(|error| format!("cannot locate browser storage: {error}"))?
            .join("browser-profiles");
        let profile = if identity_partition == DEFAULT_IDENTITY_PARTITION {
            profile_root.join(project_key_hex(&project_id))
        } else {
            profile_root
                .join("identities")
                .join(project_key_hex(&project_id))
                .join(identity_key_hex(&project_id, &identity_partition))
        };
        if profile.exists() {
            fs::remove_dir_all(profile)
                .map_err(|error| format!("cannot clear browser identity storage: {error}"))?;
        }
    }
    Ok(())
}

#[tauri::command]
pub(crate) fn browser_clear_project_data(
    app: AppHandle,
    state: State<'_, BrowserState>,
    project_id: String,
) -> Result<(), String> {
    if !valid_identifier(&project_id) {
        return Err("The Project identifier is invalid.".to_string());
    }
    let tabs: Vec<(String, String)> = state
        .tabs
        .lock()
        .map_err(|_| "Browser state is unavailable.".to_string())?
        .iter()
        .filter(|(_, tab)| tab.project_id == project_id)
        .map(|(id, tab)| (id.clone(), tab.label.clone()))
        .collect();
    for (_, label) in &tabs {
        if let Some(webview) = app.get_webview(label) {
            webview
                .clear_all_browsing_data()
                .map_err(|error| format!("cannot clear browser storage: {error}"))?;
        }
    }
    for (id, _) in tabs {
        close_tab_internal(&app, &state, &id)?;
    }
    let profile = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate browser storage: {error}"))?
        .join("browser-profiles")
        .join(project_key_hex(&project_id));
    if profile.exists() {
        fs::remove_dir_all(profile)
            .map_err(|error| format!("cannot clear browser storage: {error}"))?;
    }
    let identity_profiles = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate browser storage: {error}"))?
        .join("browser-profiles")
        .join("identities")
        .join(project_key_hex(&project_id));
    if identity_profiles.exists() {
        fs::remove_dir_all(identity_profiles)
            .map_err(|error| format!("cannot clear browser identity storage: {error}"))?;
    }
    Ok(())
}

fn percent_encode(value: &str) -> String {
    value
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

fn upload_staged(
    session: &crate::sidecar::BackendSession,
    project_id: &str,
    destination: &str,
    overwrite: bool,
    path: &Path,
) -> Result<BrowserImportResult, String> {
    let metadata = fs::metadata(path)
        .map_err(|_| "The staged download is no longer available.".to_string())?;
    if metadata.len() > MAX_DOWNLOAD_BYTES {
        return Err("The staged download exceeds the 1 GiB file limit.".to_string());
    }
    let endpoint = Url::parse(&session.endpoint)
        .map_err(|_| "Nebula Core returned an invalid endpoint.".to_string())?;
    if endpoint.host_str() != Some("127.0.0.1") {
        return Err("Refusing a non-loopback Core endpoint.".to_string());
    }
    let port = endpoint
        .port()
        .ok_or_else(|| "Nebula Core did not provide a port.".to_string())?;
    let request_path = format!(
        "{}/engagements/{}/workspace/file?path={}&overwrite={}",
        endpoint.path().trim_end_matches('/'),
        percent_encode(project_id),
        percent_encode(destination),
        overwrite
    );
    let mut stream = TcpStream::connect_timeout(
        &SocketAddrV4::new(Ipv4Addr::LOCALHOST, port).into(),
        Duration::from_secs(3),
    )
    .map_err(|error| format!("cannot connect to Nebula Core: {error}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(30)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_secs(300))))
        .map_err(|error| format!("cannot configure the Core connection: {error}"))?;
    write!(stream, "PUT {request_path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {}\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", session.token, metadata.len())
        .map_err(|error| format!("cannot start the workspace import: {error}"))?;
    let mut file =
        File::open(path).map_err(|error| format!("cannot read the staged download: {error}"))?;
    std::io::copy(&mut file, &mut stream)
        .map_err(|error| format!("cannot stream the download to Core: {error}"))?;
    stream
        .flush()
        .map_err(|error| format!("cannot finish the workspace import: {error}"))?;
    let mut response = Vec::new();
    stream
        .take(MAX_RESPONSE_BYTES + 1)
        .read_to_end(&mut response)
        .map_err(|error| format!("cannot read the Core response: {error}"))?;
    if response.len() as u64 > MAX_RESPONSE_BYTES {
        return Err("Nebula Core returned an oversized response.".to_string());
    }
    let response = String::from_utf8(response)
        .map_err(|_| "Nebula Core returned an invalid response.".to_string())?;
    let (headers, body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "Nebula Core returned a malformed response.".to_string())?;
    let status = headers
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().nth(1))
        .unwrap_or("");
    if status == "409" {
        return Ok(BrowserImportResult {
            state: "conflict",
            path: destination.to_string(),
            size: metadata.len(),
            sha256: None,
            overwritten: false,
            detail: Some("A Project file with this name already exists.".to_string()),
        });
    }
    if status != "201" {
        return Err(format!(
            "Nebula Core rejected the download import (HTTP {status})."
        ));
    }
    let value: serde_json::Value = serde_json::from_str(body)
        .map_err(|_| "Nebula Core returned malformed import JSON.".to_string())?;
    Ok(BrowserImportResult {
        state: "imported",
        path: value
            .get("path")
            .and_then(|item| item.as_str())
            .unwrap_or(destination)
            .to_string(),
        size: value
            .get("size")
            .and_then(|item| item.as_u64())
            .unwrap_or(metadata.len()),
        sha256: value
            .get("sha256")
            .and_then(|item| item.as_str())
            .map(str::to_string),
        overwritten: value
            .get("overwritten")
            .and_then(|item| item.as_bool())
            .unwrap_or(overwrite),
        detail: None,
    })
}

#[tauri::command]
pub(crate) async fn browser_import_download(
    state: State<'_, BrowserState>,
    backend: State<'_, BackendState>,
    download_id: String,
    project_id: String,
    overwrite: bool,
) -> Result<BrowserImportResult, String> {
    let path = state
        .downloads
        .lock()
        .map_err(|_| "Browser download state is unavailable.".to_string())?
        .iter()
        .find(|(_, item)| item.id == download_id && item.project_id == project_id)
        .map(|(path, _)| path.clone())
        .ok_or_else(|| "The staged download is no longer available.".to_string())?;
    let filename = state
        .downloads
        .lock()
        .map_err(|_| "Browser download state is unavailable.".to_string())?
        .get(&path)
        .map(|item| item.filename.clone())
        .ok_or_else(|| "The staged download is no longer available.".to_string())?;
    let session = backend.active_session()?;
    let upload_path = path.clone();
    let upload_project = project_id.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        upload_staged(
            &session,
            &upload_project,
            &filename,
            overwrite,
            &upload_path,
        )
    })
    .await
    .map_err(|error| format!("The browser download import stopped unexpectedly: {error}"))??;
    if result.state == "imported" {
        state
            .downloads
            .lock()
            .map_err(|_| "Browser download state is unavailable.".to_string())?
            .remove(&path);
        fs::remove_file(path)
            .map_err(|error| format!("cannot remove imported staged download: {error}"))?;
    }
    Ok(result)
}

#[tauri::command]
pub(crate) fn browser_discard_download(
    state: State<'_, BrowserState>,
    download_id: String,
    project_id: String,
) -> Result<(), String> {
    let path = state
        .downloads
        .lock()
        .map_err(|_| "Browser download state is unavailable.".to_string())?
        .iter()
        .find(|(_, item)| item.id == download_id && item.project_id == project_id)
        .map(|(path, _)| path.clone())
        .ok_or_else(|| "The staged download is no longer available.".to_string())?;
    state
        .downloads
        .lock()
        .map_err(|_| "Browser download state is unavailable.".to_string())?
        .remove(&path);
    fs::remove_file(path)
        .map_err(|error| format!("cannot remove discarded staged download: {error}"))?;
    Ok(())
}

pub(crate) fn initialize(app: &AppHandle) -> Result<(), String> {
    let staging = app
        .path()
        .app_cache_dir()
        .map_err(|error| format!("cannot locate browser cache: {error}"))?
        .join("browser-downloads");
    if staging.exists() {
        fs::remove_dir_all(&staging)
            .map_err(|error| format!("cannot clear stale browser downloads: {error}"))?;
    }
    fs::create_dir_all(staging)
        .map_err(|error| format!("cannot prepare browser downloads: {error}"))
}

pub(crate) fn shutdown(app: &AppHandle) {
    if let Ok(staging) = app.path().app_cache_dir() {
        let downloads = staging.join("browser-downloads");
        if let Err(error) = fs::remove_dir_all(downloads)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            record_browser_failure(
                app,
                "desktop.browser.shutdown_cleanup_failed",
                "Staged browser downloads could not be removed during shutdown.",
                "shutdown-cleanup",
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn accepts_only_network_urls_without_embedded_credentials() {
        assert!(validated_url("https://example.test/path").is_ok());
        assert!(validated_url("http://127.0.0.1:8000/").is_ok());
        assert!(validated_url("file:///etc/passwd").is_err());
        assert!(validated_url("javascript:alert(1)").is_err());
        assert!(validated_url("https://user:secret@example.test/").is_err());
    }

    #[test]
    fn live_page_context_is_bounded_and_contains_no_form_values() {
        let raw = serde_json::json!({
            "url": "https://app.example.test/login",
            "title": "Sign in",
            "selectedText": "csrf token rotates",
            "text": "x".repeat(MAX_CAPTURE_TEXT_CHARS + 50),
            "truncated": false,
            "forms": [{
                "method": "post",
                "action": "https://app.example.test/session",
                "fields": [{
                    "name": "password",
                    "id": "password",
                    "type": "PASSWORD",
                    "autocomplete": "current-password",
                    "required": true,
                    "value": "never-capture-this"
                }]
            }],
            "links": [{"text": "Reset", "href": "https://app.example.test/reset"}]
        })
        .to_string();

        let context = decode_browser_context(&raw).unwrap();

        assert_eq!(context.url, "https://app.example.test/login");
        assert_eq!(context.text.chars().count(), MAX_CAPTURE_TEXT_CHARS);
        assert!(context.truncated);
        assert_eq!(context.forms[0].method, "POST");
        assert_eq!(context.forms[0].fields[0].r#type, "password");
        let serialized = serde_json::to_string(&context).unwrap();
        assert!(!serialized.contains("never-capture-this"));
        assert!(!serialized.contains("\"value\""));
    }

    #[test]
    fn live_page_context_rejects_non_network_or_credentialed_provenance() {
        for url in ["file:///etc/passwd", "https://user:secret@example.test/"] {
            let raw = serde_json::json!({"url": url, "text": "page"}).to_string();
            assert!(decode_browser_context(&raw).is_err());
        }
    }

    #[test]
    fn live_page_context_rejects_an_oversized_raw_snapshot_before_decoding() {
        let raw = "x".repeat(MAX_CAPTURE_RAW_BYTES + 1);
        assert_eq!(
            decode_browser_context(&raw).unwrap_err(),
            "The live page context snapshot exceeded the safe size limit."
        );
    }

    #[test]
    fn profile_keys_are_stable_and_project_specific() {
        assert_eq!(project_key("one"), project_key("one"));
        assert_ne!(project_key("one"), project_key("two"));
    }

    #[test]
    fn filenames_cannot_escape_the_workspace() {
        assert_eq!(safe_filename("../../report.txt"), "report.txt");
        assert_eq!(safe_filename("bad:name\0.txt"), "bad_name_.txt");
        assert_eq!(safe_filename(".."), "download.bin");
    }

    #[test]
    fn bounds_reject_negative_or_unbounded_surfaces() {
        assert!(
            checked_bounds(BrowserBounds {
                x: 1.0,
                y: 2.0,
                width: 900.0,
                height: 600.0,
            })
            .is_ok()
        );
        assert!(
            checked_bounds(BrowserBounds {
                x: -1.0,
                y: 2.0,
                width: 900.0,
                height: 600.0,
            })
            .is_err()
        );
        assert!(
            checked_bounds(BrowserBounds {
                x: 1.0,
                y: 2.0,
                width: 99_000.0,
                height: 600.0,
            })
            .is_err()
        );
    }

    #[test]
    fn browser_bounds_remain_logical_at_high_density() {
        let bounds = checked_bounds(BrowserBounds {
            x: 12.5,
            y: 86.0,
            width: 900.0,
            height: 600.0,
        })
        .unwrap();
        assert_eq!(bounds.x, 12.5);
        assert_eq!(bounds.y, 86.0);
        assert_eq!(bounds.width, 900.0);
        assert_eq!(bounds.height, 600.0);
    }

    #[test]
    fn macos_browser_frame_uses_the_main_webview_coordinate_space() {
        assert_eq!(appkit_child_y(0.0, 900.0, 120.0, 600.0, true), 120.0);
        assert_eq!(appkit_child_y(0.0, 900.0, 120.0, 600.0, false), 180.0);
        assert_eq!(appkit_child_y(12.0, 900.0, 120.0, 600.0, false), 192.0);
    }

    #[test]
    fn macos_safe_area_offset_keeps_the_browser_bottom_fixed() {
        let bounds = inset_browser_bounds(
            BrowserBounds {
                x: 20.0,
                y: 100.0,
                width: 900.0,
                height: 600.0,
            },
            28.0,
        );
        assert_eq!(bounds.y, 128.0);
        assert_eq!(bounds.height, 572.0);
        assert_eq!(bounds.y + bounds.height, 700.0);
    }

    #[test]
    fn staged_download_streams_through_the_authenticated_workspace_endpoint() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let server = std::thread::spawn(move || {
            let (mut socket, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0_u8; 4096];
            loop {
                let read = socket.read(&mut buffer).unwrap();
                request.extend_from_slice(&buffer[..read]);
                let Some(header_end) = request.windows(4).position(|item| item == b"\r\n\r\n")
                else {
                    continue;
                };
                let headers = String::from_utf8_lossy(&request[..header_end]);
                let length = headers
                    .lines()
                    .find_map(|line| line.strip_prefix("Content-Length: "))
                    .unwrap()
                    .parse::<usize>()
                    .unwrap();
                if request.len() >= header_end + 4 + length {
                    break;
                }
            }
            let text = String::from_utf8_lossy(&request);
            assert!(text.starts_with("PUT /api/v1/engagements/project-1/workspace/file?path=report.txt&overwrite=false HTTP/1.1"));
            assert!(text.contains("Authorization: Bearer private-token\r\n"));
            assert!(request.ends_with(b"download body"));
            let body = r#"{"path":"report.txt","size":13,"sha256":"abc123","overwritten":false}"#;
            write!(
                socket,
                "HTTP/1.1 201 Created\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .unwrap();
        });
        let path = std::env::temp_dir().join(format!(
            "nebula-browser-test-{}",
            random_id("file").unwrap()
        ));
        fs::write(&path, b"download body").unwrap();
        let session = crate::sidecar::BackendSession {
            endpoint: format!("http://127.0.0.1:{port}/api/v1"),
            token: "private-token".to_string(),
            protocol: "nebula-sidecar-v1",
        };

        let result = upload_staged(&session, "project-1", "report.txt", false, &path).unwrap();

        assert_eq!(result.state, "imported");
        assert_eq!(result.path, "report.txt");
        assert_eq!(result.sha256.as_deref(), Some("abc123"));
        server.join().unwrap();
        fs::remove_file(path).unwrap();
    }

    #[test]
    fn oversized_staged_download_is_rejected_before_connecting_to_core() {
        let path = std::env::temp_dir().join(format!(
            "nebula-browser-large-{}",
            random_id("file").unwrap()
        ));
        let file = File::create(&path).unwrap();
        file.set_len(MAX_DOWNLOAD_BYTES + 1).unwrap();
        let session = crate::sidecar::BackendSession {
            endpoint: "http://127.0.0.1:9/api/v1".to_string(),
            token: "private-token".to_string(),
            protocol: "nebula-sidecar-v1",
        };

        let error = upload_staged(&session, "project-1", "large.bin", false, &path).unwrap_err();

        assert!(error.contains("exceeds the 1 GiB"));
        fs::remove_file(path).unwrap();
    }
}
