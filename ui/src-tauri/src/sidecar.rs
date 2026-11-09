use std::{
    fs::{self, File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpStream},
    path::{Path, PathBuf},
    process::{Child, ChildStderr, Command, Stdio},
    sync::{Mutex, mpsc, atomic::{AtomicBool, Ordering}},
    thread::JoinHandle,
    time::{Duration, Instant},
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

use crate::diagnostics::{DiagnosticLevel, DiagnosticsState, ERROR_MIRROR_PREFIX};

const SIDECAR_PROTOCOL: &str = "nebula-desktop-runtime-v1";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(50);
const HEALTH_IO_TIMEOUT: Duration = Duration::from_secs(2);
const TERMINATION_GRACE_PERIOD: Duration = Duration::from_secs(5);
const MAX_HANDSHAKE_BYTES: u64 = 16 * 1024;
const MAX_HEALTH_RESPONSE_BYTES: u64 = 64 * 1024;
const MAX_STARTUP_LOG_BYTES: usize = 256 * 1024;
const MAX_PENDING_LOG_BYTES: usize = 16 * 1024;
const STARTUP_LOG_NAME: &str = "nebula-core-startup.log";
const PLAYWRIGHT_RESOURCE_DIRECTORY: &str = "playwright-browsers";
const NOTEBOOK_RESOURCE_DIRECTORY: &str = "notebook-runtime";
const PLAYWRIGHT_RESOURCE_MANIFEST: &str = "nebula-playwright-runtime.json";
const SECURITY_PROXY_ADDON: &str = "security-proxy/addon.py";
const LOG_TRUNCATED_MARKER: &[u8] = b"\n[nebula] startup log truncated at 256 KiB\n";
const TRUSTED_SYSTEM_PATH: &str =
    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin";
const SIDECAR_ENV_ALLOWLIST: &[&str] = &[
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_CONFIG_FILE",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AZURE_AI_API_KEY",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
    "AZURE_OPENAI_API_KEY",
    "AZURE_TENANT_ID",
    "COHERE_API_KEY",
    "CONTAINER_HOST",
    "CREDENTIALS_DIRECTORY",
    "DEEPSEEK_API_KEY",
    "DOCKER_HOST",
    "DISPLAY",
    "FIREWORKS_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_ACCESS_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GROQ_API_KEY",
    "HF_TOKEN",
    "HOME",
    "LANG",
    "LC_ALL",
    "MISTRAL_API_KEY",
    "NEBULA_CALLBACK_ORIGIN",
    "NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE",
    "NEBULA_KALI_SOURCE_IMAGE",
    "NEBULA_PLAYWRIGHT_EXECUTABLE",
    "NEBULA_MITMDUMP_EXECUTABLE",
    "NEBULA_SECURITY_PROXY_ADDON",
    "NEBULA_V3_CONTAINER_RUNTIME",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ORCAROUTER_API_KEY",
    "TOGETHER_API_KEY",
    "TZ",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XAI_API_KEY",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
];

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BackendSession {
    pub(crate) endpoint: String,
    pub(crate) token: String,
    pub(crate) protocol: &'static str,
    pub(crate) source: &'static str,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BackendStatus {
    state: &'static str,
    endpoint: Option<String>,
    message: Option<String>,
}

#[derive(Debug, Serialize)]
struct BootstrapInput<'a> {
    protocol: &'static str,
    ipc_token: &'a str,
}

#[derive(Debug, Deserialize)]
struct SidecarHandshake {
    protocol: String,
    host: String,
    port: u16,
    ipc_token: String,
}

#[derive(Debug, Deserialize)]
struct HealthResponse {
    status: String,
    version: String,
    commit: String,
    target: String,
    build_timestamp: String,
    distribution_channel: String,
    mode: String,
    database: String,
    api_version: String,
    schema_version: u64,
    #[serde(default)]
    journal_mode: String,
}

struct StartupLogCapture {
    path: PathBuf,
    thread: Option<JoinHandle<Result<(), String>>>,
}

impl StartupLogCapture {
    fn finish(&mut self) -> Result<(), String> {
        let Some(thread) = self.thread.take() else {
            return Ok(());
        };
        thread
            .join()
            .map_err(|_| "Nebula Core diagnostic capture stopped unexpectedly".to_string())?
    }
}

struct ManagedBackend {
    session: BackendSession,
    port: u16,
    data_dir: PathBuf,
}

#[derive(Default)]
pub(crate) struct BackendState {
    process: Mutex<Option<ManagedBackend>>,
    operator_stopped: AtomicBool,
}

impl BackendState {
    pub(crate) fn active_session(&self) -> Result<BackendSession, String> {
        let mut process = self
            .process
            .lock()
            .map_err(|_| "the Nebula Core supervisor is unavailable".to_string())?;
        let managed = process
            .as_mut()
            .ok_or_else(|| "Nebula Core is not running".to_string())?;
        let health = request_authenticated_health(managed.port, &managed.session.token, HEALTH_IO_TIMEOUT)?;
        verify_health(&health)?;
        Ok(managed.session.clone())
    }
}

