# Corinthos v0.1.0 release candidate notes

Corinthos is a local programming-agent workspace for explicit engineering
decisions, versioned contracts and independent verification. Its Standard
keeps the five mandatory engineering Commandments in force.

- **Command and desktop** provide passive multi-project views, an operational
  Work Item tree, Agent Sessions, Context and independent Validation.
- **Sessions and continuity** let the operator explicitly select a native
  provider. A switch creates a separate Session bound to an exact Corinthos
  Handoff and Context Projection. Synthetic providers verify this mechanism.
- **Work and Context** support adaptive flows for TASK, CHANGE, PROJECT and
  CRITICAL scope. Context captures deterministic, bounded, redacted evidence.
  Existing Project Manager and Workflow publication preserve the foreground
  HUMAN terminal decision.
- **External Git import** supports ordinary local SHA-1 and SHA-256 worktrees.
  Inspect is read-only. The operator confirms the exact digest and applicable
  ambiguities before the importer writes private state.
- **Windows source installation** builds the desktop from the exact checkout
  with pinned Node, npm and Rust. It stages a private CPython 3.14.10 backend
  and tests the GUI and CLI before publishing the installation.

The first-class desktop platform is x64 Windows with Microsoft WebView2.
Ubuntu is verified for the Python backend. The CLI remains available with
`corinthos <subcommand>`. Native provider accounts and authentication
remain under their respective providers' management.

This candidate has unsigned Windows binaries unless a separately evidenced,
owner-signed distribution record says otherwise. Windows reputation and
application-control policies may restrict unsigned software. Updates use the
same source installer explicitly. A second real provider is unavailable, so
the real two-provider field run is recorded as `NOT_AVAILABLE`.
