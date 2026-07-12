mod sidecar;

use sidecar::{BackendState, backend_status, start_local_backend, stop_local_backend};
use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![
            backend_status,
            start_local_backend,
            stop_local_backend
        ])
        .build(tauri::generate_context!())
        .expect("failed to build the Nebula desktop shell");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            sidecar::stop_managed_backend(&app_handle.state::<BackendState>());
        }
    });
}
