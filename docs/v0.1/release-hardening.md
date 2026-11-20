# Corinthos v0.1 source release

## Build and install on Windows

Clone a reviewed commit with normal Git on x64 Windows, then run
`.\install.ps1` from 64-bit PowerShell 5.1 or later. The
checkout's tracked files must match its commit. The installer builds in a
separate stage, verifies its payload, then promotes the stage under
`%LOCALAPPDATA%\Programs\Corinthos`. A new terminal can run `corinthos` for
Home or `corinthos <subcommand>` for the CLI. Providers are optional.

The installer requires working Git, network access on a cache miss, the
Microsoft Visual C++ x64 toolset and Windows SDK, and Edge WebView2. It
locates the system prerequisites, verifies Microsoft Authenticode before
running their official bootstrappers when necessary, and checks the result.
Where elevation is needed it applies to the Microsoft build prerequisite.
Corinthos itself installs into the current account. UAC refusal leaves the
previous Corinthos installation available.

Node 24.22.2, npm 11.19.1 and Rust/Cargo 1.99.1 are build tools. Their verified
archives are cached under `%LOCALAPPDATA%\Corinthos\cache\install`. The
frontend runs `npm ci` with lifecycle scripts disabled, typecheck, tests and
the production build. Cargo uses the checked-in `Cargo.lock`, `--locked`,
the x64 Windows MSVC target and the release custom protocol. Staged dependencies
and target files are removed with the stage. The final runtime contains the
private CPython 3.14.10, installed Python files, launchers and embedded
frontend. Node, Rust and `node_modules` are outside the installed application.

Rerun the same installer to upgrade or reinstall. Close installed Corinthos
windows first. Setup recovers an interrupted swap using the staged/installed
marker, saved previous installation and a journal of the owned PATH change.
It verifies both GUI and CLI before changing PATH. Uninstall removes the
inventory-owned application and its PATH entry. Project `.corinthos` state,
user JSON, verified download cache and shared Microsoft prerequisites are
preserved. Ubuntu is supported by the backend verification matrix. Desktop
installation targets Windows.

## Provenance and release disposition

The product, Python package, frontend, Cargo desktop and Tauri application
versions are 0.1.0. The stable desktop identifier is `org.corinthos.desktop`.
RPC stays v2 and the SQLite database stays v9. Existing schemas keep their
independent versions. The private Windows runtime stays on CPython 3.14.10.

`installation.json` enumerates owned entries. `release-manifest.json`
binds the source commit and tree, build versions and explicit hashes.
`SHA256SUMS` verifies the desktop, launcher, release record and archival
resources. The inventory includes the pinned Python wheels and npm/Cargo
lockfile dependencies. A lockfile inventory includes build and transitive
items as well as dependencies that ship. Python wheel license files and
CPython's license stay with their corresponding payloads.

The source installer owns deployment and recovery. Tauri bundle generation
is disabled. CI artifacts are candidate evidence. A GitHub Release and
version tag require explicit publication. This source path produces unsigned
binaries. Windows SmartScreen or Smart App Control can restrict them. An
owner-approved signing service can sign a separately controlled distribution
candidate using SHA-256 and an approved RFC 3161 timestamp server, then verify
it with Windows SignTool `verify /pa` and regenerate the signed artifact
checksums and provenance. Do not pass signing secrets to application builds.
Public distribution remains pending an owner signing disposition.

The explicit updater uses official stable releases and a pinned Ed25519-signed
manifest for same-project-schema Windows updates. It is shared by Settings and
`corinthos update`; startup does not install updates. See the
[updater and recovery guide](updater.md) for signing, complete-payload checks,
runtime admission and rollback. Publishing a signed release remains a separate
authorized action; a live newer signed release is not implied. The source
installer remains available. Real cross-provider verification is
`NOT_AVAILABLE` in the current owner environment and is independent of release readiness.

## Trust boundaries

| Boundary | Mitigation | Residual risk |
| --- | --- | --- |
| WebView | Bundled local frontend, restrictive CSP and main-window IPC capability set. | Locally compromised application files or an unpatched operating system threaten the same user account. |
| Rust/Python RPC | Typed workspace allowlist, bounded frames and fixed sidecar lifecycle. Installed production locates its own private interpreter. | Local authorized operations still rely on project input and the current user boundary. |
| Providers | Explicit selection and execution. Provider-owned authentication stays external. Classified secrets are excluded or redacted from provider observations and derived Context. Ordinary desktop errors are bounded. | A native provider is a subprocess with the capabilities of its own installation and execution mode. Secret detection is pattern-based. |
| Project state | Separate private project state, local path checks and exact references. Command reads do not publish or migrate. | A local writer with access to the same files can modify working-tree material between observations. |
| Installer | Owned paths and inventory, HTTPS and pinned hash/size for immutable downloads, Microsoft publisher validation for changing prerequisite installers. | Microsoft system prerequisites and the selected dependency publishers remain part of the supply chain. |
| Process ownership | Existing Windows Job Object and POSIX process guardian with independent cleanup status and recovery. | Indeterminate cleanup remains visible and blocks unsafe continuation. |

Corinthos owns no persistent credentials in v0.1 and no general persistent
application logging subsystem. It does retain sanitized, bounded Session
events and project-local evidence through their respective owners. The
installer may use `LOCALAPPDATA`, known Windows system paths, Git from PATH
and temporary build environment variables, with the contributor sidecar
override limited to contributor builds. No Corinthos telemetry or automatic
update traffic is configured. Installer requests reach Python.org,
files.pythonhosted.org, Node.js, the npm registry, Rust distribution,
crates.io and official Microsoft prerequisite endpoints. Optional chosen
native providers handle their own network requests. Explicit update checks and
downloads use the fixed official GitHub release channel; ordinary startup and
saved-status reads do not make those requests.
