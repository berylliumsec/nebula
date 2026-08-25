//! Desktop-local Core selection. Remote credentials never enter the config file.

use std::{fs, io::Write, path::PathBuf};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager};

use crate::{
    diagnostics::DiagnosticsState,
    sidecar::{BackendSession, BackendState, start_local_backend},
};

const CONFIG_NAME: &str = "core-connection.json";
const TOKEN_SERVICE: &str = "io.berylliumsec.nebula.remote-core";
const TOKEN_ACCOUNT: &str = "bearer-token";

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct StoredConnection {
    #[serde(default = "local_mode")]
    mode: String,
    endpoint: Option<String>,
    device_id: String,
}

fn local_mode() -> String {
    "local".into()
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct CoreConnectionStatus {
    pub(crate) mode: String,
    pub(crate) endpoint: Option<String>,
    pub(crate) token_available: bool,
    pub(crate) device_id: String,
}

fn config_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|error| format!("cannot resolve application data: {error}"))?
        .join(CONFIG_NAME))
}

fn random_device_id() -> Result<String, String> {
    let mut bytes = [0_u8; 18];
    getrandom::fill(&mut bytes)
        .map_err(|error| format!("cannot create desktop identity: {error}"))?;
    Ok(format!("desktop-{}", URL_SAFE_NO_PAD.encode(bytes)))
}

fn load(app: &AppHandle) -> Result<StoredConnection, String> {
    let path = config_path(app)?;
    match fs::read(&path) {
        Ok(raw) => serde_json::from_slice(&raw).map_err(|_| {
            "desktop Core connection settings are invalid; clear and reconnect".to_string()
        }),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(StoredConnection {
            mode: local_mode(),
            endpoint: None,
            device_id: random_device_id()?,
        }),
        Err(error) => Err(format!(
            "cannot read desktop Core connection settings: {error}"
        )),
    }
}

fn save(app: &AppHandle, value: &StoredConnection) -> Result<(), String> {
    let path = config_path(app)?;
    let parent = path
        .parent()
        .ok_or_else(|| "desktop Core settings path is invalid".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("cannot create desktop Core settings directory: {error}"))?;
    let temporary = path.with_extension("json.tmp");
    let mut file = fs::File::create(&temporary)
        .map_err(|error| format!("cannot save desktop Core settings: {error}"))?;
    file.write_all(&serde_json::to_vec(value).map_err(|error| error.to_string())?)
        .map_err(|error| format!("cannot save desktop Core settings: {error}"))?;
    file.sync_all()
        .map_err(|error| format!("cannot save desktop Core settings: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("cannot protect desktop Core settings: {error}"))?;
    }
    fs::rename(temporary, path)
        .map_err(|error| format!("cannot activate desktop Core settings: {error}"))
}

fn token_entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(TOKEN_SERVICE, TOKEN_ACCOUNT)
        .map_err(|_| "the operating-system credential vault is unavailable".to_string())
}

fn normalize_endpoint(value: &str, insecure_acknowledged: bool) -> Result<String, String> {
    let mut endpoint = value.trim().trim_end_matches('/').to_string();
    if endpoint.ends_with("/api/v1") {
        endpoint.truncate(endpoint.len() - "/api/v1".len());
    }
    if endpoint.is_empty()
        || endpoint.contains(char::is_whitespace)
        || endpoint.contains('@')
        || endpoint.contains('?')
        || endpoint.contains('#')
    {
        return Err(
            "Enter an HTTP(S) Core origin without credentials, query, or fragment.".to_string(),
        );
    }
    let secure = endpoint.starts_with("https://");
    let insecure = endpoint.starts_with("http://");
    let host = endpoint
        .strip_prefix("https://")
        .or_else(|| endpoint.strip_prefix("http://"))
        .unwrap_or("");
    if host.is_empty() || host.contains('/') || host.contains('\\') {
        return Err("Enter a valid Core origin.".to_string());
    }
    if insecure && !insecure_acknowledged {
        return Err(
            "HTTPS is required unless you explicitly acknowledge insecure bearer-token transport."
                .to_string(),
        );
    }
    if !secure && !insecure {
        return Err("Core endpoints must use HTTP or HTTPS.".to_string());
    }
    Ok(format!("{endpoint}/api/v1"))
}

