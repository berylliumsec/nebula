#![deny(unused_must_use)]

mod browser;
mod diagnostics;
mod release;
mod sidecar;

use diagnostics::{
    DiagnosticLevel, DiagnosticsState, diagnostics_files, diagnostics_get_settings,
    diagnostics_log_frontend, diagnostics_recent_errors, diagnostics_reveal_logs,
    diagnostics_status, diagnostics_update_settings, install_panic_hook,
};
use release::{check_for_update, install_available_update, release_info, restart_application};
use sidecar::{BackendState, backend_status, start_local_backend, stop_local_backend};
use tauri::menu::{MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::{Emitter, Manager, Wry};

fn build_app() -> tauri::App<Wry> {
    let builder = tauri::Builder::default()
        .manage(BackendState::default())
        .manage(browser::BrowserState::default())
        .manage(DiagnosticsState::default())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let app_data_dir = app.path().app_data_dir()?;
            let diagnostics = DiagnosticsState::clone(&*app.state::<DiagnosticsState>());
            if let Err(error) = diagnostics.initialize(&app_data_dir) {
                // The application remains usable so the interface can show its
                // persistent diagnostics-unavailable state and emergency detail.
                eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE {error}");
            }
            install_panic_hook(diagnostics.clone());
            browser::initialize(app.handle()).map_err(std::io::Error::other)?;
            diagnostics.record_desktop(
                DiagnosticLevel::Info,
                "desktop.application.starting",
                "Nebula desktop is starting.",
                Some("started"),
                Some("bootstrap"),
                None,
                serde_json::Map::new(),
            );
            Ok(())
        })
        .menu(|app| {
            let home = MenuItemBuilder::with_id("home", "Home")
                .accelerator("CmdOrCtrl+1")
                .build(app)?;
            let new_contextual = MenuItemBuilder::with_id("new-contextual", "New")
                .accelerator("CmdOrCtrl+N")
                .build(app)?;
            let settings = MenuItemBuilder::with_id("settings", "Settings…")
                .accelerator("CmdOrCtrl+,")
                .build(app)?;
            let command_center = MenuItemBuilder::with_id("command-center", "Command Center…")
                .accelerator("CmdOrCtrl+K")
                .build(app)?;
            let toggle_sidebar = MenuItemBuilder::with_id("toggle-sidebar", "Show or Hide Sidebar")
                .accelerator("Alt+CmdOrCtrl+S")
                .build(app)?;
            let toggle_inspector =
                MenuItemBuilder::with_id("toggle-inspector", "Show or Hide Activity Inspector")
                    .accelerator("Alt+CmdOrCtrl+I")
                    .build(app)?;

            let app_menu = SubmenuBuilder::new(app, "Nebula")
                .about(None)
                .separator()
                .item(&settings)
                .separator()
                .hide()
                .hide_others()
                .separator()
                .quit()
                .build()?;
            let file_menu = SubmenuBuilder::new(app, "File")
                .item(&new_contextual)
                .separator()
                .item(&home)
                .separator()
                .close_window()
                .build()?;
            let edit_menu = SubmenuBuilder::new(app, "Edit")
                .undo()
                .redo()
                .separator()
                .cut()
                .copy()
                .paste()
                .select_all()
                .build()?;
            let view_menu = SubmenuBuilder::new(app, "View")
                .item(&command_center)
                .separator()
                .item(&toggle_sidebar)
                .item(&toggle_inspector)
                .build()?;
            let window_menu = SubmenuBuilder::new(app, "Window")
                .minimize()
                .close_window()
                .build()?;
            let help_menu = SubmenuBuilder::new(app, "Help").build()?;

            MenuBuilder::new(app)
                .items(&[
                    &app_menu,
                    &file_menu,
                    &edit_menu,
                    &view_menu,
                    &window_menu,
                    &help_menu,
                ])
                .build()
        })
        .on_menu_event(|app, event| {
            let command = event.id().as_ref();
            if matches!(
                command,
                "home"
                    | "settings"
                    | "command-center"
                    | "new-contextual"
                    | "toggle-sidebar"
                    | "toggle-inspector"
            ) && app.emit("nebula-menu-command", command).is_err()
            {
                app.state::<DiagnosticsState>().record_desktop(
                    DiagnosticLevel::Error,
                    "desktop.menu.dispatch_failed",
                    "A native menu action could not be delivered to the interface.",
                    Some("failure"),
                    Some("menu-dispatch"),
                    Some(true),
                    serde_json::Map::new(),
                );
            }
        })
        .invoke_handler(tauri::generate_handler![
            backend_status,
            start_local_backend,
            stop_local_backend,
            release_info,
            check_for_update,
            install_available_update,
            restart_application,
            diagnostics_get_settings,
            diagnostics_update_settings,
            diagnostics_log_frontend,
            diagnostics_files,
            diagnostics_recent_errors,
            diagnostics_status,
            diagnostics_reveal_logs,
            browser::browser_capabilities,
            browser::browser_create_tab,
            browser::browser_navigate,
            browser::browser_control,
            browser::browser_set_bounds,
            browser::browser_set_visible,
            browser::browser_close_tab,
            browser::browser_clear_project_data,
            browser::browser_import_download,
            browser::browser_discard_download
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
            browser::shutdown(app_handle);
            let diagnostics = app_handle.state::<DiagnosticsState>();
            if let Err(error) =
                sidecar::stop_managed_backend(&app_handle.state::<BackendState>(), &diagnostics)
            {
                eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE desktop shutdown: {error}");
            }
        }
    });
}
