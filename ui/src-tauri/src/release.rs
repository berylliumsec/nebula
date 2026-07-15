use serde::Serialize;
use tauri::{AppHandle, Manager};

use crate::diagnostics::{DiagnosticLevel, DiagnosticsState};

#[cfg(feature = "direct-updater")]
use std::time::Duration;
#[cfg(feature = "direct-updater")]
use tauri_plugin_updater::UpdaterExt;

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ReleaseInfo {
    version: String,
    commit: &'static str,
    build_target: &'static str,
    built_at: &'static str,
    distribution: &'static str,
    update_channel: Option<&'static str>,
    updater_enabled: bool,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AvailableUpdate {
    current_version: String,
    version: String,
    notes: Option<String>,
    published_at: Option<String>,
}

fn build_release_info(version: String) -> ReleaseInfo {
    ReleaseInfo {
        version,
        commit: option_env!("NEBULA_BUILD_COMMIT").unwrap_or("development"),
        build_target: option_env!("NEBULA_BUILD_TARGET").unwrap_or("local"),
        built_at: option_env!("NEBULA_BUILD_TIMESTAMP").unwrap_or("unknown"),
        distribution: if cfg!(feature = "direct-updater") {
            "direct"
        } else {
            option_env!("NEBULA_DISTRIBUTION").unwrap_or("managed")
        },
        update_channel: if cfg!(feature = "direct-updater") {
            Some(option_env!("NEBULA_UPDATE_CHANNEL").unwrap_or("prerelease"))
        } else {
            None
        },
        updater_enabled: cfg!(feature = "direct-updater"),
    }
}

#[tauri::command]
pub fn release_info(app: AppHandle) -> ReleaseInfo {
    app.state::<DiagnosticsState>().record_desktop(
        DiagnosticLevel::Debug,
        "desktop.release.info_read",
        "Desktop release information was read.",
        Some("success"),
        Some("release-info"),
        None,
        serde_json::Map::new(),
    );
    build_release_info(app.package_info().version.to_string())
}

#[cfg(feature = "direct-updater")]
fn configured_updater(app: &AppHandle) -> Result<tauri_plugin_updater::Updater, String> {
    let endpoint = env!("NEBULA_UPDATE_ENDPOINT")
        .parse::<tauri::Url>()
        .map_err(|error| format!("invalid update endpoint: {error}"))?;

    app.updater_builder()
        .pubkey(env!("NEBULA_UPDATER_PUBLIC_KEY"))
        .endpoints(vec![endpoint])
        .map_err(|error| format!("invalid update configuration: {error}"))?
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|error| format!("could not initialize updater: {error}"))
}

#[tauri::command]
pub async fn check_for_update(app: AppHandle) -> Result<Option<AvailableUpdate>, String> {
    let diagnostics = app.state::<DiagnosticsState>();
    diagnostics.record_desktop(
        DiagnosticLevel::Info,
        "desktop.updater.check_started",
        "The desktop update check started.",
        Some("started"),
        Some("update-check"),
        None,
        serde_json::Map::new(),
    );
    #[cfg(feature = "direct-updater")]
    {
        let updater = configured_updater(&app).map_err(|error| {
            diagnostics.record_desktop(
                DiagnosticLevel::Error,
                "desktop.updater.configuration_failed",
                "The desktop updater configuration could not be initialized.",
                Some("failure"),
                Some("update-configuration"),
                Some(false),
                serde_json::Map::new(),
            );
            error
        })?;
        let update = updater.check().await.map_err(|error| {
            diagnostics.record_desktop(
                DiagnosticLevel::Error,
                "desktop.updater.check_failed",
                "The desktop update check failed.",
                Some("failure"),
                Some("update-check"),
                Some(true),
                serde_json::Map::new(),
            );
            format!("update check failed: {error}")
        })?;
        diagnostics.record_desktop(
            DiagnosticLevel::Info,
            "desktop.updater.check_completed",
            "The desktop update check completed.",
            Some("success"),
            Some("update-check"),
            None,
            serde_json::Map::new(),
        );
        return Ok(update.map(|update| AvailableUpdate {
            current_version: update.current_version,
            version: update.version,
            notes: update.body,
            published_at: update.date.map(|date| date.to_string()),
        }));
    }

    #[cfg(not(feature = "direct-updater"))]
    {
        diagnostics.record_desktop(
            DiagnosticLevel::Warning,
            "desktop.updater.managed_installation",
            "The installation package manager owns updates for this build.",
            Some("denied"),
            Some("update-check"),
            Some(false),
            serde_json::Map::new(),
        );
        Err("updates are managed by the installation package manager".to_string())
    }
}

