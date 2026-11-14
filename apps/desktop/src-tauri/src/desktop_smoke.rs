// SPDX-License-Identifier: MPL-2.0
//! Observes the real WebView in an explicitly enabled desktop test build.

use std::env;
use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use tauri::webview::PageLoadEvent;
use tauri::{Runtime, Url, WebviewWindowBuilder};

pub fn with_probe<'a, R: Runtime, M: tauri::Manager<R>>(
    window: WebviewWindowBuilder<'a, R, M>,
) -> io::Result<WebviewWindowBuilder<'a, R, M>> {
    let project = env::var_os("CORINTHOS_DESKTOP_SMOKE_PROJECT");
    let receipt = env::var_os("CORINTHOS_DESKTOP_SMOKE_RECEIPT");
    let (project, receipt) = match (project, receipt) {
        (None, None) => return Ok(window),
        (Some(project), Some(receipt)) => (PathBuf::from(project), PathBuf::from(receipt)),
        _ => return Err(invalid_fixture()),
    };
    let project_text = project.to_str().ok_or_else(invalid_fixture)?;
    if !project.is_absolute()
        || !project.is_dir()
        || project_text.chars().count() > 4096
        || receipt.parent() != project.parent()
        || receipt.extension().is_none_or(|ext| ext != "txt")
        || receipt.try_exists()?
    {
        return Err(invalid_fixture());
    }
    let mode = match env::var("CORINTHOS_DESKTOP_SMOKE_START").as_deref() {
        Ok("adaptive") => "adaptive",
        Ok("adaptive-passive") => "adaptive-passive",
        Ok("import-preview") => "import-preview",
        Ok("import-commit") => "import-commit",
        Ok("import-execute") => "import-execute",
        Ok("1") => "sessions",
        _ => "passive",
    };
    let script = format!(
        "({})({}, \"{mode}\");",
        include_str!("../tests/desktop-smoke.js"),
        serde_json::to_string(project_text)?
    );
    let injected = Arc::new(AtomicBool::new(false));
    let can_report = Arc::clone(&injected);
    Ok(window
        .on_navigation(local_asset)
        .on_page_load(move |window, event| {
            if event.event() == PageLoadEvent::Finished
                && local_asset(event.url())
                && !injected.swap(true, Ordering::AcqRel)
            {
                let _ = window.eval(&script);
            }
        })
        .on_document_title_changed(move |_window, title| {
            if !can_report.load(Ordering::Acquire) {
                return;
            }
            let outcome = match title.as_str() {
                "Corinthos desktop smoke: pass" => "pass\n",
                "Corinthos desktop smoke: fail [backend]" => "fail [backend]\n",
                "Corinthos desktop smoke: fail [registry]" => "fail [registry]\n",
                "Corinthos desktop smoke: fail [project]" => "fail [project]\n",
                "Corinthos desktop smoke: fail [agents]" => "fail [agents]\n",
                "Corinthos desktop smoke: fail [context]" => "fail [context]\n",
                "Corinthos desktop smoke: fail [verify]" => "fail [verify]\n",
                "Corinthos desktop smoke: fail [binding]" => "fail [binding]\n",
                "Corinthos desktop smoke: fail [handoff]" => "fail [handoff]\n",
                "Corinthos desktop smoke: fail [start]" => "fail [start]\n",
                "Corinthos desktop smoke: fail [cancel]" => "fail [cancel]\n",
                "Corinthos desktop smoke: fail [timeout]" => "fail [timeout]\n",
                "Corinthos desktop smoke: fail [switch]" => "fail [switch]\n",
                "Corinthos desktop smoke: fail [replay]" => "fail [replay]\n",
                "Corinthos desktop smoke: fail [target-selection]" => "fail [target-selection]\n",
                "Corinthos desktop smoke: fail [target-context]" => "fail [target-context]\n",
                "Corinthos desktop smoke: fail [target-output]" => "fail [target-output]\n",
                "Corinthos desktop smoke: fail [adaptive-open-form]" => "fail [adaptive-open-form]\n",
                "Corinthos desktop smoke: fail [adaptive-open-project]" => "fail [adaptive-open-project]\n",
                "Corinthos desktop smoke: fail [adaptive-storage]" => "fail [adaptive-storage]\n",
                "Corinthos desktop smoke: fail [adaptive-work-view]" => "fail [adaptive-work-view]\n",
                "Corinthos desktop smoke: fail [adaptive-create-project]" => "fail [adaptive-create-project]\n",
                "Corinthos desktop smoke: fail [adaptive-planning-gate]" => "fail [adaptive-planning-gate]\n",
                "Corinthos desktop smoke: fail [adaptive-task]" => "fail [adaptive-task]\n",
                "Corinthos desktop smoke: fail [adaptive-session]" => "fail [adaptive-session]\n",
                "Corinthos desktop smoke: fail [adaptive-report]" => "fail [adaptive-report]\n",
                "Corinthos desktop smoke: fail [import-preview]" => "fail [import-preview]\n",
                "Corinthos desktop smoke: fail [import-commit]" => "fail [import-commit]\n",
                "Corinthos desktop smoke: fail [import-project]" => "fail [import-project]\n",
                "Corinthos desktop smoke: fail [import-work]" => "fail [import-work]\n",
                "Corinthos desktop smoke: fail [import-session]" => "fail [import-session]\n",
                "Corinthos desktop smoke: fail [import-context]" => "fail [import-context]\n",
                "Corinthos desktop smoke: fail [unexpected]" => "fail [unexpected]\n",
                _ => return,
            };
            if let Ok(mut file) = OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&receipt)
            {
                let _ = file.write_all(outcome.as_bytes());
            }
        }))
}

fn local_asset(url: &Url) -> bool {
    matches!(
        (url.scheme(), url.host_str(), url.port()),
        ("http" | "https", Some("tauri.localhost"), None) | ("tauri", Some("localhost"), None)
    )
}

fn invalid_fixture() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "The desktop smoke requires an absolute test project and a new sibling .txt receipt.",
    )
}