fn sibling_sidecar_path() -> Result<PathBuf, String> {
    let current_executable = std::env::current_exe()
        .map_err(|error| format!("cannot locate the Nebula desktop executable: {error}"))?;
    let executable_dir = current_executable
        .parent()
        .ok_or_else(|| "the Nebula desktop executable has no parent directory".to_string())?
        .canonicalize()
        .map_err(|error| format!("cannot resolve the Nebula application directory: {error}"))?;

    #[cfg(target_os = "windows")]
    let sidecar_name = "nebula-core.exe";
    #[cfg(not(target_os = "windows"))]
    let sidecar_name = "nebula-core";

    let candidate = executable_dir.join(sidecar_name);
    let resolved = candidate.canonicalize().map_err(|_| {
        format!(
            "Nebula Core is not installed at the fixed sidecar path {}",
            candidate.display()
        )
    })?;

    if resolved.parent() != Some(executable_dir.as_path()) || !resolved.starts_with(&executable_dir)
    {
        return Err("refusing to launch a sidecar outside the application directory".to_string());
    }
    if !resolved.is_file() {
        return Err("the configured Nebula Core sidecar is not a file".to_string());
    }
    Ok(resolved)
}

fn bundled_playwright_browsers_path(resource_dir: &Path) -> Result<Option<PathBuf>, String> {
    let candidate = resource_dir.join(PLAYWRIGHT_RESOURCE_DIRECTORY);
    if !candidate.join(PLAYWRIGHT_RESOURCE_MANIFEST).is_file() {
        return Ok(None);
    }
    let resolved_resource_dir = resource_dir
        .canonicalize()
        .map_err(|error| format!("cannot resolve the Nebula resource directory: {error}"))?;
    let resolved = candidate
        .canonicalize()
        .map_err(|error| format!("cannot resolve the bundled Playwright runtime: {error}"))?;
    if !resolved.is_dir() || !resolved.starts_with(&resolved_resource_dir) {
        return Err(
            "refusing to use a bundled Playwright runtime outside the application resources"
                .to_string(),
        );
    }
    Ok(Some(resolved))
}

fn bundled_security_proxy_addon_path(resource_dir: &Path) -> Result<Option<PathBuf>, String> {
    let candidate = resource_dir.join(SECURITY_PROXY_ADDON);
    if !candidate.is_file() {
        return Ok(None);
    }
    let root = resource_dir
        .canonicalize()
        .map_err(|error| format!("cannot resolve the Nebula resource directory: {error}"))?;
    let addon = candidate
        .canonicalize()
        .map_err(|error| format!("cannot resolve the bundled security proxy addon: {error}"))?;
    if !addon.starts_with(root) {
        return Err(
            "refusing to use a security proxy addon outside application resources".to_string(),
        );
    }
    Ok(Some(addon))
}

fn trusted_mitmdump_path() -> Option<PathBuf> {
    TRUSTED_SYSTEM_PATH.split(':').find_map(|directory| {
        let candidate = Path::new(directory).join(if cfg!(windows) {
            "mitmdump.exe"
        } else {
            "mitmdump"
        });
        candidate
            .is_file()
            .then(|| candidate.canonicalize().ok())
            .flatten()
    })
}

fn secure_token() -> Result<String, String> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|error| format!("cannot create an IPC token: {error}"))?;
    Ok(URL_SAFE_NO_PAD.encode(bytes))
}

#[cfg(unix)]
fn signal_process_group(process_group: u32, signal: i32) -> Result<bool, String> {
    let process_group = i32::try_from(process_group)
        .map_err(|_| "Nebula Core returned an invalid process identifier".to_string())?;
    // The sidecar is placed in a dedicated process group before it starts. A
    // negative PID addresses that complete group, including PyInstaller's
    // extracted child process rather than only its bootloader parent.
    let result = unsafe { libc::kill(-process_group, signal) };
    if result == 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        return Ok(false);
    }
    Err(format!(
        "cannot signal the Nebula Core process group: {error}"
    ))
}

#[cfg(unix)]
fn process_group_exists(process_group: u32) -> Result<bool, String> {
    signal_process_group(process_group, 0)
}

