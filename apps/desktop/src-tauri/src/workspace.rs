// SPDX-License-Identifier: MPL-2.0
//! Desktop-only request admission. The Python application validates domain inputs.

use serde_json::Value;

const WORKSPACE_METHODS: &[&str] = &[
    "command.overview",
    "command.project",
    "settings.providers",
    "providers.refresh",
    "provider.configure",
    "provider.reset",
    "review.list",
    "review.get",
    "review.defer",
    "projects.list",
    "project.summary",
    "project.open",
    "import.inspect",
    "import.commit",
    "import.status",
    "work.tree",
    "work.get",
    "work.create",
    "work.status",
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

pub fn validate(method: &str, params: &Value) -> Result<(), &'static str> {
    if !WORKSPACE_METHODS.contains(&method) || !params.is_object() {
        return Err("That workspace operation is unavailable.");
    }
    if let Some(root) = params.get("root")
        && !root.as_str().is_some_and(valid_root)
    {
        return Err("Enter an absolute local project directory.");
    }
    Ok(())
}

fn valid_root(root: &str) -> bool {
    if root.chars().any(char::is_control) || root.chars().count() > 4096 {
        return false;
    }
    if cfg!(windows) {
        return matches!(root.as_bytes(), [drive, b':', b'/' | b'\\', ..] if drive.is_ascii_alphabetic())
            && !root[2..].contains(':')
            && !root[2..].split(['/', '\\']).any(|part| part == "..");
    }
    std::path::Path::new(root).is_absolute()
        && !root.split('/').any(|part| part == "..")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn workspace_allowlist_matches_typed_frontend_operations() {
        let source = include_str!("../../src/bridge.ts");
        let mut used = Vec::new();
        for statement in source.lines().filter(|line| line.contains("request<")) {
            if let Some((_, arguments)) = statement.rsplit_once(">(") {
                for value in arguments.split('"').skip(1).step_by(2) {
                    if value.contains('.')
                        && value.bytes().all(|byte| byte.is_ascii_lowercase() || byte == b'.' || byte == b'_')
                    {
                        used.push(value);
                    }
                }
            }
        }
        used.sort_unstable();
        used.dedup();
        let mut admitted = WORKSPACE_METHODS.to_vec();
        admitted.sort_unstable();
        assert_eq!(used, admitted);
    }

    #[test]
    fn desktop_disallows_unlisted_methods_and_rejects_nonlocal_roots() {
        assert!(validate("system.shutdown", &json!({})).is_err());
        assert!(validate("work.get", &json!([])).is_err());
        assert!(validate("settings.providers", &json!({})).is_ok());
        for root in [json!(null), json!(""), json!("relative"), json!(123), json!("\0")] {
            assert!(validate("project.summary", &json!({"root": root})).is_err());
        }
        let (local, traversal) = if cfg!(windows) {
            ("C:\\Users\\Workspace", "C:/Users/../private")
        } else {
            ("/home/user/workspace", "/home/user/../private")
        };
        assert!(validate("project.summary", &json!({"root": local})).is_ok());
        assert!(validate("project.summary", &json!({"root": traversal})).is_err());
        #[cfg(windows)]
        for root in [r"\\host\share\project", r"\\?\C:\project", "C:relative", "C:/dir:stream"] {
            assert!(validate("project.summary", &json!({"root": root})).is_err());
        }
    }

    #[test]
    fn application_capability_and_production_policy_remain_local_and_explicit() {
        let config: Value = serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let app = &config["app"];
        assert_eq!(app["withGlobalTauri"], false);
        assert_eq!(app["windows"].as_array().unwrap().len(), 1);
        assert_eq!(app["windows"][0]["label"], "main");
        assert!(app["windows"][0].get("url").is_none());
        let security = &app["security"];
        assert_eq!(security["capabilities"], json!(["status-panel"]));
        assert_eq!(security["assetProtocol"], json!({"enable": false, "scope": []}));
        let policy = security["csp"].as_str().unwrap();
        for expected in [
            "default-src 'none'", "script-src 'self'", "style-src 'self'",
            "connect-src ipc: http://ipc.localhost", "frame-src 'none'", "object-src 'none'",
            "base-uri 'none'", "frame-ancestors 'none'", "form-action 'none'",
        ] {
            assert!(policy.split("; ").any(|directive| directive == expected), "{expected}");
        }
        for unexpected in ["'unsafe-eval'", "'unsafe-inline'", "https:"] {
            assert!(!policy.contains(unexpected));
        }
        let capability: Value =
            serde_json::from_str(include_str!("../capabilities/status-panel.json")).unwrap();
        assert_eq!(capability["windows"], json!(["main"]));
        assert!(capability.get("remote").is_none());
        assert_eq!(
            capability["permissions"],
            json!([
                "allow-backend-snapshot",
                "allow-workspace-request",
                "allow-restart-backend",
                "core:event:allow-listen",
                "core:event:allow-unlisten",
            ]),
        );
    }
}
