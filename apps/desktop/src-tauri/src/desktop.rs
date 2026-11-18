// SPDX-License-Identifier: MPL-2.0
use corinthos_desktop::{
    sidecar::{EventSink, Handshake, Sidecar},
    workspace,
};
use serde::Serialize;
use serde_json::Value;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, Url, WebviewWindowBuilder};

#[derive(Default)]
struct Backend {
    process: Option<(Sidecar, Handshake)>,
    attempted_start: bool,
    startup_error: Option<&'static str>,
    closing: bool,
}

#[derive(Clone, Default)]
struct DesktopState {
    backend: Arc<Mutex<Backend>>,
    generation: Arc<AtomicU64>,
}

#[derive(Clone, Serialize)]
struct DesktopIdentity {
    version: String,
    identifier: String,
}

impl DesktopIdentity {
    fn from_app(app: &AppHandle) -> Self {
        Self {
            version: app.package_info().version.to_string(),
            identifier: app.config().identifier.clone(),
        }
    }
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum FailureOrigin {
    Startup,
    Transport,
    Busy,
    Closing,
    Bridge,
}

#[derive(Serialize)]
struct Snapshot {
    connection: &'static str,
    desktop: DesktopIdentity,
    #[serde(skip_serializing_if = "Option::is_none")]
    handshake: Option<Handshake>,
    #[serde(skip_serializing_if = "Option::is_none")]
    providers: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    failure_origin: Option<FailureOrigin>,
}

impl Snapshot {
    fn failure(desktop: DesktopIdentity, origin: FailureOrigin, error: &'static str) -> Self {
        Self {
            connection: if matches!(origin, FailureOrigin::Startup) {
                "unavailable"
            } else {
                "disconnected"
            },
            desktop,
            handshake: None,
            providers: None,
            error: Some(error),
            failure_origin: Some(origin),
        }
    }
}

async fn snapshot(app: AppHandle, state: DesktopState, restart: bool) -> Snapshot {
    let desktop = DesktopIdentity::from_app(&app);
    let fallback_desktop = desktop.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let Ok(mut backend) = state.backend.try_lock() else {
            return Snapshot::failure(
                desktop,
                FailureOrigin::Busy,
                "The sidecar is busy. Retry the request.",
            );
        };
        if backend.closing {
            return Snapshot::failure(
                desktop,
                FailureOrigin::Closing,
                "The Corinthos desktop is closing.",
            );
        }
        if restart {
            state.generation.fetch_add(1, Ordering::AcqRel);
            if let Some((mut sidecar, _)) = backend.process.take()
                && let Err(error) = sidecar.shutdown()
            {
                return Snapshot::failure(desktop, FailureOrigin::Transport, error);
            }
            backend.attempted_start = false;
            backend.startup_error = None;
        }
        if !backend.attempted_start {
            backend.attempted_start = true;
            let epoch = state.generation.load(Ordering::Acquire);
            let generation = Arc::clone(&state.generation);
            let established = AtomicBool::new(false);
            let events: EventSink = Arc::new(move |event| {
                if epoch == generation.load(Ordering::Acquire) {
                    if event.method == "system.ready" {
                        established.store(true, Ordering::Release);
                    } else if matches!(event.method, "backend.disconnected" | "system.stopping")
                        && !established.load(Ordering::Acquire)
                    {
                        return;
                    }
                    let _ = app.emit("backend-lifecycle", event);
                }
            });
            match Sidecar::start(events) {
                Ok(process) => backend.process = Some(process),
                Err(error) => {
                    backend.startup_error = Some(error);
                    return Snapshot::failure(desktop, FailureOrigin::Startup, error);
                }
            }
        }
        if let Some(error) = backend.startup_error {
            return Snapshot::failure(desktop, FailureOrigin::Startup, error);
        }
        if let Some((sidecar, handshake)) = backend.process.as_mut()
            && sidecar.is_running()
        {
            return match sidecar.providers_status() {
                Ok(providers) => Snapshot {
                    connection: "ready",
                    desktop,
                    handshake: Some(handshake.clone()),
                    providers: Some(providers),
                    error: None,
                    failure_origin: None,
                },
                Err(error) => Snapshot::failure(desktop, FailureOrigin::Transport, error),
            };
        }
        Snapshot::failure(
            desktop,
            FailureOrigin::Transport,
            "The Corinthos sidecar is disconnected.",
        )
    })
    .await
    .unwrap_or_else(|_| {
        Snapshot::failure(
            fallback_desktop,
            FailureOrigin::Bridge,
            "The sidecar bridge could not complete the request.",
        )
    })
}

#[tauri::command]
async fn backend_snapshot(
    app: AppHandle,
    state: State<'_, DesktopState>,
) -> Result<Snapshot, &'static str> {
    Ok(snapshot(app, state.inner().clone(), false).await)
}