#[cfg(unix)]
fn terminate(child: &mut Child) -> Result<(), String> {
    let process_group = child.id();
    match child.try_wait() {
        Ok(Some(_)) => return Ok(()),
        Ok(None) => {}
        Err(error) => {
            return Err(format!(
                "cannot inspect Nebula Core before terminating it: {error}"
            ));
        }
    }
    if let Err(signal_error) = signal_process_group(process_group, libc::SIGTERM) {
        return match child.try_wait() {
            Ok(Some(_)) => Ok(()),
            Ok(None) => Err(signal_error),
            Err(error) => Err(format!(
                "{signal_error}; cannot inspect Nebula Core after signaling it: {error}"
            )),
        };
    }
    let deadline = Instant::now() + TERMINATION_GRACE_PERIOD;

    loop {
        // try_wait reaps the direct PyInstaller bootloader when it exits. Its
        // extracted Python child may live slightly longer, so group liveness is
        // checked separately before the stderr capture thread is joined.
        child
            .try_wait()
            .map_err(|error| format!("cannot inspect Nebula Core while terminating it: {error}"))?;
        if !process_group_exists(process_group)? {
            break;
        }
        if Instant::now() >= deadline {
            signal_process_group(process_group, libc::SIGKILL)?;
            break;
        }
        std::thread::sleep(STARTUP_POLL_INTERVAL);
    }

    child
        .wait()
        .map(|_| ())
        .map_err(|error| format!("cannot reap Nebula Core after termination: {error}"))
}

#[cfg(not(unix))]
fn terminate(child: &mut Child) -> Result<(), String> {
    match child.try_wait() {
        Ok(Some(_)) => return Ok(()),
        Ok(None) => {}
        Err(error) => {
            return Err(format!(
                "cannot inspect Nebula Core before terminating it: {error}"
            ));
        }
    }
    child
        .kill()
        .map_err(|error| format!("cannot terminate Nebula Core: {error}"))?;
    child
        .wait()
        .map(|_| ())
        .map_err(|error| format!("cannot reap Nebula Core after termination: {error}"))
}

fn wait_for_child_exit(child: &mut Child, deadline: Instant) -> Result<bool, String> {
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return Ok(true),
            Ok(None) => {}
            Err(error) => {
                return Err(format!(
                    "cannot inspect Nebula Core during graceful shutdown: {error}"
                ));
            }
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(false);
        }
        std::thread::sleep(remaining.min(STARTUP_POLL_INTERVAL));
    }
}


fn read_handshake(
    child: &mut Child,
    deadline: Instant,
) -> Result<(SidecarHandshake, JoinHandle<Result<(), String>>), String> {
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Nebula Core did not expose its handshake stream".to_string())?;
    let (sender, receiver) = mpsc::sync_channel(1);
    let output_thread = std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let result = reader
            .by_ref()
            .take(MAX_HANDSHAKE_BYTES)
            .read_line(&mut line)
            .and_then(|read| {
                if read == 0 {
                    Err(io::Error::new(
                        io::ErrorKind::UnexpectedEof,
                        "the handshake stream closed before a frame was received",
                    ))
                } else {
                    Ok(line)
                }
            });
        if sender.send(result).is_err() {
            return Ok(());
        }
        io::copy(&mut reader, &mut io::sink())
            .map(|_| ())
            .map_err(|error| format!("cannot drain Nebula Core output: {error}"))
    });

    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(
                "Nebula Core did not complete its loopback handshake within 60 seconds".to_string(),
            );
        }
        match receiver.recv_timeout(remaining.min(STARTUP_POLL_INTERVAL)) {
            Ok(Ok(line)) => {
                if line.len() as u64 >= MAX_HANDSHAKE_BYTES || !line.ends_with('\n') {
                    return Err("Nebula Core returned an invalid handshake frame".to_string());
                }
                let handshake = serde_json::from_str(&line)
                    .map_err(|_| "Nebula Core returned malformed handshake JSON".to_string())?;
                return Ok((handshake, output_thread));
            }
            Ok(Err(error)) => {
                return Err(format!("cannot read the Nebula Core handshake: {error}"));
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err("Nebula Core closed its handshake stream unexpectedly".to_string());
            }
            Err(mpsc::RecvTimeoutError::Timeout) => match child.try_wait() {
                Ok(Some(status)) if status.success() => {}
                Ok(Some(status)) => {
                    return Err(format!(
                        "Nebula Core exited with {status} before completing its startup handshake"
                    ));
                }
                Ok(None) => {}
                Err(error) => {
                    return Err(format!(
                        "cannot inspect Nebula Core during its startup handshake: {error}"
                    ));
                }
            },
        }
    }
}

fn preserve_startup_log(input: &[u8]) -> Vec<u8> {
    input.to_vec()
}

