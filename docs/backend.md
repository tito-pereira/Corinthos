# Replacement backend operations

All project calls require an explicit `root`. Skill runs accept optional positive `group`, provider name or explicit command vector, `instruction`, relative `claims` and timeout. CLI help gives exact argument syntax. Protocol 2 uses bounded JSON-RPC 2.0 JSON Lines; handshake is required, mutations require request IDs and unknown/removed methods return method-not-found.

| RPC method | Parameters | Result |
| --- | --- | --- |
| `system.handshake` | `protocol_version` | Version and advertised methods |
| `system.version`, `system.diagnostics` | none | Package/runtime and workspace format |
| `system.shutdown` | none | Closes peer; detached workers remain accounted for |
| `project.init`, `project.open`, `project.status` | `root` | Root, project UUID, workspace format |
| `projects.list` | none | Recent project navigation hints |
| `providers.status` | optional `root` | Configured explicit command vectors |
| `provider.configure` | `provider`, `command` array | Saved provider choices |
| `skill.list`, `skill.show` | `root`; `name` for show | Local skills/instructions |
| `skill.run` | `root`, `skill`; optional provider/command/group/instruction/claims/timeout | Run identity and initial status |
| `run.get`, `run.cancel`, `run.integrate` | `root`, `run_id` | Status or integration paths |
| `run.list` | `root` | Project-bound run records |
| `run.discard` | `root`, `run_id`, `summary` | Retains summary before removing terminal artifacts |
| `continuity.show`, `continuity.save` | `root`; supplied current fields for save | Bounded current context |
| `cleanup` | `root`, `kind`, `summary`; `group` for reports/group, optional `resolved_decisions` | Preserved continuity and cleanup result |
| `update.check`, `update.start`, `update.status`, `update.finish` | Existing operation ID/restart fields | Retained installed-app Update projection |

Runs observe `STARTING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` or `INTERRUPTED`. Status includes isolated workspace/output paths, original project/group, declared claims and error. On reconnect, owner locks distinguish live workers from interrupted records; a PID is not authority. Use explicit cancellation and wait for terminal state before cleanup/discard.

Health mode exposes only handshake/version/shutdown and bypasses ordinary project/provider/runtime construction. Protocol 1 retains only the core method names; replacement project result shapes are not the deleted Engine schemas. Deferred old GUI views must not infer support from method names alone; the current shell gates on the replacement `skill.run` capability.

The native `workspace_request` allowlist admits the replacement operations and rejects removed Engine names. `update.finish` remains available only through its separate origin-checked native handoff. The current desktop uses retained shell/Update operations; screen bindings for skill/project commands remain deferred.
