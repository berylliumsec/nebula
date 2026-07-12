use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{Mutex, mpsc},
    time::Duration,
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, State};

const SIDECAR_PROTOCOL: &str = "nebula-sidecar-v1";
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(8);
const MAX_HANDSHAKE_BYTES: u64 = 16 * 1024;

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

struct ManagedBackend {
    child: Child,
    session: BackendSession,
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

fn terminate(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn read_handshake(child: &mut Child) -> Result<SidecarHandshake, String> {
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Nebula Core did not expose its handshake stream".to_string())?;
    let (sender, receiver) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut line = String::new();
        let result = BufReader::new(stdout)
            .take(MAX_HANDSHAKE_BYTES)
            .read_line(&mut line)
            .map(|_| line);
        let _ = sender.send(result);
    });

    let line = receiver
        .recv_timeout(HANDSHAKE_TIMEOUT)
        .map_err(|_| "Nebula Core did not complete its loopback handshake in time".to_string())?
        .map_err(|error| format!("cannot read the Nebula Core handshake: {error}"))?;
    if line.len() as u64 >= MAX_HANDSHAKE_BYTES || !line.ends_with('\n') {
        return Err("Nebula Core returned an invalid handshake frame".to_string());
    }
    serde_json::from_str(&line)
        .map_err(|_| "Nebula Core returned malformed handshake JSON".to_string())
}

fn launch(app: &AppHandle) -> Result<ManagedBackend, String> {
    let sidecar = sibling_sidecar_path()?;
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot locate the Nebula data directory: {error}"))?
        .join("core");
    fs::create_dir_all(&data_dir)
        .map_err(|error| format!("cannot prepare the Nebula data directory: {error}"))?;
    let token = secure_token()?;

    let mut command = Command::new(sidecar);
    command
        .env_clear()
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
        .stderr(Stdio::null());

    #[cfg(target_os = "windows")]
    if let Some(system_root) = std::env::var_os("SystemRoot") {
        command.env("SystemRoot", system_root);
    }

    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot start Nebula Core: {error}"))?;

    let bootstrap = BootstrapInput {
        protocol: SIDECAR_PROTOCOL,
        ipc_token: &token,
    };
    let write_result = child.stdin.take().map_or_else(
        || Err("Nebula Core did not expose its bootstrap input".to_string()),
        |mut stdin| {
            serde_json::to_writer(&mut stdin, &bootstrap)
                .map_err(|error| format!("cannot encode the sidecar bootstrap: {error}"))?;
            stdin
                .write_all(b"\n")
                .and_then(|_| stdin.flush())
                .map_err(|error| format!("cannot deliver the sidecar bootstrap: {error}"))
        },
    );
    if let Err(error) = write_result {
        terminate(&mut child);
        return Err(error);
    }

    let handshake = match read_handshake(&mut child) {
        Ok(handshake) => handshake,
        Err(error) => {
            terminate(&mut child);
            return Err(error);
        }
    };
    if handshake.protocol != SIDECAR_PROTOCOL
        || handshake.host != "127.0.0.1"
        || handshake.port == 0
    {
        terminate(&mut child);
        return Err("Nebula Core did not bind an approved loopback endpoint".to_string());
    }

    if child
        .try_wait()
        .map_err(|error| error.to_string())?
        .is_some()
    {
        return Err("Nebula Core exited during its startup handshake".to_string());
    }
    Ok(ManagedBackend {
        child,
        session: BackendSession {
            endpoint: format!("http://127.0.0.1:{}/api/v1", handshake.port),
            token,
            protocol: SIDECAR_PROTOCOL,
        },
    })
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
    if let Some(managed) = process.as_mut() {
        match managed.child.try_wait() {
            Ok(None) => return Ok(managed.session.clone()),
            Ok(Some(_)) | Err(_) => *process = None,
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
    if let Some(managed) = process.as_mut() {
        if matches!(managed.child.try_wait(), Ok(None)) {
            return BackendStatus {
                state: "running",
                endpoint: Some(managed.session.endpoint.clone()),
                message: None,
            };
        }
        *process = None;
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
        terminate(&mut managed.child);
    }
}

#[cfg(test)]
mod tests {
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
}
