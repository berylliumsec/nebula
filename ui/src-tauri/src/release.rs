use serde::Serialize;
use tauri::AppHandle;

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
    #[cfg(feature = "direct-updater")]
    {
        let update = configured_updater(&app)?
            .check()
            .await
            .map_err(|error| format!("update check failed: {error}"))?;
        return Ok(update.map(|update| AvailableUpdate {
            current_version: update.current_version,
            version: update.version,
            notes: update.body,
            published_at: update.date.map(|date| date.to_string()),
        }));
    }

    #[cfg(not(feature = "direct-updater"))]
    {
        let _ = app;
        Err("updates are managed by the installation package manager".to_string())
    }
}

#[tauri::command]
pub async fn install_available_update(app: AppHandle) -> Result<bool, String> {
    #[cfg(feature = "direct-updater")]
    {
        let Some(update) = configured_updater(&app)?
            .check()
            .await
            .map_err(|error| format!("update check failed: {error}"))?
        else {
            return Ok(false);
        };

        update
            .download_and_install(|_, _| {}, || {})
            .await
            .map_err(|error| format!("update installation failed: {error}"))?;
        return Ok(true);
    }

    #[cfg(not(feature = "direct-updater"))]
    {
        let _ = app;
        Err("updates are managed by the installation package manager".to_string())
    }
}

#[tauri::command]
pub fn restart_application(app: AppHandle) {
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
