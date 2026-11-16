// SPDX-License-Identifier: MPL-2.0
fn main() {
    #[cfg(feature = "desktop")]
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "backend_snapshot",
            "workspace_request",
            "restart_backend",
            "select_project_directory",
        ]),
    ))
    .expect("desktop configuration must be valid");
}