#[tauri::command]
async fn restart_backend(
    app: AppHandle,
    state: State<'_, DesktopState>,
) -> Result<Snapshot, &'static str> {
    Ok(snapshot(app, state.inner().clone(), true).await)
}

#[tauri::command]
async fn workspace_request(
    method: String,
    params: Value,
    state: State<'_, DesktopState>,
) -> Result<Value, &'static str> {
    workspace::validate(&method, &params)?;
    let state = state.inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut backend = state
            .backend
            .try_lock()
            .map_err(|_| "The sidecar is busy. Retry the request.")?;
        if backend.closing {
            return Err("The Corinthos desktop is closing.");
        }
        let (sidecar, handshake) = backend
            .process
            .as_mut()
            .ok_or("The Corinthos sidecar is disconnected.")?;
        if !handshake.supports(&method) {
            return Err("That workspace operation is unavailable.");
        }
        sidecar.request(&method, params)
    })
    .await
    .map_err(|_| "The sidecar bridge could not complete the request.")?
}

#[tauri::command]
async fn select_project_directory(window: tauri::WebviewWindow) -> Result<Option<String>, &'static str> {
    use corinthos_desktop::directory_picker;
    if window.label() != "main" {
        return Err(directory_picker::UNAVAILABLE);
    }
    #[cfg(windows)]
    {
        let (sender, mut receiver) = tauri::async_runtime::channel(1);
        std::thread::Builder::new().name("corinthos-folder-picker".to_owned()).spawn(move || {
            let selected = window.hwnd().map_err(|_| directory_picker::FAILED)
                .and_then(directory_picker::select);
            let _ = sender.blocking_send(selected);
        }).map_err(|_| directory_picker::FAILED)?;
        receiver.recv().await.ok_or(directory_picker::FAILED)?
    }
    #[cfg(not(windows))]
    Err(directory_picker::UNAVAILABLE)
}

pub fn run() {
    let app = tauri::Builder::default()
        .manage(DesktopState::default())
        .setup(|app| {
            let config = app.config();
            let allowed_development_origin = if cfg!(debug_assertions) && tauri::is_dev() {
                config.build.dev_url.as_ref().map(Url::origin)
            } else {
                None
            };
            let window = WebviewWindowBuilder::from_config(app, &config.app.windows[0])?
                .on_navigation(move |url| {
                    local_asset(url)
                        || allowed_development_origin
                            .as_ref()
                            .is_some_and(|origin| *origin == url.origin())
                })
                .on_new_window(|_, _| NewWindowResponse::Deny)
                .on_download(|_, event| !matches!(event, DownloadEvent::Requested { .. }))
                .devtools(cfg!(debug_assertions));
            #[cfg(feature = "desktop-smoke")]
            let window = crate::desktop_smoke::with_probe(window)?;
            window.build()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_snapshot,
            workspace_request,
            restart_backend,
            select_project_directory,
        ])
        .build(tauri::generate_context!())
        .expect("could not build the Corinthos desktop application");
    app.run(|app, event| {
        if matches!(event, RunEvent::Exit) {
            let state = app.state::<DesktopState>();
            if let Ok(mut backend) = state.backend.lock() {
                backend.closing = true;
                state.generation.fetch_add(1, Ordering::AcqRel);
                if let Some((mut sidecar, _)) = backend.process.take() {
                    let _ = sidecar.shutdown();
                }
            }
        }
    });
}

pub fn local_asset(url: &Url) -> bool {
    url.username().is_empty()
        && url.password().is_none()
        && matches!(
            (url.scheme(), url.host_str(), url.port()),
            ("http" | "https", Some("tauri.localhost"), None) | ("tauri", Some("localhost"), None)
        )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity() -> DesktopIdentity {
        DesktopIdentity {
            version: "0.1.0".into(),
            identifier: "org.corinthos.desktop".into(),
        }
    }

    #[test]
    fn startup_failure_retains_native_identity_and_a_typed_origin() {
        let value = serde_json::to_value(Snapshot::failure(
            identity(),
            FailureOrigin::Startup,
            "Runtime missing",
        ))
        .unwrap();
        assert_eq!(value["connection"], "unavailable");
        assert_eq!(value["failure_origin"], "startup");
        assert_eq!(value["desktop"]["identifier"], "org.corinthos.desktop");
        assert_eq!(value["desktop"]["version"], "0.1.0");
        assert!(value.get("handshake").is_none());
    }

    #[test]
    fn established_transport_failure_is_disconnected() {
        let value = serde_json::to_value(Snapshot::failure(
            identity(),
            FailureOrigin::Transport,
            "Connection closed",
        ))
        .unwrap();
        assert_eq!(value["connection"], "disconnected");
        assert_eq!(value["failure_origin"], "transport");
    }
}
