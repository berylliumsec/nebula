mod release;
mod sidecar;

use release::{check_for_update, install_available_update, release_info};
use sidecar::{BackendState, backend_status, start_local_backend, stop_local_backend};
use tauri::{Manager, Wry};

fn build_app() -> tauri::App<Wry> {
    let builder = tauri::Builder::default()
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![
            backend_status,
            start_local_backend,
            stop_local_backend,
            release_info,
            check_for_update,
            install_available_update
        ]);

    #[cfg(feature = "direct-updater")]
    let builder = builder.plugin(tauri_plugin_updater::Builder::new().build());

    builder
        .build(tauri::generate_context!())
        .expect("failed to build the Nebula desktop shell")
}

/// Exercise the installed desktop-to-Core boundary without entering the UI loop.
pub fn self_test() -> Result<(), String> {
    let app = build_app();
    sidecar::self_test_local_backend(app.handle())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = build_app();

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            sidecar::stop_managed_backend(&app_handle.state::<BackendState>());
        }
    });
}
