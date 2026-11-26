# Corinthos

Corinthos is a local Python application for project workspaces and project-local development skills. One installation serves multiple independent projects. Skills define planning, implementation, independent review, correction and orchestration; the backend supplies processes, isolation, persistence and installed-app Update.

This development snapshot replaces the former Engines, project contracts and approval/publication machinery. Legacy `.corinthos` and `.nukytera` project state is preserved and explicitly refused for execution. Use a separate prototype workspace; no automatic migration is supplied.

## Start from source

Python 3.12 or newer is required. From the repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m corinthos --help
```

On POSIX, use `.venv/bin/python` for the last two commands. Continue with the virtual environment's Python and console command (`.venv/Scripts/corinthos.exe` on Windows or `.venv/bin/corinthos` on POSIX), or activate that environment first. Source commands do not install or replace the desktop app. The Windows shared installer remains `install.ps1`; see [installation and Update](docs/updater.md).

```text
corinthos project init --root <absolute-project-path>
corinthos project open --root <absolute-project-path>
corinthos skill list --root <absolute-project-path>
```

Initialization fills missing blueprint files, including all 17 bundled skills. Existing repository content, context, custom instructions and customized skills remain intact. Project identity and compact continuity live in `context_agents/corinthos/`. Numbered artifacts use `task/task_N/` and `task_agents/task_N_agents/`.

## Run a local skill

Configure a native executable and its arguments as a JSON array. The executable receives a UTF-8 prompt on stdin, runs without a shell and must implement the selected skill. Choose arguments supported by your installed provider. Live Codex/Claude behavior has not been verified for this redesign.

```text
corinthos provider configure local --command-json '["/absolute/provider","argument"]'
corinthos skill run implement-feature --root <project> --group 1 --provider local --claim repo/src --claim task/task_1/implement-report.md
corinthos run list --root <project>
corinthos run get --root <project> <run-id>
corinthos run cancel --root <project> <run-id>
corinthos run integrate --root <project> <run-id>
```

Use your shell's quoting rules for the JSON array. Workers survive a CLI or desktop connection closing and hold the installed-runtime lease until their process trees are reaped. Inspect status/output and cancel explicitly. Independent claims can execute concurrently; overlapping active claims are rejected. Each run receives an isolated workspace. Integration checks all changed paths against its write scopes and original destination fingerprints before copying changes. Exit zero records process success; independent review and product acceptance remain distinct.

Snapshot preparation runs asynchronously; inspect the admitted run for preparation failures. Git-backed source must be clean before isolation; commit or separately preserve changes first. Provider processes are trusted local tools: isolation and ownership checks are coordination, not an operating-system security sandbox. Snapshots omit generated caches and are bounded to 10000 files/128 MiB. Output is bounded to 1 MiB and runs default to one hour. At most 64 retained runs per project are admitted; after retaining results, use `run discard --summary <outcome>` to remove terminal run artifacts.

## Continuity and cleanup

```text
corinthos continuity save --root <project> --pending "Next task" --verification "Relevant checks and limits" --resume "Where to continue"
corinthos cleanup reports --root <project> --group 1 --summary "Completed work and remaining limits"
corinthos cleanup group --root <project> --group 1 --summary "Group result and resume information"
corinthos cleanup planning --root <project> --summary "Documentation result and remaining decisions"
```

Cleanup saves the summary before removing temporary artifacts, preserves pending/blocker/resume fields and refuses active workers. Planning cleanup preserves `DECISIONS.md` by default; `--resolved-decisions` clears it only when the caller has consolidated all resolved and unrelated pending decisions. Bundled cleanup skills use this boundary. Direct external deletion is outside this guarantee.

## Desktop and verification

The Windows desktop has two pages: Main and Project. Main conversation is independent of remembered projects; Project conversation uses its workspace instructions, context and local skills. The left sidebar provides project creation/resumption/import or project ideas/tasks/execution, with Update anchored in its footer. The locally packaged temple/wordmark reference supplies the navy, silver, gold and cyan identity. See the [GUI guide](docs/gui.md).

Creation invokes the real local `create-project` skill, retains name/description and publishes only successful owned staging. Unmanaged source import makes a disclosed independent working copy, preserving the original files and Git metadata. Project conversation/task results stay isolated until Apply Changes succeeds; Discard retains an outcome summary. See [backend operations](docs/backend.md) and [architecture](docs/architecture.md).

```text
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

Current tests cover disposable workspace preservation, controlled real worker overlap/conflicts, skill artifacts, continuity/reopen and retained Update safeguards. Offline controlled providers do not establish live AI-provider correctness. CI additionally checks installed-wheel resources, platform/runtime packaging and the native bridge. No release publication or real user-project migration is part of this snapshot.

Licensed under [MPL-2.0](LICENSE).
