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

Health mode exposes only handshake/version/shutdown and bypasses ordinary project/provider/runtime construction. Protocol 1 retains only the core method names; replacement project result shapes are not the deleted Engine schemas. The GUI binds only the advertised replacement operations; removed Engine result schemas are not reused.

The native `workspace_request` allowlist admits the replacement operations and rejects removed Engine names. `update.finish` remains available only through its separate origin-checked native handoff. The two-page desktop uses the `gui.*` interactions below and the retained Update operations.


## Desktop interactions

GUI operations return `{ok: true, value: ...}` or a bounded `{ok: false, error: ...}` result so application read/conflict failures remain actionable without exposing transport internals. Mutations still require JSON-RPC request IDs. Native admission lists every supported name explicitly.

| Operations | Purpose |
| --- | --- |
| `gui.projects`, `gui.open`, `gui.import`, `gui.create` | Validated recent hints, enclosing-workspace selection, preserving working-copy import and actual skill-backed creation. |
| `gui.operation.get`, `gui.operation.cancel` | Observe/cancel an opaque, scoped asynchronous request. Copy, provider, integration and publication work stays outside request handling. |
| `gui.conversation.open/get/send/output` | Main or project scope, bounded transcript, real provider admission and cursor-based output chunks. |
| `gui.summary`, `gui.execute` | Fresh read-only ideas/tasks; explicit local orchestrator dispatch or `No Tasks Yet`, including active-run deduplication. |
| `gui.runs`, `gui.run.get/action` | Origin-bound retained runs and existing Apply/Discard/Cancel operations. Status reads fail promptly when integration owns the project lock. |
| `gui.provider.configure` | Reuse the native executable/argument registry, without a separate authentication system. |

Project references carry validated UUID/root metadata; conversations and run actions retain their originating scope across navigation. GUI replies and output chunks remain below the 65,536-byte transport frame. Scope validation never accepts an arbitrary output file path from the WebView. See the [GUI guide](gui.md) for behavior and limits.
