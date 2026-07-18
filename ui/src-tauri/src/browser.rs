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

#[derive(Default)]
pub(crate) struct BrowserState {
    tabs: Mutex<HashMap<String, BrowserTab>>,
    downloads: Mutex<HashMap<PathBuf, PendingDownload>>,
}

struct BrowserTab {
    project_id: String,
    label: String,
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
    }
    Ok(())
}

#[tauri::command]
pub(crate) fn browser_create_tab(
    app: AppHandle,
    state: State<'_, BrowserState>,
    tab_id: String,
    project_id: String,
    url: String,
    bounds: BrowserBounds,
) -> Result<(), String> {
    if !valid_identifier(&tab_id) || !valid_identifier(&project_id) {
        return Err("The browser tab or Project identifier is invalid.".to_string());
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
    let profile_dir = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate browser storage: {error}"))?
        .join("browser-profiles")
        .join(project_key_hex(&project_id));
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
        .devtools(false);

    #[cfg(target_os = "macos")]
    {
        if macos_supports_project_store() {
            builder = builder.data_store_identifier(project_key(&project_id));
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
        .insert(tab_id, BrowserTab { project_id, label });
    Ok(())
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
