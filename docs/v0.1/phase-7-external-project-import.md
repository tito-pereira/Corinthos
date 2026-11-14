# v0.1 Phase 7 external project import

Import an existing local Git repository to use it with Corinthos Work Items,
Context and Agent Sessions. Import has three authority labels.

| Label | Meaning |
| --- | --- |
| `DISCOVERED` | Bounded observations from Git, tracked files and supported static manifests. |
| `CONFIRMED` | The operator accepted the exact discovery and its required confirmations for Corinthos management. |
| `CANONICAL` | Authority established by the existing contract publication and approval flow. |

Import does not approve architecture. Repository observations can inform work,
while Project, Workflow, Module and Technology Decision authority comes from
their respective approved contracts.

## Import from Projects

1. In **Projects**, open **Import existing repository**. Enter the exact root
   of an existing local worktree with an ordinary `.git` directory. Corinthos
   requires a native `git` executable on PATH outside the selected repository.
2. Select **Inspect repository**. Review the `DISCOVERED` Git root, HEAD,
   branch, worktree state, languages, manifests, documentation, tests, candidate
   modules, exclusions and ambiguities. The preview shows the discovery digest.
3. Acknowledge each required ambiguity you accept and select **Import
   repository**. Corinthos submits the reviewed digest and confirmation codes.
   A successful import reports a `CONFIRMED` snapshot and opens the project
   when the backend supports opening it.
4. In **Work Items**, create a `TASK` or a bounded `CHANGE`. Include relevant
   repository terms or paths in the objective and acceptance criteria. Use
   **Adaptive workflow** to inspect the gates and explicitly select a provider
   when the Work Item is eligible. [Adaptive Workflow](phase-6-adaptive-workflow.md)

Inspection and preview perform no project or registry writes. Import does not
execute project code, tests, build tools, package managers or providers. The
first provider execution is a separate explicit action after import. Existing
Git state, source bytes, `.gitignore`, `AGENTS.md` and `CLAUDE.md` are preserved
through import confirmation.

## Terminal workflow

Replace `ROOT` with the local repository path. Take `DISCOVERY_DIGEST` directly
from the inspection output. Add each required code as a separate `--confirm`
argument. This example assumes the only required code is `DIRTY_TRACKED`.

```text
corinthos import inspect ROOT
corinthos import commit ROOT --digest DISCOVERY_DIGEST --confirm DIRTY_TRACKED
corinthos import status --root ROOT
corinthos import files --root ROOT --limit 10 --offset 0
```

Inspection and file inventory responses are paginated. Follow `next_offset`
until it is null. The discovery digest describes the complete bounded scan,
independent of the returned page. `import files` reads the saved inventory and
accepts `--snapshot-digest` to select an earlier snapshot. `import status`
verifies the recorded import without rescanning Git or running providers.

For an explicit refresh, inspect the tree again and use the current saved
snapshot digest from status alongside the newly reviewed discovery digest.
Supply the confirmation codes required by the new inspection.

```text
corinthos import inspect ROOT
corinthos import status --root ROOT
corinthos import refresh --root ROOT --expected-snapshot-digest CURRENT_SNAPSHOT_DIGEST --digest DISCOVERY_DIGEST
```

Refresh checks that the expected snapshot is still current. An equivalent
refresh reuses the current snapshot. A changed observation creates a new
snapshot and updates the project's import summary. Old snapshots and existing
Context Projections remain available. Newly tracked files become candidates for
Context only after an explicit refresh. Opening the project performs no refresh.

## Discovery policy v1

Discovery uses bounded native Git index and tree reads from isolated temporary
metadata outside the repository. It compares the current HEAD, branch and
index before returning. The SHA-256 `discovery_digest` covers canonical
repository observations, including HEAD and branch identity, index and staged
facts, verifiable worktree bytes, untracked inventory and the policy version.
It excludes the local root, existing Corinthos state and incidental timestamps.
Commit rescans against the supplied digest before initialization and checks it
again before recording the import. An old digest is rejected when tracked state
or another included observation changes.

| Boundary | Policy v1 behavior |
| --- | --- |
| Repository type | Ordinary local SHA-1 Git worktree with a compatible index and independent local objects. Bare, linked and sparse worktrees, network paths, redirected metadata, shared or alternate object storage and partial clones are unsupported. |
| Inventory | At most 2,048 tracked paths and 8,192 worktree entries. The index is limited to 8 MiB and Git object storage to 65,536 entries and 512 MiB. Git has an eight-second command timeout and a 2 MiB output bound. A bound or unsafe Git layout can reject a scan. |
| File content | At most 64 KiB per eligible file and 8 MiB total content read. Large and binary files supply metadata. Unverifiable tracked state blocks import. |
| Tracked changes | Staged or tracked worktree changes require explicit `DIRTY_TRACKED` confirmation. Discovery compares raw bytes without running Git filters, which can report differences in a line-ending-normalized checkout. |
| Untracked files | Counts and inventory digest only, including ignored paths. Their contents are excluded from the scan and Context. |
| Links and children | The scan avoids symlink and reparse traversal and records Git submodules as metadata. Nested repositories are identified without traversal. |
| Private storage | Any top-level `.corinthos` entry in the index or HEAD prevents import, even when the working-tree copy is absent. |
| Sensitive files | Recognized sensitive paths are excluded from content reads and their path identities are suppressed in the public observation. Excerpts are redacted before truncation. |
| Manifests | Static parsing extracts allowlisted names and limited declarations from `package.json`, `pyproject.toml`, `Cargo.toml`, simple `requirements*.txt` and `go.mod`. Additional recognized manifests can appear as metadata. Script names are observations. |

