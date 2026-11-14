// SPDX-License-Identifier: MPL-2.0
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod desktop;
#[cfg(feature = "desktop-smoke")]
mod desktop_smoke;

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    if arguments
        .next()
        .is_some_and(|argument| argument == "--installation-check")
    {
        let passed = arguments.next().is_none()
            && corinthos_desktop::sidecar::Sidecar::check_installation().is_ok();
        std::process::exit(if passed { 0 } else { 1 });
    }
    #[cfg(windows)]
    for (name, _) in std::env::vars_os() {
        if name
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("WEBVIEW2_")
        {
            // Windows environment updates are thread-safe. Release uses the installed WebView2.
            unsafe { std::env::remove_var(name) };
        }
    }
    desktop::run();
}
