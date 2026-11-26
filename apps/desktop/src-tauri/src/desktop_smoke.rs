// SPDX-License-Identifier: MPL-2.0
//! A passive current-GUI observer in explicitly enabled disposable test builds.
use std::{env, fs::OpenOptions, io::{self, Write}, path::PathBuf, sync::{Arc, atomic::{AtomicBool, Ordering}}};
use tauri::{Runtime, WebviewWindowBuilder, webview::PageLoadEvent};

pub fn with_probe<'a, R: Runtime, M: tauri::Manager<R>>(window: WebviewWindowBuilder<'a, R, M>) -> io::Result<WebviewWindowBuilder<'a, R, M>> {
    let (project, receipt) = match (env::var_os("CORINTHOS_DESKTOP_SMOKE_PROJECT"), env::var_os("CORINTHOS_DESKTOP_SMOKE_RECEIPT")) {
        (None, None) => return Ok(window),
        (Some(project), Some(receipt)) => (PathBuf::from(project), PathBuf::from(receipt)),
        _ => return Err(invalid_fixture()),
    };
    if !project.is_absolute() || !project.is_dir() || receipt.parent() != project.parent() || receipt.extension().is_none_or(|value| value != "txt") || receipt.try_exists()? {
        return Err(invalid_fixture());
    }
    let window = window.data_directory(project.parent().ok_or_else(invalid_fixture)?.join("fixture-webview"));
    if env::var("CORINTHOS_DESKTOP_SMOKE_START").as_deref() == Ok("picker-small") { return Ok(window.inner_size(620.0, 480.0)); }
    if env::var("CORINTHOS_DESKTOP_SMOKE_START").as_deref() == Ok("picker") { return Ok(window); }
    let injected = Arc::new(AtomicBool::new(false));
    let can_report = Arc::clone(&injected);
    Ok(window.on_page_load(move |window,event| {
        if event.event() == PageLoadEvent::Finished && crate::desktop::local_asset(event.url()) && !injected.swap(true, Ordering::AcqRel) {
            let _ = window.eval(include_str!("../tests/desktop-smoke.js"));
        }
    }).on_document_title_changed(move |_window,title| {
        if !can_report.load(Ordering::Acquire) { return; }
        let outcome = match title.as_str() {
            "Corinthos desktop smoke: pass" => "pass\n",
            "Corinthos desktop smoke: fail [layout]" => "fail [layout]\n",
            "Corinthos desktop smoke: fail [backend]" => "fail [backend]\n",
            _ => return,
        };
        if let Ok(mut file) = OpenOptions::new().create_new(true).write(true).open(&receipt) { let _ = file.write_all(outcome.as_bytes()); }
    }))
}
fn invalid_fixture() -> io::Error { io::Error::new(io::ErrorKind::InvalidInput,"Select an absolute disposable project and a new sibling text receipt.") }
