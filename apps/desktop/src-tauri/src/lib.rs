// Pinflow desktop shell. On startup it spawns the bundled FastAPI backend (the
// PyInstaller `pinflow-api` *onedir* sidecar, shipped as an app Resource so its
// `_internal/` sits next to the exe — no per-launch unpack, unlike onefile) on
// 127.0.0.1:8787 with production config injected via env, and kills it on exit.
// The frontend talks to that local service; all KiCad / LLM / agent work lives
// there.

use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use tauri::path::BaseDirectory;
use tauri::Manager;

/// Holds the running backend child so we can reap it on exit.
struct Sidecar(Mutex<Option<Child>>);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            // The onedir sidecar is bundled under Resources/ (exe + _internal/
            // sibling). The glob form preserves the path relative to src-tauri,
            // so probe both likely spots for the executable — and on Windows
            // PyInstaller names it `pinflow-api.exe`, so try that suffix too.
            let exe = [
                "binaries/pinflow-api/pinflow-api",
                "binaries/pinflow-api/pinflow-api.exe",
                "pinflow-api/pinflow-api",
                "pinflow-api/pinflow-api.exe",
            ]
            .iter()
            .filter_map(|p| app.path().resolve(p, BaseDirectory::Resource).ok())
            .find(|p| p.exists())
            .expect("pinflow-api sidecar executable not found in app resources");

            // Tauri's resource copy may drop the executable bit — restore it.
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if let Ok(meta) = std::fs::metadata(&exe) {
                    let mut perms = meta.permissions();
                    perms.set_mode(perms.mode() | 0o755);
                    let _ = std::fs::set_permissions(&exe, perms);
                }
            }

            let child = Command::new(&exe)
                .env("PINFLOW_API_PORT", "8787")
                .env("PINFLOW_CLOUD_URL", "https://api.pinflow.faradworks.com")
                .env("PINFLOW_LOGIN_URL", "https://pinflow.faradworks.com/login")
                .env("PINFLOW_PARENT_PID", std::process::id().to_string())
                // Inherit stdio so the sidecar's pipes never fill (no manual drain).
                .stdout(Stdio::inherit())
                .stderr(Stdio::inherit())
                .spawn()
                .expect("failed to spawn the pinflow-api sidecar");

            app.manage(Sidecar(Mutex::new(Some(child))));

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Pinflow desktop app")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(sidecar) = app_handle.try_state::<Sidecar>() {
                    if let Some(mut child) = sidecar.0.lock().unwrap().take() {
                        let _ = child.kill();
                    }
                }
            }
        });
}
