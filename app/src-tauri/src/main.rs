mod dataclaw;
mod hf;
mod logs;
mod scheduler;
mod startup;
mod tray;
mod updater;

fn main() {
    let mut builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build());

    #[cfg(target_os = "macos")]
    {
        builder = builder.on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let _ = window.hide();
                api.prevent_close();
            }
        });
    }

    builder
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            logs::start_log_watcher(app.handle().clone())?;
            tray::setup(app.handle())?;
            if let Err(error) = startup::reconcile_launch_at_login_default() {
                eprintln!("launch-at-login setup failed: {error}");
            }
            if let Err(error) = scheduler::start(app.handle().clone()) {
                eprintln!("scheduler setup failed: {error}");
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            dataclaw::dataclaw_status,
            dataclaw::dataclaw_auto_now,
            dataclaw::dataclaw_config_get,
            dataclaw::dataclaw_config_set,
            dataclaw::dataclaw_list_projects,
            hf::hf_save_token,
            hf::hf_load_token,
            hf::hf_delete_token,
            hf::hf_whoami,
            logs::logs_open_in_finder,
            logs::logs_tail,
            startup::launch_at_login_is_installed,
            updater::check_for_updates,
            updater::install_update
        ])
        .run(tauri::generate_context!())
        .expect("error running tauri");
}
