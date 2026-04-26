use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Emitter, LogicalPosition, Manager, Runtime, WebviewWindow,
};

const WINDOW_LABEL: &str = "main";

fn main_window<R: Runtime>(app: &AppHandle<R>) -> Option<WebviewWindow<R>> {
    app.get_webview_window(WINDOW_LABEL)
}

fn position_near_tray<R: Runtime>(window: &WebviewWindow<R>, anchor_x: f64, anchor_y: f64) {
    let scale = window.scale_factor().unwrap_or(1.0);
    let size = window.outer_size().unwrap_or_default();
    let logical_w = (size.width as f64) / scale;
    let x = (anchor_x - logical_w / 2.0).max(8.0);
    let y = anchor_y + 8.0;
    let _ = window.set_position(LogicalPosition::new(x, y));
}

fn toggle_window<R: Runtime>(app: &AppHandle<R>, anchor: Option<(f64, f64)>) {
    let Some(window) = main_window(app) else {
        return;
    };
    let visible = window.is_visible().unwrap_or(false);
    if visible {
        let _ = window.hide();
    } else {
        if let Some((x, y)) = anchor {
            position_near_tray(&window, x, y);
        }
        let _ = window.show();
        let _ = window.set_focus();
    }
}

pub fn setup<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let show_item = MenuItem::with_id(app, "show", "Show DataClaw", true, None::<&str>)?;
    let run_item = MenuItem::with_id(app, "run-now", "Run now", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Quit DataClaw", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_item, &run_item, &quit_item])?;

    let tray = TrayIconBuilder::with_id("main")
        .tooltip("DataClaw")
        .icon(app.default_window_icon().cloned().expect("no default icon"))
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => toggle_window(app, None),
            "quit" => app.exit(0),
            "run-now" => {
                let _ = app.emit("tray-run-now", ());
                toggle_window(app, None);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                rect,
                ..
            } = event
            {
                let app = tray.app_handle();
                let scale = main_window(app)
                    .and_then(|w| w.scale_factor().ok())
                    .unwrap_or(1.0);
                let pos = rect.position.to_logical::<f64>(scale);
                let size = rect.size.to_logical::<f64>(scale);
                let center_x = pos.x + size.width / 2.0;
                let bottom_y = pos.y + size.height;
                toggle_window(app, Some((center_x, bottom_y)));
            }
        })
        .build(app)?;

    let _ = tray;
    Ok(())
}