#[tauri::command]
pub async fn install_available_update(app: AppHandle) -> Result<bool, String> {
    let diagnostics = app.state::<DiagnosticsState>();
    diagnostics.record_desktop(
        DiagnosticLevel::Info,
        "desktop.updater.install_started",
        "Desktop update download and installation started.",
        Some("started"),
        Some("update-install"),
        None,
        serde_json::Map::new(),
    );
    #[cfg(feature = "direct-updater")]
    {
        let updater = configured_updater(&app).map_err(|error| {
            diagnostics.record_desktop(
                DiagnosticLevel::Error,
                "desktop.updater.configuration_failed",
                "The desktop updater configuration could not be initialized.",
                Some("failure"),
                Some("update-configuration"),
                Some(false),
                serde_json::Map::new(),
            );
            error
        })?;
        let Some(update) = updater.check().await.map_err(|error| {
            diagnostics.record_desktop(
                DiagnosticLevel::Error,
                "desktop.updater.install_check_failed",
                "The pre-install update check failed.",
                Some("failure"),
                Some("update-check"),
                Some(true),
                serde_json::Map::new(),
            );
            format!("update check failed: {error}")
        })?
        else {
            diagnostics.record_desktop(
                DiagnosticLevel::Info,
                "desktop.updater.no_update",
                "No desktop update is currently available.",
                Some("success"),
                Some("update-check"),
                None,
                serde_json::Map::new(),
            );
            return Ok(false);
        };

        update
            .download_and_install(|_, _| {}, || {})
            .await
            .map_err(|error| {
                diagnostics.record_desktop(
                    DiagnosticLevel::Error,
                    "desktop.updater.install_failed",
                    "The desktop update could not be downloaded or installed.",
                    Some("failure"),
                    Some("update-install"),
                    Some(true),
                    serde_json::Map::new(),
                );
                format!("update installation failed: {error}")
            })?;
        diagnostics.record_desktop(
            DiagnosticLevel::Info,
            "desktop.updater.install_completed",
            "The desktop update was downloaded and installed.",
            Some("success"),
            Some("update-install"),
            None,
            serde_json::Map::new(),
        );
        return Ok(true);
    }

    #[cfg(not(feature = "direct-updater"))]
    {
        diagnostics.record_desktop(
            DiagnosticLevel::Warning,
            "desktop.updater.managed_installation",
            "The installation package manager owns updates for this build.",
            Some("denied"),
            Some("update-install"),
            Some(false),
            serde_json::Map::new(),
        );
        Err("updates are managed by the installation package manager".to_string())
    }
}

#[tauri::command]
pub fn restart_application(app: AppHandle) {
    app.state::<DiagnosticsState>().record_desktop(
        DiagnosticLevel::Info,
        "desktop.application.restart_requested",
        "A desktop restart was requested.",
        Some("started"),
        Some("restart"),
        None,
        serde_json::Map::new(),
    );
    app.request_restart();
}

#[cfg(test)]
mod tests {
    use super::build_release_info;

    #[test]
    fn managed_builds_never_advertise_the_updater() {
        let info = build_release_info(env!("CARGO_PKG_VERSION").to_string());

        if cfg!(feature = "direct-updater") {
            assert!(info.updater_enabled);
            assert_eq!(info.distribution, "direct");
            assert!(info.update_channel.is_some());
        } else {
            assert!(!info.updater_enabled);
            assert_eq!(info.distribution, "managed");
            assert!(info.update_channel.is_none());
        }
    }
}