Check the ambiguity list before confirming. In addition to dirty tracked
state, nested Git roots, submodules and multiple named manifests or module
roots can require confirmation. Candidate modules and manifest dependencies
remain repository observations. Discovery supports no remote clone, network
fetch or submodule initialization.

## Saved state and recovery

Successful confirmation allocates or reuses one operational project identity,
initializes the private Standard profile and SQLite v9, and saves an immutable
snapshot in `.corinthos/snapshots/`. Snapshot files are addressed by digest.
Lifecycle records hold the import or refresh summary and snapshot reference.
The Project Registry records successful imports and Project State presents the
bounded import summary. Import publishes no contracts or provider instructions.

On Windows and Linux, import prepares the intent in temporary storage outside
the repository on the same filesystem, then atomically publishes the private
directory without replacing existing state. An interruption after this point
leaves a recoverable intent. Git editors remain free to change the worktree,
including after the final scan. The snapshot records what the bounded final
scan observed. An explicit refresh can capture later changes.

| Status or error | What to do |
| --- | --- |
| `REPOSITORY_CHANGED` | Inspect again and review the new digest and confirmations before submitting. |
| `CONFIRMATION_REQUIRED` | Inspect the ambiguity list and submit the required codes you accept. |
| `ALREADY_MANAGED` | Open the existing Corinthos project. An exact replay of a successful import can reuse its identity and retry registry registration. |
| `INCOMPLETE` | Preserve private state. Retry with the original confirmed digest and codes if discovery still matches. A different pending intent produces `IMPORT_INTENT_CHANGED` and requires inspection before any recovery. |
| `INVALID` | Preserve the existing private state for inspection. The importer refuses to replace unverified state. |
| `REGISTRY_UNAVAILABLE` | The confirmed local import exists. Retry opening the project or replay its exact original import to retry registration. |
| `SNAPSHOT_CHANGED` | Read status and inspect again before refreshing against the current snapshot. |
| `SNAPSHOT_LIMIT` | The project has reached the 32-snapshot limit. Existing history is retained. |

## Repository context and work

The saved inventory makes repository metadata, manifests, documents, source
files and related tests eligible for Context. These sources are `OPERATIONAL`.
Lexical matches and related-test signals are `DERIVED`. Exact canonical
sources, the Work Item and an exact required Handoff take precedence over
imported fallback observations. [Deterministic Context Engine](phase-4-context.md)

For a newly created projection, Context rechecks that candidates from the saved
inventory remain tracked. It captures eligible current file bytes and their
SHA-256, together with the relative path and snapshot digest. The manifest
observation from import retains its original content digest. A removed or
untracked replacement cannot supply fallback context for the old path. If
tracking cannot be verified, Context can retain the repository summary without
file excerpts.

Selector policy v1 takes up to three manifests and three key documents, reads
up to 32 lexical source candidates and selects up to four matching sources and
three related tests, subject to projection budgets. It scores normalized terms
from Work Item objectives and criteria against paths and redacted text, with
stable path tie-breaking. Excerpts are bounded to 24 lines and 4 KiB. Exact
source identities remain auditable after a new snapshot is recorded.

```text
corinthos work create --root ROOT --scope CHANGE --objective "Review the payment retry flow" --criterion "Explain the retry decision in payments.py"
corinthos flow status --root ROOT --work WORK_ID
corinthos flow start --root ROOT --work WORK_ID --provider codex --read-only
corinthos work report --root ROOT --work WORK_ID
```

Create a `TASK` for smaller scope. A `CHANGE` can use repository observations
without fabricated Module or Workflow Contracts. Existing gates still require
approved authority for any affected Modules named on the Work Item. A direct
Agent Session can succeed and produce a report while independent Validation
remains `NOT_VERIFIED`. Work Item completion remains a separate decision.
[Scope and completion](phase-6-adaptive-workflow.md)

## Interface and verification

The Application Layer exposes `import_inspect`, `import_commit`,
`import_status`, `import_refresh` and `import_files`. RPC v2 advertises the
corresponding `import.inspect`, `import.commit`, `import.status`,
`import.refresh` and `import.files` methods. Commit and refresh require request
IDs. RPC v1 remains frozen. The desktop uses feature detection for inspection,
confirmation and status. Refresh and saved inventory inspection are available
through the terminal and RPC.

The Windows WebView fixture covers a dirty external repository, passive preview,
digest confirmation, preservation of tracked bytes and Git state, and a
separately started synthetic `CHANGE` Agent Session that receives the selected
repository marker. The imported Context tests cover provenance, ranking,
tracking changes, refresh and redaction. Live imported-repository execution
requires separate native-provider evidence. The two-real-provider switch gate
remains **NOT VERIFIED / DEFERRED** to v0.1 closure.
[Windows desktop fixture](../../apps/desktop/test-windows.ps1) ·
[Imported Context tests](../../tests/test_import_context.py) · [Provider switch evidence](phase-5-provider-switch.md#verification)
