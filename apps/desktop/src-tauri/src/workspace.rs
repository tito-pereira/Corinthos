// SPDX-License-Identifier: MPL-2.0
//! Desktop-only request admission. The Python application validates domain inputs.

use serde_json::Value;

const WORKSPACE_METHODS: &[&str] = &[
    "system.diagnostics",
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
    "project.create",
    "project.inspect",
    "project.open_checked",
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
    if method == "system.diagnostics" && params.as_object().is_none_or(|value| !value.is_empty()) {
        return Err("Diagnostics does not accept parameters.");
    }
    if method == "project.create"
        && (params.as_object().is_none_or(|value| value.len() != 2)
            || !params.get("root").is_some_and(Value::is_string)
            || !params.get("display_name").and_then(Value::as_str).is_some_and(|name| {
                !name.trim().is_empty() && name.chars().count() <= 128
            }))
    {
        return Err("PROJECT_CREATE_INVALID_INPUT");
    }
    if method == "project.inspect"
        && (params.as_object().is_none_or(|value| value.len() != 1)
            || !params.get("root").is_some_and(Value::is_string))
    {
        return Err("Supply a project directory to inspect.");
    }
    if method == "project.open_checked"
        && (params.as_object().is_none_or(|value| {
            value
                .keys()
                .any(|key| !["root", "inspection_digest", "migrate"].contains(&key.as_str()))
        }) || !params.get("root").is_some_and(Value::is_string)
            || !params
                .get("inspection_digest")
                .and_then(Value::as_str)
                .is_some_and(|digest| {
                    digest.len() == 64
                        && digest
                            .bytes()
                            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                })
            || params
                .get("migrate")
                .is_some_and(|value| !value.is_boolean()))
    {
        return Err("Supply a current project inspection.");
    }
    if let Some(root) = params.get("root")
        && !root.as_str().is_some_and(valid_root)
    {
        return Err(if method == "project.create" {
            "PROJECT_CREATE_INVALID_INPUT"
        } else {
            "Enter an absolute local project directory."
        });
    }
    Ok(())
}

pub(crate) fn valid_root(root: &str) -> bool {
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
    fn diagnostics_admits_only_an_empty_projection_request() {
        assert!(validate("system.diagnostics", &json!({})).is_ok());
        assert!(validate("system.diagnostics", &json!({"root": "/project"})).is_err());
        assert!(validate("system.diagnostics", &json!([])).is_err());
    }

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
    fn project_creation_admits_only_its_two_bounded_fields() {
        let root = if cfg!(windows) { "C:/project" } else { "/project" };
        assert!(validate("project.create", &json!({"root": root, "display_name": "Café"})).is_ok());
        for params in [
            json!({"root": root}),
            json!({"root": root, "display_name": 1}),
            json!({"root": root, "display_name": "   "}),
            json!({"root": root, "display_name": "x".repeat(129)}),
            json!({"root": root, "display_name": "Name", "extra": true}),
            json!({"root": "relative", "display_name": "Name"}),
        ] {
            assert_eq!(validate("project.create", &params), Err("PROJECT_CREATE_INVALID_INPUT"));
        }
    }

    #[test]
    fn inspected_open_requires_a_bounded_digest_and_explicit_boolean_consent() {
        let root = if cfg!(windows) {
            "C:/project"
        } else {
            "/project"
        };
        let digest = "a".repeat(64);
        assert!(validate("project.inspect", &json!({"root": root})).is_ok());
        assert!(
            validate(
                "project.open_checked",
                &json!({
                    "root": root, "inspection_digest": digest, "migrate": true
                })
            )
            .is_ok()
        );
        for params in [
            json!({"root": root}),
            json!({"root": root, "inspection_digest": "bad"}),
            json!({"root": root, "inspection_digest": "A".repeat(64)}),
            json!({"root": root, "inspection_digest": digest, "migrate": "true"}),
            json!({"root": root, "inspection_digest": digest, "extra": true}),
        ] {
            assert!(validate("project.open_checked", &params).is_err());
        }
        assert!(validate("project.inspect", &json!({"root": root, "migrate": true})).is_err());
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
                "allow-select-project-directory",
                "core:event:allow-listen",
                "core:event:allow-unlisten",
            ]),
        );
    }
}