fn process_startup_line(input: &[u8], diagnostics: &DiagnosticsState) -> Vec<u8> {
    let text = String::from_utf8_lossy(input);
    if let Some(frame) = text.trim_end().strip_prefix(ERROR_MIRROR_PREFIX) {
        match diagnostics.aggregate_core_error(frame) {
            Ok(validated) => {
                let newline = if input.ends_with(b"\n") { "\n" } else { "" };
                return format!("{ERROR_MIRROR_PREFIX}{validated}{newline}").into_bytes();
            }
            Err(_) => {
                diagnostics.record_desktop(
                    DiagnosticLevel::Error,
                    "desktop.core_error_aggregation.failed",
                    "A Core error frame could not be validated for the aggregate error log.",
                    Some("failure"),
                    Some("stderr-aggregation"),
                    Some(true),
                    serde_json::Map::new(),
                );
            }
        }
    }
    preserve_startup_log(input)
}

fn write_bounded_log(
    file: &mut File,
    fragment: &[u8],
    written: &mut usize,
    truncated: &mut bool,
) -> io::Result<()> {
    if *truncated {
        return Ok(());
    }
    let content_limit = MAX_STARTUP_LOG_BYTES - LOG_TRUNCATED_MARKER.len();
    let available = content_limit.saturating_sub(*written);
    let to_write = available.min(fragment.len());
    file.write_all(&fragment[..to_write])?;
    *written += to_write;
    if to_write < fragment.len() {
        file.write_all(LOG_TRUNCATED_MARKER)?;
        *written += LOG_TRUNCATED_MARKER.len();
        *truncated = true;
    }
    Ok(())
}

fn capture_startup_log(
    mut stderr: ChildStderr,
    mut file: File,
    diagnostics: DiagnosticsState,
) -> Result<(), String> {
    let mut written = 0;
    let mut truncated = false;
    write_bounded_log(
        &mut file,
        b"[nebula] Nebula Core startup diagnostics\n",
        &mut written,
        &mut truncated,
    )
    .map_err(|error| format!("cannot write Nebula Core startup diagnostics: {error}"))?;

    let mut buffer = [0_u8; 4096];
    let mut pending = Vec::new();
    loop {
        let read = stderr
            .read(&mut buffer)
            .map_err(|error| format!("cannot read Nebula Core startup diagnostics: {error}"))?;
        if read == 0 {
            break;
        }
        pending.extend_from_slice(&buffer[..read]);

        while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
            let line: Vec<u8> = pending.drain(..=newline).collect();
            let line = process_startup_line(&line, &diagnostics);
            write_bounded_log(&mut file, &line, &mut written, &mut truncated).map_err(|error| {
                format!("cannot write Nebula Core startup diagnostics: {error}")
            })?;
        }

        if pending.len() > MAX_PENDING_LOG_BYTES {
            let flush_len = pending.len();
            let fragment: Vec<u8> = pending.drain(..flush_len).collect();
            let fragment = process_startup_line(&fragment, &diagnostics);
            write_bounded_log(&mut file, &fragment, &mut written, &mut truncated).map_err(
                |error| format!("cannot write Nebula Core startup diagnostics: {error}"),
            )?;
        }
    }

    if !pending.is_empty() {
        let fragment = process_startup_line(&pending, &diagnostics);
        write_bounded_log(&mut file, &fragment, &mut written, &mut truncated)
            .map_err(|error| format!("cannot write Nebula Core startup diagnostics: {error}"))?;
    }
    file.flush()
        .map_err(|error| format!("cannot flush Nebula Core startup diagnostics: {error}"))
}

