use std::{
    fs::{self, File, OpenOptions},
    io::{self, BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpStream},
    path::PathBuf,
    process::{Child, ChildStderr, ChildStdin, Command, Stdio},
    sync::{Mutex, mpsc},
    thread::JoinHandle,
    time::{Duration, Instant},
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

const SIDECAR_PROTOCOL: &str = "nebula-sidecar-v1";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const STARTUP_POLL_INTERVAL: Duration = Duration::from_millis(50);
const HEALTH_IO_TIMEOUT: Duration = Duration::from_secs(2);
const TERMINATION_GRACE_PERIOD: Duration = Duration::from_secs(5);
const MAX_HANDSHAKE_BYTES: u64 = 16 * 1024;
const MAX_HEALTH_RESPONSE_BYTES: u64 = 64 * 1024;
const MAX_STARTUP_LOG_BYTES: usize = 256 * 1024;
const MAX_PENDING_LOG_BYTES: usize = 16 * 1024;
const STARTUP_LOG_NAME: &str = "nebula-core-startup.log";
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
    "DEEPSEEK_API_KEY",
    "DOCKER_HOST",
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
    "NEBULA_V3_CONTAINER_RUNTIME",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "TOGETHER_API_KEY",
    "TZ",
    "XAI_API_KEY",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
];

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct BackendSession {
    endpoint: String,
    token: String,
    protocol: &'static str,
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
    child: Child,
    session: BackendSession,
    startup_log: StartupLogCapture,
    stdout_thread: Option<JoinHandle<Result<(), String>>>,
    bootstrap_stdin: Option<ChildStdin>,
}

#[derive(Default)]
pub(crate) struct BackendState {
    process: Mutex<Option<ManagedBackend>>,
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
    signal_process_group(process_group, libc::SIGTERM)?;
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

fn terminate_managed(managed: &mut ManagedBackend) -> Result<(), String> {
    // Closing the lifetime pipe asks Core to stop even if signal delivery is
    // delayed. The process-group signal remains the bounded fallback.
    managed.bootstrap_stdin.take();
    let process_result = terminate(&mut managed.child);
    let stdout_result = managed.stdout_thread.take().map_or(Ok(()), |thread| {
        thread
            .join()
            .map_err(|_| "Nebula Core output drain stopped unexpectedly".to_string())?
    });
    let log_result = managed.startup_log.finish();
    let errors: Vec<String> = [process_result, stdout_result, log_result]
        .into_iter()
        .filter_map(Result::err)
        .collect();
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors.join("; "))
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

fn redact_startup_log(input: &[u8], ipc_token: &str) -> Vec<u8> {
    let replaced = String::from_utf8_lossy(input).replace(ipc_token, "[REDACTED]");
    let markers = [
        "authorization:",
        "x-api-key:",
        "api_key=",
        "api-key=",
        "token=",
        "\"api_key\":",
        "\"apikey\":",
        "\"token\":",
        "\"authorization\":",
    ];
    let mut redacted = String::with_capacity(replaced.len());

    for line in replaced.split_inclusive('\n') {
        let lower = line.to_ascii_lowercase();
        let sensitive = markers
            .iter()
            .filter_map(|marker| lower.find(marker).map(|index| (index, marker.len())))
            .min_by_key(|(index, _)| *index);
        if let Some((index, marker_len)) = sensitive {
            redacted.push_str(&line[..index + marker_len]);
            redacted.push_str(" [REDACTED]");
            if line.ends_with('\n') {
                redacted.push('\n');
            }
        } else {
            redacted.push_str(line);
        }
    }
    redacted.into_bytes()
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
    ipc_token: String,
) -> Result<(), String> {
    let mut written = 0;
    let mut truncated = false;
    write_bounded_log(
        &mut file,
        b"[nebula] Nebula Core startup diagnostics (sensitive values redacted)\n",
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
            let line = redact_startup_log(&line, &ipc_token);
            write_bounded_log(&mut file, &line, &mut written, &mut truncated).map_err(|error| {
                format!("cannot write Nebula Core startup diagnostics: {error}")
            })?;
        }

        if pending.len() > MAX_PENDING_LOG_BYTES {
            // Retain enough bytes to catch a secret split across read boundaries.
            let guard = ipc_token.len().saturating_sub(1).clamp(32, 512);
            let flush_len = pending.len().saturating_sub(guard);
            let fragment: Vec<u8> = pending.drain(..flush_len).collect();
            let fragment = redact_startup_log(&fragment, &ipc_token);
            write_bounded_log(&mut file, &fragment, &mut written, &mut truncated).map_err(
                |error| format!("cannot write Nebula Core startup diagnostics: {error}"),
            )?;
        }
    }

