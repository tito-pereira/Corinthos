// SPDX-License-Identifier: MPL-2.0
use corinthos_desktop::sidecar::{EventSink, Handshake, Sidecar};
use serde::Serialize;
use serde_json::Value;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU64, Ordering},
};
use tauri::{AppHandle, Emitter, Manager, RunEvent, State, WebviewWindowBuilder};

const WORKSPACE_METHODS: &[&str] = &[
    "projects.list",
    "project.summary",
    "project.open",
    "import.inspect",
    "import.commit",
    "import.status",
    "import.refresh",
    "import.files",
    "work.tree",
    "work.get",
    "work.create",
    "work.status",
    "scope.assess",
    "scope.set",
    "flow.status",
    "flow.start",
    "flow.complete",
    "work.report",
    "session.start",
    "session.list",
    "session.get",
    "session.events",
    "session.cancel",
    "session.recover",
    "context.create",
    "context.get",
    "context.list",
    "context.sources",
    "context.verify",
    "context.for_session",
    "handoff.create",
    "handoff.get",
    "handoff.list",
    "switch.status",
    "switch.execute",
];

#[derive(Default)]
struct Backend {
    process: Option<(Sidecar, Handshake)>,
    attempted_start: bool,
    closing: bool,
}

#[derive(Clone, Default)]
struct DesktopState {
    backend: Arc<Mutex<Backend>>,
    generation: Arc<AtomicU64>,
}

#[derive(Serialize)]
struct Snapshot {
    connection: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    handshake: Option<Handshake>,
    #[serde(skip_serializing_if = "Option::is_none")]
    providers: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<&'static str>,
}

impl Snapshot {
    fn disconnected(error: &'static str) -> Self {
        Self {
            connection: "disconnected",
            handshake: None,
            providers: None,
            error: Some(error),
        }
    }
}

async fn snapshot(app: AppHandle, state: DesktopState, restart: bool) -> Snapshot {
    tauri::async_runtime::spawn_blocking(move || {
        let Ok(mut backend) = state.backend.try_lock() else {
            return Snapshot::disconnected("The sidecar is busy. Retry the request.");
        };
        if backend.closing {
            return Snapshot::disconnected("The Corinthos desktop is closing.");
        }
        if restart {
            state.generation.fetch_add(1, Ordering::AcqRel);
            if let Some((mut sidecar, _)) = backend.process.take()
                && let Err(error) = sidecar.shutdown()
            {
                return Snapshot::disconnected(error);
            }
            backend.attempted_start = false;
        }
        if !backend.attempted_start {
            backend.attempted_start = true;
            let epoch = state.generation.load(Ordering::Acquire);
            let generation = Arc::clone(&state.generation);
            let events: EventSink = Arc::new(move |event| {
                if epoch == generation.load(Ordering::Acquire) {
                    let _ = app.emit("backend-lifecycle", event);
                }
            });
            match Sidecar::start(events) {
                Ok(process) => backend.process = Some(process),
                Err(error) => return Snapshot::disconnected(error),
            }
        }
        if let Some((sidecar, handshake)) = backend.process.as_mut()
            && sidecar.is_running()
        {
            return match sidecar.providers_status() {
                Ok(providers) => Snapshot {
                    connection: "ready",
                    handshake: Some(handshake.clone()),
                    providers: Some(providers),
                    error: None,
                },
                Err(error) => Snapshot::disconnected(error),
            };
        }
        Snapshot::disconnected("The Corinthos sidecar is disconnected.")
    })
    .await
    .unwrap_or_else(|_| {
        Snapshot::disconnected("The sidecar bridge could not complete the request.")
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
    if !WORKSPACE_METHODS.contains(&method.as_str()) || !params.is_object() {
        return Err("That workspace operation is unavailable.");
    }
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

pub fn run() {
    let app = tauri::Builder::default()
        .manage(DesktopState::default())
        .setup(|app| {
            let window = WebviewWindowBuilder::from_config(app, &app.config().app.windows[0])?;
            #[cfg(feature = "desktop-smoke")]
            let window = crate::desktop_smoke::with_probe(window)?;
            window.build()?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            backend_snapshot,
            workspace_request,
            restart_backend,
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