#[tauri::command]
pub(crate) fn desktop_core_connection(app: AppHandle) -> Result<CoreConnectionStatus, String> {
    let stored = load(&app)?;
    let token_available = stored.mode == "remote"
        && token_entry()
            .and_then(|entry| {
                entry
                    .get_password()
                    .map_err(|_| "credential unavailable".to_string())
            })
            .map(|value| !value.is_empty())
            .unwrap_or(false);
    Ok(CoreConnectionStatus {
        mode: stored.mode,
        endpoint: stored.endpoint,
        token_available,
        device_id: stored.device_id,
    })
}

#[tauri::command]
pub(crate) fn configure_remote_backend(
    app: AppHandle,
    endpoint: String,
    token: String,
    acknowledge_insecure_transport: bool,
) -> Result<CoreConnectionStatus, String> {
    let endpoint = normalize_endpoint(&endpoint, acknowledge_insecure_transport)?;
    if token.trim().len() < 24 {
        return Err("Enter the remote Core bearer token.".to_string());
    }
    token_entry()?.set_password(token.trim()).map_err(|_| {
        "cannot save the remote Core token in the operating-system credential vault".to_string()
    })?;
    let mut stored = load(&app)?;
    stored.mode = "remote".into();
    stored.endpoint = Some(endpoint);
    save(&app, &stored)?;
    desktop_core_connection(app)
}

#[tauri::command]
pub(crate) fn use_local_backend(app: AppHandle) -> Result<CoreConnectionStatus, String> {
    let mut stored = load(&app)?;
    stored.mode = local_mode();
    stored.endpoint = None;
    save(&app, &stored)?;
    desktop_core_connection(app)
}

#[tauri::command]
pub(crate) fn clear_remote_backend(app: AppHandle) -> Result<CoreConnectionStatus, String> {
    token_entry()?
        .delete_credential()
        .map_err(|error| format!("cannot clear remote Core credential: {error}"))?;
    use_local_backend(app)
}

#[tauri::command]
pub(crate) fn desktop_device_id(app: AppHandle) -> Result<String, String> {
    Ok(load(&app)?.device_id)
}

#[tauri::command]
pub(crate) fn resolve_backend_connection(
    app: AppHandle,
    state: tauri::State<'_, BackendState>,
    diagnostics: tauri::State<'_, DiagnosticsState>,
) -> Result<BackendSession, String> {
    let stored = load(&app)?;
    if stored.mode == "remote" {
        let endpoint = stored.endpoint.ok_or_else(|| {
            "remote Core endpoint is missing; choose Local Core or configure a remote endpoint"
                .to_string()
        })?;
        let token = token_entry()?
            .get_password()
            .map_err(|_| "remote Core token is unavailable; reconnect in Settings".to_string())?;
        if token.is_empty() {
            return Err("remote Core token is unavailable; reconnect in Settings".to_string());
        }
        return Ok(BackendSession {
            endpoint,
            token,
            protocol: "nebula-remote-core-v1",
            source: "remote",
        });
    }
    start_local_backend(app, state, diagnostics)
}

#[cfg(test)]
mod tests {
    use super::normalize_endpoint;

    #[test]
    fn normalizes_remote_api_origin_without_userinfo() {
        assert_eq!(
            normalize_endpoint("https://core.example/api/v1/", false).unwrap(),
            "https://core.example/api/v1"
        );
        assert!(normalize_endpoint("https://token@core.example", false).is_err());
        assert!(normalize_endpoint("https://core.example/path", false).is_err());
    }

    #[test]
    fn http_requires_explicit_acknowledgement() {
        assert!(normalize_endpoint("http://192.0.2.10:8000", false).is_err());
        assert_eq!(
            normalize_endpoint("http://192.0.2.10:8000", true).unwrap(),
            "http://192.0.2.10:8000/api/v1"
        );
    }
}