    if !pending.is_empty() {
        let fragment = redact_startup_log(&pending, &ipc_token);
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
        let _ = remove_result;
        return Err(format!(
            "cannot write to the Nebula data directory {}: {error}",
            data_dir.display()
        ));
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
    let expected_version = env!("CARGO_PKG_VERSION");
    let expected_commit = option_env!("NEBULA_BUILD_COMMIT");
    let expected_target = option_env!("NEBULA_BUILD_TARGET");
    let expected_timestamp = option_env!("NEBULA_BUILD_TIMESTAMP");
    let expected_distribution = if cfg!(feature = "direct-updater") {
        Some("direct")
    } else {
        option_env!("NEBULA_DISTRIBUTION")
    };
    if health.status != "ok"
        || health.database != "ok"
        || health.api_version != "v1"
        || health.mode != "local"
        || health.schema_version == 0
        || !health.journal_mode.eq_ignore_ascii_case("wal")
        || health.version != expected_version
        || health.commit.trim().is_empty()
        || health.target.trim().is_empty()
        || health.build_timestamp.trim().is_empty()
        || health.distribution_channel.trim().is_empty()
    {
        return Err(format!(
            "Nebula Core health validation failed (status={}, database={}, api_version={}, mode={}, schema_version={}, journal_mode={})",
            health.status,
            health.database,
            health.api_version,
            health.mode,
            health.schema_version,
            health.journal_mode
        ));
    }
    for (label, actual, expected) in [
        ("commit", health.commit.as_str(), expected_commit),
        ("target", health.target.as_str(), expected_target),
        (
            "build timestamp",
            health.build_timestamp.as_str(),
            expected_timestamp,
        ),
        (
            "distribution channel",
            health.distribution_channel.as_str(),
            expected_distribution,
        ),
    ] {
        if let Some(expected) = expected
            && actual != expected
        {
            return Err(format!(
                "Nebula Core {label} {actual:?} does not match desktop {expected:?}"
            ));
        }
    }
    Ok(())
}

fn wait_for_authenticated_health(
    child: &mut Child,
    port: u16,
    ipc_token: &str,
    deadline: Instant,
) -> Result<(), String> {
    let mut last_error = "the health endpoint has not accepted a connection".to_string();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return Err(format!(
                    "Nebula Core exited with {status} before becoming healthy"
                ));
            }
            Ok(None) => {}
            Err(error) => {
                return Err(format!(
                    "cannot inspect Nebula Core while checking its health: {error}"
                ));
            }
        }

        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(format!(
                "Nebula Core did not become healthy within 60 seconds: {last_error}"
            ));
        }
        match request_authenticated_health(port, ipc_token, remaining.min(HEALTH_IO_TIMEOUT)) {
            Ok(health) => return verify_health(&health),
            Err(error) => last_error = error,
        }

        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            continue;
        }
        std::thread::sleep(remaining.min(STARTUP_POLL_INTERVAL));
    }
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
        (None, None) => format!("{error}; redacted startup diagnostics: {diagnostics}"),
        (Some(cleanup_error), None) => format!(
            "{error}; sidecar cleanup also failed: {cleanup_error}; redacted startup diagnostics: {diagnostics}"
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
    let app_data_dir = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate the Nebula data directory: {error}"))?;
    fs::create_dir_all(&app_data_dir)
        .map_err(|error| format!("cannot prepare the Nebula application directory: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&app_data_dir, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("cannot secure the Nebula application directory: {error}"))?;
    }
    let data_dir = app_data_dir.join("core");
    fs::create_dir_all(&data_dir)
        .map_err(|error| format!("cannot prepare the Nebula data directory: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&data_dir, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("cannot secure the Nebula data directory: {error}"))?;
    }
    verify_writable_storage(&data_dir)?;

    let token = secure_token()?;
    let startup_log_path = app_data_dir.join("logs").join(STARTUP_LOG_NAME);
    let mut startup_log_file = open_startup_log(&startup_log_path)?;

    let mut command = Command::new(sidecar);
    command
        .env_clear()
        .env("PATH", TRUSTED_SYSTEM_PATH)
        .env("NEBULA_V3_DATA_DIR", &data_dir)
        .args([
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--handshake-stdout",
        ])
        .current_dir(&data_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    for name in SIDECAR_ENV_ALLOWLIST {
        if let Some(value) = std::env::var_os(name) {
            command.env(name, value);
        }
    }

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }

    #[cfg(target_os = "windows")]
    if let Some(system_root) = std::env::var_os("SystemRoot") {
        command.env("SystemRoot", system_root);
    }

    let mut child = command.spawn().map_err(|error| {
        let message = format!("cannot start Nebula Core: {error}");
        let mut written = 0;
        let mut truncated = false;
        let _ = write_bounded_log(
            &mut startup_log_file,
            message.as_bytes(),
            &mut written,
            &mut truncated,
        );
        let _ = startup_log_file.flush();
        format!(
            "{message}; redacted startup diagnostics: {}",
            startup_log_path.display()
        )
    })?;
    let stderr = match child.stderr.take() {
        Some(stderr) => stderr,
        None => {
            let cleanup = terminate(&mut child)
                .err()
                .map(|error| format!("; sidecar cleanup also failed: {error}"))
                .unwrap_or_default();
            return Err(format!(
                "Nebula Core did not expose its diagnostic stream{cleanup}; diagnostic path: {}",
                startup_log_path.display(),
            ));
        }
    };
    let log_token = token.clone();
    let log_thread =
        std::thread::spawn(move || capture_startup_log(stderr, startup_log_file, log_token));
    let mut startup_log = StartupLogCapture {
        path: startup_log_path,
        thread: Some(log_thread),
    };
    let deadline = Instant::now() + STARTUP_TIMEOUT;

    let bootstrap = BootstrapInput {
        protocol: SIDECAR_PROTOCOL,
        ipc_token: &token,
    };
    let Some(mut bootstrap_stdin) = child.stdin.take() else {
        return Err(startup_failure(
            &mut child,
            &mut startup_log,
            "Nebula Core did not expose its bootstrap input".to_string(),
        ));
    };
    let write_result = serde_json::to_writer(&mut bootstrap_stdin, &bootstrap)
        .map_err(|error| format!("cannot encode the sidecar bootstrap: {error}"))
        .and_then(|_| {
            bootstrap_stdin
                .write_all(b"\n")
                .and_then(|_| bootstrap_stdin.flush())
                .map_err(|error| format!("cannot deliver the sidecar bootstrap: {error}"))
        });
    if let Err(error) = write_result {
        return Err(startup_failure(&mut child, &mut startup_log, error));
    }

    let (handshake, stdout_thread) = match read_handshake(&mut child, deadline) {
        Ok(result) => result,
        Err(error) => {
            return Err(startup_failure(&mut child, &mut startup_log, error));
        }
    };
    if handshake.protocol != SIDECAR_PROTOCOL
        || handshake.host != "127.0.0.1"
        || handshake.port == 0
    {
        return Err(startup_failure(
            &mut child,
            &mut startup_log,
            "Nebula Core did not bind an approved loopback endpoint".to_string(),
        ));
    }

    if let Err(error) = wait_for_authenticated_health(&mut child, handshake.port, &token, deadline)
    {
        return Err(startup_failure(&mut child, &mut startup_log, error));
    }

    Ok(ManagedBackend {
        child,
        session: BackendSession {
            endpoint: format!("http://127.0.0.1:{}/api/v1", handshake.port),
            token,
            protocol: SIDECAR_PROTOCOL,
        },
        startup_log,
        stdout_thread: Some(stdout_thread),
        bootstrap_stdin: Some(bootstrap_stdin),
    })
}

/// Run the complete packaged-Core startup check without leaving a server behind.
///
/// `launch` verifies writable application storage, the migrated SQLite schema,
/// WAL mode, and an authenticated `/api/v1/health` response before returning.
/// This primitive is intentionally independent of frontend state so the Tauri
/// entry point can expose it through a command-line flag or a diagnostic command.
#[allow(dead_code)]
pub(crate) fn self_test_local_backend(app: &AppHandle) -> Result<(), String> {
    let mut managed = launch(app)?;
    terminate_managed(&mut managed)
        .map_err(|error| format!("Nebula Core self-test passed but cleanup failed: {error}"))
}

#[tauri::command]
pub(crate) fn start_local_backend(
    app: AppHandle,
    state: State<'_, BackendState>,
) -> Result<BackendSession, String> {
    let mut process = state
        .process
        .lock()
        .map_err(|_| "the Nebula Core supervisor is unavailable".to_string())?;
    if let Some(mut managed) = process.take() {
        match managed.child.try_wait() {
            Ok(None) => {
                let session = managed.session.clone();
                *process = Some(managed);
                return Ok(session);
            }
            Ok(Some(_)) => {
                let _ = managed.startup_log.finish();
            }
            Err(_) => {
                let _ = terminate_managed(&mut managed);
            }
        }
    }
    let managed = launch(&app)?;
    let session = managed.session.clone();
    *process = Some(managed);
    Ok(session)
}

#[tauri::command]
pub(crate) fn backend_status(state: State<'_, BackendState>) -> BackendStatus {
    let Ok(mut process) = state.process.lock() else {
        return BackendStatus {
            state: "unavailable",
            endpoint: None,
            message: Some("the Nebula Core supervisor lock is unavailable".to_string()),
        };
    };
    if let Some(mut managed) = process.take() {
        match managed.child.try_wait() {
            Ok(None) => {
                let endpoint = managed.session.endpoint.clone();
                *process = Some(managed);
                return BackendStatus {
                    state: "running",
                    endpoint: Some(endpoint),
                    message: None,
                };
            }
            Ok(Some(status)) => {
                let diagnostics = managed.startup_log.path.display().to_string();
                let _ = managed.startup_log.finish();
                return BackendStatus {
                    state: "stopped",
                    endpoint: None,
                    message: Some(format!(
                        "Nebula Core exited with {status}; redacted diagnostics: {diagnostics}"
                    )),
                };
            }
            Err(error) => {
                let diagnostics = managed.startup_log.path.display().to_string();
                let _ = terminate_managed(&mut managed);
                return BackendStatus {
                    state: "unavailable",
                    endpoint: None,
                    message: Some(format!(
                        "cannot inspect Nebula Core: {error}; redacted diagnostics: {diagnostics}"
                    )),
                };
            }
        }
    }
    BackendStatus {
        state: "stopped",
        endpoint: None,
        message: None,
    }
}

#[tauri::command]
pub(crate) fn stop_local_backend(state: State<'_, BackendState>) {
    stop_managed_backend(&state);
}

pub(crate) fn stop_managed_backend(state: &State<'_, BackendState>) {
    if let Ok(mut process) = state.process.lock()
        && let Some(mut managed) = process.take()
    {
        let _ = terminate_managed(&mut managed);
    }
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
    fn startup_logs_redact_ipc_tokens_and_credential_fields() {
        let token = "secret-sidecar-token";
        let input = format!(
            "starting with {token}\nAuthorization: Bearer another-secret\napi_key=hunter2\nsafe line\n"
        );
        let output = String::from_utf8(redact_startup_log(input.as_bytes(), token))
            .expect("redacted logs should be UTF-8");
        assert!(!output.contains(token));
        assert!(!output.contains("another-secret"));
        assert!(!output.contains("hunter2"));
        assert!(output.contains("safe line"));
        assert!(output.contains("[REDACTED]"));
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