fn open_startup_log(path: &std::path::Path) -> Result<File, String> {
    let parent = path
        .parent()
        .ok_or_else(|| "the Nebula Core startup log has no parent directory".to_string())?;
    fs::create_dir_all(parent).map_err(|error| {
        format!(
            "cannot prepare the Nebula Core diagnostics directory {}: {error}",
            parent.display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700)).map_err(|error| {
            format!(
                "cannot secure the Nebula Core diagnostics directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let oldest = path.with_file_name(format!(
        "{}.2",
        path.file_name().unwrap_or_default().to_string_lossy()
    ));
    if oldest.exists() {
        fs::remove_file(&oldest)
            .map_err(|error| format!("cannot prune the oldest Core startup log: {error}"))?;
    }
    let first = path.with_file_name(format!(
        "{}.1",
        path.file_name().unwrap_or_default().to_string_lossy()
    ));
    if first.exists() {
        fs::rename(&first, &oldest)
            .map_err(|error| format!("cannot advance the Core startup log rotation: {error}"))?;
    }
    if path.exists() {
        fs::rename(path, &first)
            .map_err(|error| format!("cannot rotate the Core startup log: {error}"))?;
    }
    let mut options = OpenOptions::new();
    options.create(true).truncate(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = options.open(path).map_err(|error| {
        format!(
            "cannot open the Nebula Core startup log {}: {error}",
            path.display()
        )
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(fs::Permissions::from_mode(0o600))
            .map_err(|error| {
                format!(
                    "cannot secure the Nebula Core startup log {}: {error}",
                    path.display()
                )
            })?;
    }
    Ok(file)
}

fn verify_writable_storage(data_dir: &std::path::Path) -> Result<(), String> {
    let suffix = secure_token()?;
    let probe = data_dir.join(format!(".nebula-write-test-{}", &suffix[..12]));
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&probe).map_err(|error| {
        format!(
            "the Nebula data directory {} is not writable: {error}",
            data_dir.display()
        )
    })?;
    let write_result = file
        .write_all(b"nebula-storage-self-test\n")
        .and_then(|_| file.flush());
    drop(file);
    let remove_result = fs::remove_file(&probe);
    if let Err(error) = write_result {
        return match remove_result {
            Ok(()) => Err(format!(
                "cannot write to the Nebula data directory {}: {error}",
                data_dir.display()
            )),
            Err(cleanup_error) => Err(format!(
                "cannot write to the Nebula data directory {}: {error}; storage probe cleanup also failed: {cleanup_error}",
                data_dir.display()
            )),
        };
    }
    remove_result.map_err(|error| {
        format!(
            "cannot clean the Nebula storage probe in {}: {error}",
            data_dir.display()
        )
    })
}

fn parse_health_response(response: &[u8]) -> Result<HealthResponse, String> {
    let response = std::str::from_utf8(response)
        .map_err(|_| "Nebula Core returned a non-UTF-8 health response".to_string())?;
    let (headers, body) = response
        .split_once("\r\n\r\n")
        .ok_or_else(|| "Nebula Core returned a malformed HTTP health response".to_string())?;
    let status_line = headers
        .lines()
        .next()
        .ok_or_else(|| "Nebula Core returned an empty HTTP health response".to_string())?;
    let mut status_parts = status_line.split_whitespace();
    let protocol = status_parts.next().unwrap_or_default();
    let status = status_parts.next().unwrap_or_default();
    if !protocol.starts_with("HTTP/1.") || status != "200" {
        return Err(format!(
            "Nebula Core health check returned an unexpected status: {status_line}"
        ));
    }
    serde_json::from_str(body).map_err(|_| "Nebula Core returned malformed health JSON".to_string())
}

fn request_authenticated_health(
    port: u16,
    ipc_token: &str,
    timeout: Duration,
) -> Result<HealthResponse, String> {
    let address = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let connect_timeout = timeout.min(Duration::from_millis(500));
    let mut stream = TcpStream::connect_timeout(&address.into(), connect_timeout)
        .map_err(|error| format!("cannot connect to the Nebula Core health endpoint: {error}"))?;
    stream
        .set_read_timeout(Some(timeout))
        .and_then(|_| stream.set_write_timeout(Some(timeout)))
        .map_err(|error| format!("cannot configure the Nebula Core health connection: {error}"))?;

    let request = format!(
        "GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {ipc_token}\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .and_then(|_| stream.flush())
        .map_err(|error| format!("cannot send the Nebula Core health request: {error}"))?;

    let mut response = Vec::new();
    stream
        .take(MAX_HEALTH_RESPONSE_BYTES + 1)
        .read_to_end(&mut response)
        .map_err(|error| format!("cannot read the Nebula Core health response: {error}"))?;
    if response.len() as u64 > MAX_HEALTH_RESPONSE_BYTES {
        return Err("Nebula Core returned an oversized health response".to_string());
    }
    parse_health_response(&response)
}

fn verify_health(health: &HealthResponse) -> Result<(), String> {
    // Desktop and API versions can differ during a supported update.
    if !matches!(health.status.as_str(), "ok" | "degraded" | "starting")
        || health.database != "ok" || health.api_version != "v1"
        || health.schema_version == 0
        || (health.mode == "local" && !health.journal_mode.eq_ignore_ascii_case("wal"))
    {
        return Err("Nebula Core health or storage is unavailable; reconnect after resolving the runtime failure".to_string());
    }
    Ok(())
}

fn startup_failure(
    child: &mut Child,
    startup_log: &mut StartupLogCapture,
    error: String,
) -> String {
    let terminate_error = terminate(child).err();
    let diagnostics = startup_log.path.display().to_string();
    let log_error = startup_log.finish().err();
    match (terminate_error, log_error) {
        (None, None) => format!("{error}; startup diagnostics: {diagnostics}"),
        (Some(cleanup_error), None) => format!(
            "{error}; sidecar cleanup also failed: {cleanup_error}; startup diagnostics: {diagnostics}"
        ),
        (None, Some(log_error)) => format!(
            "{error}; startup diagnostic capture also failed: {log_error}; diagnostic path: {diagnostics}"
        ),
        (Some(cleanup_error), Some(log_error)) => format!(
            "{error}; sidecar cleanup also failed: {cleanup_error}; startup diagnostic capture also failed: {log_error}; diagnostic path: {diagnostics}"
        ),
    }
}

fn launch(app: &AppHandle) -> Result<ManagedBackend, String> {
    let sidecar = sibling_sidecar_path()?;
    let app_data = app.path().app_data_dir().map_err(|error| error.to_string())?;
    let data_dir = std::env::var_os("NEBULA_V3_DATA_DIR")
        .map(PathBuf::from).unwrap_or_else(|| app_data.join("core"));
    fs::create_dir_all(&data_dir).map_err(|error| error.to_string())?;
    let data_dir = fs::canonicalize(&data_dir).map_err(|error| error.to_string())?;
    #[cfg(unix)] {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&data_dir, fs::Permissions::from_mode(0o700)).map_err(|error| error.to_string())?;
    }
    verify_writable_storage(&data_dir)?;
    let log_path = data_dir.join("desktop-connect.log");
    let output = open_startup_log(&log_path)?;
    let resource_dir = app.path().resource_dir().map_err(|error| error.to_string())?;
    let mut command = Command::new(sidecar);
    command.env_clear().env("PATH", TRUSTED_SYSTEM_PATH)
        .env("NEBULA_V3_LOG_DIR", app_data.join("logs"))
        .env("NEBULA_V3_DIAGNOSTICS_SETTINGS", app_data.join("diagnostics-settings.json"))
        .env("NEBULA_V3_DIAGNOSTICS_PARENT", "runtime")
        .args(["desktop-connect", "--bootstrap-stdin", "--data-dir"])
        .arg(&data_dir).current_dir(&data_dir)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    for name in SIDECAR_ENV_ALLOWLIST {
        if let Some(value) = std::env::var_os(name) { command.env(name, value); }
    }
    command.env("NEBULA_RUNTIME_BUNDLE", resource_dir.join("nebula-runtime"));
    #[cfg(unix)] {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(target_os = "windows")]
    if let Some(root) = std::env::var_os("SystemRoot") { command.env("SystemRoot", root); }
    let mut child = command.spawn().map_err(|error| format!("cannot connect to local Core: {error}"))?;
    let stderr = child.stderr.take().ok_or("Core connector has no diagnostic stream")?;
    let diagnostics = DiagnosticsState::clone(&*app.state::<DiagnosticsState>());
    let thread = std::thread::spawn(move || capture_startup_log(stderr, output, diagnostics));
    let mut capture = StartupLogCapture { path: log_path, thread: Some(thread) };
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    let bootstrap = BootstrapInput { protocol: SIDECAR_PROTOCOL, ipc_token: "" };
    let mut input = child.stdin.take().ok_or("Core connector has no bootstrap input")?;
    serde_json::to_writer(&mut input, &bootstrap).map_err(|error| error.to_string())?;
    input.write_all(b"\n").map_err(|error| error.to_string())?;
    drop(input);
    let (handshake, stdout_thread) = read_handshake(&mut child, deadline)
        .map_err(|error| startup_failure(&mut child, &mut capture, error))?;
    if !wait_for_child_exit(&mut child, deadline)? {
        return Err(startup_failure(&mut child, &mut capture, "Core connector did not finish".into()));
    }
    if !child.wait().map_err(|error| error.to_string())?.success() {
        return Err(startup_failure(&mut child, &mut capture, "Core connector failed".into()));
    }
    stdout_thread.join().map_err(|_| "Core connector output stopped unexpectedly")??;
    capture.finish()?;
    if handshake.protocol != SIDECAR_PROTOCOL || handshake.host != "127.0.0.1"
        || handshake.port == 0 || handshake.ipc_token.len() < 32 {
        return Err("Core connector returned an invalid local connection".into());
    }
    let health = request_authenticated_health(handshake.port, &handshake.ipc_token, HEALTH_IO_TIMEOUT)?;
    verify_health(&health)?;
    Ok(ManagedBackend { port: handshake.port, data_dir, session: BackendSession {
        endpoint: format!("http://127.0.0.1:{}/api/v1", handshake.port),
        token: handshake.ipc_token, protocol: SIDECAR_PROTOCOL, source: "local",
    }})
}

/// A self-test attaches without shutting down an existing user's Core.
pub(crate) fn self_test_local_backend(app: &AppHandle) -> Result<(), String> {
    launch(app).map(|_| ())
}

#[tauri::command]
pub(crate) fn start_local_backend(app: AppHandle, state: State<'_, BackendState>,
    _diagnostics: State<'_, DiagnosticsState>) -> Result<BackendSession, String> {
    if state.operator_stopped.load(Ordering::Acquire) {
        return Err("Core is shut down. Reopen the desktop to start it again.".into());
    }
    if let Ok(session) = state.active_session() { return Ok(session); }
    let mut current = state.process.lock().map_err(|_| "Core connection is unavailable")?;
    if state.operator_stopped.load(Ordering::Acquire) {
        return Err("Core is shut down. Reopen the desktop to start it again.".into());
    }
    let managed = launch(&app)?;
    let result = managed.session.clone();
    *current = Some(managed);
    Ok(result)
}

#[tauri::command]
pub(crate) fn backend_status(state: State<'_, BackendState>,
    _diagnostics: State<'_, DiagnosticsState>) -> BackendStatus {
    match state.active_session() {
        Ok(session) => BackendStatus { state: "running", endpoint: Some(session.endpoint), message: None },
        Err(error) => BackendStatus { state: "unavailable", endpoint: None, message: Some(error) },
    }
}

#[tauri::command]
pub(crate) async fn stop_local_backend(_app: AppHandle, state: State<'_, BackendState>,
    _diagnostics: State<'_, DiagnosticsState>) -> Result<(), String> {
    let data_dir = state.process.lock().map_err(|_| "Core connection is unavailable")?
        .as_ref().ok_or("No local Core connection is active")?.data_dir.clone();
    let executable = sibling_sidecar_path()?;
    if state.operator_stopped.swap(true, Ordering::AcqRel) {
        return Err("Core shutdown is already pending or complete.".into());
    }
    let result = tauri::async_runtime::spawn_blocking(move || {
        let output = Command::new(executable).arg("runtime-stop").arg("--data-dir")
            .arg(data_dir).stdin(Stdio::null()).output()
            .map_err(|error| format!("Cannot request Core shutdown: {error}"))?;
        if output.status.success() { return Ok(()); }
        Err(format!("Core shutdown was not verified: {}", String::from_utf8_lossy(&output.stderr)))
    }).await.map_err(|error| error.to_string()).and_then(|result| result);
    if result.is_err() { state.operator_stopped.store(false, Ordering::Release); }
    result
}

/// Closing/restarting the desktop releases only its connection, never Core.
pub(crate) fn stop_managed_backend(state: &State<'_, BackendState>,
    _diagnostics: &DiagnosticsState) -> Result<(), String> {
    state.process.lock().map_err(|_| "Core connection is unavailable")?.take();
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{
        io::{Read, Write},
        net::TcpListener,
        path::Path,
    };

    use super::*;

    #[test]
    fn ipc_tokens_have_256_bits_of_url_safe_entropy() {
        let token = secure_token().expect("token generation should work");
        assert_eq!(token.len(), 43);
        assert!(
            token
                .chars()
                .all(|value| value.is_ascii_alphanumeric() || value == '-' || value == '_')
        );
    }

    #[test]
    fn fixed_sidecar_name_has_no_path_components() {
        let name = if cfg!(target_os = "windows") {
            "nebula-core.exe"
        } else {
            "nebula-core"
        };
        assert_eq!(Path::new(name).components().count(), 1);
    }

    #[test]
    fn bundled_playwright_runtime_requires_a_manifest_inside_resources() {
        let directory = std::env::temp_dir().join(format!(
            "nebula-playwright-resource-test-{}",
            secure_token().expect("token generation should work")
        ));
        let browsers = directory.join(PLAYWRIGHT_RESOURCE_DIRECTORY);
        fs::create_dir_all(&browsers).expect("browser resource should be created");
        assert_eq!(
            bundled_playwright_browsers_path(&directory)
                .expect("an absent manifest should be accepted"),
            None
        );
        fs::write(
            browsers.join(PLAYWRIGHT_RESOURCE_MANIFEST),
            b"{\"schema\":1}\n",
        )
        .expect("browser manifest should be written");
        assert_eq!(
            bundled_playwright_browsers_path(&directory).expect("a bundled runtime should resolve"),
            Some(
                browsers
                    .canonicalize()
                    .expect("browser resource should canonicalize")
            )
        );
        fs::remove_dir_all(directory).expect("browser resource should be removed");
    }

    #[cfg(unix)]
    #[test]
    fn bundled_security_proxy_addon_stays_inside_resources() {
        use std::os::unix::fs::symlink;

        let root = std::env::temp_dir().join(format!(
            "nebula-security-proxy-resource-test-{}",
            secure_token().expect("token generation should work")
        ));
        let resources = root.join("resources");
        let addon = resources.join(SECURITY_PROXY_ADDON);
        fs::create_dir_all(addon.parent().expect("addon has a parent"))
            .expect("security proxy resource directory should be created");
        fs::write(&addon, b"# bundled addon\n").expect("security proxy addon should be created");
        assert_eq!(
            bundled_security_proxy_addon_path(&resources).expect("bundled addon should resolve"),
            Some(addon.canonicalize().expect("addon should canonicalize"))
        );
        let outside = root.join("outside.py");
        fs::write(&outside, b"# outside addon\n").expect("outside file should be created");
        fs::remove_file(&addon).expect("bundled addon should be removable");
        symlink(&outside, &addon).expect("escape symlink should be created");
        assert!(bundled_security_proxy_addon_path(&resources).is_err());
        fs::remove_dir_all(root).expect("security proxy test directory should be removed");
    }

    #[test]
    fn startup_logs_preserve_stderr_content() {
        let token = "secret-sidecar-token";
        let input = format!(
            "starting with {token}\nRuntimeError: canary-prompt-command-output\n-----BEGIN PRIVATE KEY-----canary-key\n"
        );
        let output = String::from_utf8(preserve_startup_log(input.as_bytes()))
            .expect("startup logs should be UTF-8");
        assert_eq!(output, input);
    }

    #[test]
    fn bounded_log_never_exceeds_its_limit() {
        let directory = std::env::temp_dir().join(format!(
            "nebula-sidecar-log-test-{}",
            secure_token().expect("token generation should work")
        ));
        fs::create_dir_all(&directory).expect("test directory should be created");
        let path = directory.join("startup.log");
        let mut file = open_startup_log(&path).expect("startup log should open");
        let mut written = 0;
        let mut truncated = false;
        write_bounded_log(
            &mut file,
            &vec![b'x'; MAX_STARTUP_LOG_BYTES * 2],
            &mut written,
            &mut truncated,
        )
        .expect("bounded log should be written");
        file.flush().expect("bounded log should flush");
        assert!(truncated);
        assert_eq!(written, MAX_STARTUP_LOG_BYTES);
        assert_eq!(
            fs::metadata(&path).expect("metadata should exist").len(),
            MAX_STARTUP_LOG_BYTES as u64
        );
        drop(file);
        fs::remove_dir_all(directory).expect("test directory should be removed");
    }

    #[test]
    fn health_check_sends_bearer_token_and_validates_storage_health() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .expect("loopback health listener should bind");
        let port = listener
            .local_addr()
            .expect("listener should have an address")
            .port();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("health request should connect");
            let mut request = [0_u8; 4096];
            let read = stream
                .read(&mut request)
                .expect("request should be readable");
            let request = std::str::from_utf8(&request[..read]).expect("request should be UTF-8");
            assert!(request.starts_with("GET /api/v1/health HTTP/1.1\r\n"));
            assert!(request.contains("\r\nAuthorization: Bearer self-test-token\r\n"));
            let distribution = if cfg!(feature = "direct-updater") {
                "direct"
            } else {
                option_env!("NEBULA_DISTRIBUTION").unwrap_or("managed")
            };
            let commit = option_env!("NEBULA_BUILD_COMMIT").unwrap_or("test-commit");
            let target = option_env!("NEBULA_BUILD_TARGET").unwrap_or("test-target");
            let built_at = option_env!("NEBULA_BUILD_TIMESTAMP").unwrap_or("2026-07-12T12:00:00Z");
            let body = format!(
                r#"{{"status":"ok","version":"{}","commit":"{commit}","target":"{target}","build_timestamp":"{built_at}","distribution_channel":"{distribution}","mode":"local","database":"ok","api_version":"v1","schema_version":1,"journal_mode":"wal"}}"#,
                env!("CARGO_PKG_VERSION"),
            );
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(), body
            )
            .expect("health response should be written");
        });

        let health = request_authenticated_health(port, "self-test-token", Duration::from_secs(2))
            .expect("health request should succeed");
        verify_health(&health).expect("health payload should be accepted");
        server.join().expect("health server should finish");
    }

    #[test]
    fn health_accepts_retained_api_version_and_recoverable_admission() {
        let health: HealthResponse = serde_json::from_str(r#"{"status":"degraded","version":"previous-compatible-version","commit":"previous-commit","target":"linux","build_timestamp":"previous-build","distribution_channel":"managed","mode":"local","database":"ok","api_version":"v1","schema_version":5,"journal_mode":"wal"}"#).unwrap();
        assert!(verify_health(&health).is_ok());
    }

    #[test]
    fn health_validation_rejects_unmigrated_storage() {
        let health = HealthResponse {
            status: "ok".to_string(),
            version: env!("CARGO_PKG_VERSION").to_string(),
            commit: option_env!("NEBULA_BUILD_COMMIT")
                .unwrap_or("test-commit")
                .to_string(),
            target: option_env!("NEBULA_BUILD_TARGET")
                .unwrap_or("test-target")
                .to_string(),
            build_timestamp: option_env!("NEBULA_BUILD_TIMESTAMP")
                .unwrap_or("2026-07-12T12:00:00Z")
                .to_string(),
            distribution_channel: if cfg!(feature = "direct-updater") {
                "direct".to_string()
            } else {
                option_env!("NEBULA_DISTRIBUTION")
                    .unwrap_or("managed")
                    .to_string()
            },
            mode: "local".to_string(),
            database: "ok".to_string(),
            api_version: "v1".to_string(),
            schema_version: 0,
            journal_mode: "wal".to_string(),
        };
        assert!(verify_health(&health).is_err());
    }
}
