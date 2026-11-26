// SPDX-License-Identifier: MPL-2.0
//! Desktop-only request admission. Python validates operation inputs.
use serde_json::Value;

const WORKSPACE_METHODS: &[&str] = &[
    "system.version", "system.diagnostics", "project.init", "project.open", "project.status",
    "projects.list", "providers.status", "provider.configure", "skill.list", "skill.show",
    "skill.run", "run.get", "run.list", "run.cancel", "run.integrate", "run.discard",
    "continuity.show", "continuity.save", "cleanup", "update.check", "update.start", "update.status",
];

pub fn validate(method: &str, params: &Value) -> Result<(), &'static str> {
    if !WORKSPACE_METHODS.contains(&method) || !params.is_object() {
        return Err("That workspace operation is unavailable.");
    }
    if matches!(method, "system.version" | "system.diagnostics") && params.as_object().is_none_or(|value| !value.is_empty()) {
        return Err("This system operation does not accept parameters.");
    }
    if method.starts_with("update.") {
        let fields = params.as_object().ok_or("Invalid update request.")?;
        if fields.keys().any(|key| key != "operation_id")
            || (method == "update.check" && !fields.is_empty())
            || (method == "update.start" && !fields.contains_key("operation_id"))
            || fields.get("operation_id").is_some_and(|value| !value.as_str().is_some_and(valid_update_id))
        {
            return Err("Supply a valid update operation identifier.");
        }
    }
    let needs_root = method.starts_with("project.") || method.starts_with("skill.")
        || method.starts_with("run.") || method.starts_with("continuity.") || method == "cleanup";
    if (needs_root && !params.get("root").is_some_and(Value::is_string))
        || params.get("root").is_some_and(|root| !root.as_str().is_some_and(valid_root))
    {
        return Err("Enter an absolute local project directory.");
    }
    Ok(())
}

pub fn valid_update_id(value: &str) -> bool {
    value.len() == 32
        && value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
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
    fn replacement_methods_are_admitted_and_removed_engines_are_rejected() {
        let root = if cfg!(windows) { "C:/project" } else { "/project" };
        for method in ["project.init", "project.open", "skill.run", "run.get", "run.integrate", "continuity.save", "cleanup"] {
            assert!(validate(method, &json!({"root": root})).is_ok(), "{method}");
            assert!(validate(method, &json!({})).is_err(), "{method}");
        }
        for method in ["work.create", "session.start", "context.create", "project.create", "project.open_checked", "system.shutdown", "update.finish"] {
            assert!(validate(method, &json!({"root": root})).is_err(), "{method}");
        }
        assert!(validate("providers.status", &json!({})).is_ok());
        assert!(validate("skill.list", &json!([])).is_err());
        assert!(validate("system.diagnostics", &json!({})).is_ok());
        assert!(validate("system.diagnostics", &json!({"extra": true})).is_err());
    }

    #[test]
    fn update_admission_keeps_finish_behind_its_separate_native_handoff() {
        let operation = "a".repeat(32);
        assert!(validate("update.check", &json!({})).is_ok());
        assert!(validate("update.status", &json!({})).is_ok());
        for method in ["update.start", "update.status"] {
            assert!(validate(method, &json!({"operation_id": operation})).is_ok());
            for value in [json!(null), json!(""), json!("A".repeat(32)), json!("../candidate"), json!(1)] {
                assert!(validate(method, &json!({"operation_id": value})).is_err());
            }
        }
        assert!(validate("update.start", &json!({})).is_err());
        assert!(validate("update.status", &json!({"url": "https://example.test"})).is_err());
        assert!(validate("update.finish", &json!({"operation_id": operation})).is_err());
    }

    #[test]
    fn project_roots_remain_local_and_bounded() {
        let local = if cfg!(windows) { "C:/project" } else { "/project" };
        assert!(validate("project.status", &json!({"root": local})).is_ok());
        for root in [json!(null), json!(""), json!("relative"), json!(123), json!("/a/../b"), json!("\0")] {
            assert!(validate("project.status", &json!({"root": root})).is_err());
        }
        #[cfg(windows)]
        for root in [r"\\host\share\project", r"\\?\C:\project", "C:relative", "C:/dir:stream"] {
            assert!(validate("project.status", &json!({"root": root})).is_err());
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
                "allow-select-project-directory",
                "allow-finish-update",
                "core:event:allow-listen",
                "core:event:allow-unlisten",
            ]),
        );
    }
}
