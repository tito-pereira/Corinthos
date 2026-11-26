# Desktop guide

Corinthos has two primary pages. Main shows `Welcome to Corinthos`; Project shows `This is the X project page` using its actual name. Dialogs, recent projects and the owned local folder browser are temporary overlays. Update remains available in the left-sidebar footer on either page.

## Provider and conversations

Select an available configured provider in the composer. Configure provider opens a temporary dialog for a name and native executable/argument JSON array, reusing the existing registry. The executable must accept a UTF-8 prompt on stdin and return readable text; its own ordinary configuration supplies authentication. Windows shell wrappers are refused. Missing or failed providers are reported; product replies are never canned. Controlled-process tests do not establish live Codex/Claude behavior.

Main uses its own neutral application scope and never silently targets a remembered project. Project conversation loads that workspace's README, instructions, canonical context, bounded supporting agent context and local skill paths. Missing/unreadable or omitted context is reported. Switching projects preserves pending work and late output in the original conversation.

Requests are asynchronous and cancellable. Output is bounded to 1 MiB; runs default to one hour. The recent transcript is bounded, with truncation/omission notices and original run artifacts retained until explicit discard. The shared runtime admits at most 64 retained runs per workspace, including the neutral Main workspace; its existing CLI can inspect/discard artifacts after a retained summary. Project run cards expose Apply Changes, Discard and Cancel directly.

## Projects

Create Project has only name and description fields. Its destination hint uses the platform Documents folder and `Corinthos Projects/<name>`. Reserved names, collisions and unsafe paths are rejected. Creation runs the local `create-project` skill in owned staging, integrates and validates its result, retains both fields, then publishes/registers only on success. Cancel before publication leaves the intended destination unchanged. An interrupted owned stage is preserved/recovered only when its UUID/journal match; it never authorizes migration of another project.

Resume displays at most ten recent entries and always offers `...` / `Choose Other`. Unavailable hints are distinct from valid projects. Choose Other and Import use the owned local Windows folder browser. Selecting an inner source directory resolves its enclosing Corinthos workspace, retaining that workspace's instructions and skills.

Importing otherwise unmanaged source creates a disclosed independent working-copy workspace. Ordinary uncommitted/untracked source and custom README/instructions/skills/context/task content are copied; the original is unchanged and its Git metadata/history stays there. Future work changes the new copy. Known generated/cache directories are omitted; unsafe paths or copies exceeding 10,000 files/128 MiB fail rather than publish an incomplete result. Existing Git-backed Corinthos workspaces keep the clean-source isolation requirement; no reset or implicit commit is performed.

## Ideas, tasks and project changes

Future Ideas reads `context/DECISIONS.md`; Task List reads `task/implementation-groups.md` afresh. Both are read-only, summarize actual content and show `Nothing Yet` when absent/empty/no-pending. Access errors or malformed group records remain errors. Merely opening a project, recent list or summary never dispatches work.

Execute Tasks checks current pending/resumable groups and invokes that project's local `orchestrator`, reusing an active run. Otherwise it shows `No Tasks Yet` without dispatch. Process completion is observable separately from whether its changes were applied.

Project conversation/task cards retain the original project/run after navigation. Apply Changes uses existing scope/fingerprint checks and shows applied paths/count or a conflict. Discard retains the supplied outcome summary before deleting isolated artifacts; conversation text remains. Cancel stays pending until ownership/process cleanup is confirmed. A conflict never offers a force overwrite or claims success.

## Installation and evidence

The reference branding is packaged with the desktop; no Pictures path or external fonts are required. Native folder-browser internals follow OS rendering, while application controls/overlays share the navy/silver/gold/cyan theme. Update uses the installed application's signed staging/recovery flow, separate from project `code-updater`.

Use disposable fixtures outside every existing Corinthos workspace ancestor for native/raw-import verification, with separate user-state/WebView/temp paths. The installed-wheel peer and actual native binary must match the feature being observed. Shipping health, mocked UI behavior, controlled provider processes, real desktop observations and live AI-provider behavior are different evidence; do not infer one from another.
