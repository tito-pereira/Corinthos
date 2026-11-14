# Corinthos Command and desktop

The [desktop](phase-1-desktop.md) uses the existing Python Application Layer
through bounded RPC v2. Home shows connection health, saved projects, provider
observations and explicit repository import. Command gives a paginated view of
work across projects. Projects opens or inspects a selected root. Settings
shows cached provider facts and offers explicit refresh, configure and reset.

Project detail has five tabs. Overview shows stored project and workflow
references, operational status and pending decisions. Work retains the tree,
scope assessment, Adaptive Workflow actions and Work Report. Agents shows
sessions, telemetry when observed, and explicit cancel, recover and switch.
Context lists projections and handoffs, their binding and digests, source
authority and redactions. Validation shows active plans, latest run and result
references, and selected Work Item verification. Validation execution remains
available in the [CLI](../validation.md).

## Command and authority

Command is a derived read model. Project State owns exact project observations,
Validation owns independent evidence, and Workflow owns execution gates.
Command owns no tables or grants of authority. Canonical references point to
approved revisions. Operational facts come from project-owned Work Items,
sessions, imports, lifecycle records and Validation runs. Focus, attention and
progress are derived. A successful provider session carries zero Validation
authority.

The Application Layer reads the existing Project Registry in recent-project
order and observes each root independently through a bounded SQLite read
snapshot with a 250 ms SQLite observation budget per root. Missing, invalid and
busy projects retain their own card states.
Command uses up to ten cards per page within the response budget, three focus
Work Items and eight attention items per project. It prioritizes a Work Item
with a live session, followed by active, blocked, and open Work Items with
stable tie breaks. Work Item progress is completed items over all stored items,
including cancelled items in the denominator. The fraction has work-tracking
scope. Project Manager planning completion is a separate measure.

Each card shows at most one active and one recent terminal session. The
automatic Work Item read caps detailed Validation evaluation at 64 non-report
audit entries for a bound Workflow. Beyond that bound, open the Work Item for
the authoritative detail.

Attention identifies its source and opens the corresponding view. Work,
sessions, validation, imports and pending human decisions contribute to the
list. Derived attention remains transient. Command refreshes every four seconds
while visible and offers a manual refresh. Project views read their detail on
navigation. Passive reads leave provider capabilities, recency, Context,
imports and stored project state untouched. Each mutation uses its existing
owner's precondition and reloads the resulting state.

## Exact reviews and provider changes

Project Manager and Workflow decisions can be inspected by exact session ID.
Frozen reviews show the reviewed digest, candidate and predecessor differences
when the presentation fits the display bound and passes redaction. Sensitive
or large reviews remain available in their foreground terminal workflow.
Explicit deferral requires the exact saved version and digest. Final HUMAN
publication uses the established foreground terminal challenge and receipt.
The desktop displays the relevant command for the selected project. Draft
authoring remains available through the existing guided terminal flow.

Settings displays observed capabilities and the saved executable source. It
calls the existing provider and bootstrap operations when the user requests a
change. Provider precedence remains operation, project, user, then discovery.
The desktop transmits no credentials and starts no probes on Settings mount.

Provider switching always requires the target to be chosen. The UI links the
source Session, its Handoff, the target Projection and the new Session. The
two-real-provider acceptance gate remains pending.

## RPC and launcher

The added passive methods are `command.overview`, `command.project`,
`settings.providers`, `review.list` and `review.get`. The added mutations
`providers.refresh`, `provider.configure`, `provider.reset` and
`review.defer` require request IDs. RPC v1 and existing v2 methods keep their
wire shapes. SQLite remains v9.

Build the source desktop on Windows using the
[contributor desktop commands](phase-1-desktop.md#windows-contributor-build). Run the
resulting `corinthos-desktop.exe` directly or use `npm run tauri -- dev`
from `apps/desktop` with a contributor Python configured as documented there.
The Windows `corinthos.exe` launcher opens an ordinary adjacent
`corinthos-desktop.exe` on a no-argument invocation. When the executable is
absent, the launcher opens terminal Home. Arguments continue to select CLI
paths. The source installer supplies the pinned CPython 3.14.10 runtime and
CLI. Phase 9 owns installing the desktop executable and its final packaging.
