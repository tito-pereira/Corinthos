# v0.1 Phase 1 desktop integration spike

The Phase 0 baseline is the canonical merge
[`6e5b57401e17baf438b155488e8eea12b6c4f64e`](https://github.com/tito-pereira/Corinthos/commit/6e5b57401e17baf438b155488e8eea12b6c4f64e),
tree `6e87ebbbe55c0e62f231e4152974500815e098ea`.
Its [post-merge CI](https://github.com/tito-pereira/Corinthos/actions/runs/43406847574)
passed all five jobs. See the [Phase 0 compatibility records](baseline.md).

## Shared read boundary

`corinthos status --root PATH` and the desktop sidecar call the same
[application operations](../../src/corinthos/application/__init__.py).
The CLI emits JSON with version, project status and known provider rows.
Project status inspects the supplied directory, bootstrap and existing store.
Storage is absent, current, legacy, invalid or unverified. Passive reads do
not create a workspace, migrate SQLite, update recent projects or rewrite
provider settings. Paths are limited to 4,096 characters. Windows UNC,
device and drive-relative paths are rejected. SQLite inspection uses an
immutable connection and leaves journaled, changing or time-limited state
unverified. The SQLite VM inspection budget is two seconds.
The provider view reports explicit choices and compatible
historical observations. An absent observation remains unobserved. A stale
or nonmatching observation carries no current availability or command authority.
The CLI returns a null provider section if it cannot read provider settings.

The packaged React view shows a compact connection state and explicit project
operations. Settings About / Diagnostics exposes application and protocol
versions, the running Python implementation/version, supported storage schema
and native desktop package identity. Selected-project schema and storage come
from its passive summary. Bootstrap-only projects have no database schema.
Unavailable values and previously observed values are labeled in the support
view. Rust owns process lifecycle and transports these operations.
The WebView receives no shell capability and ships its frontend locally.
No-argument `corinthos` continues to open the CLI Home.

## Native directory selection

Create, Open and repository Import share a directory-selection control. Its
`select_project_directory` command is direct Tauri IPC, independent of the Python
request queue. The main-window capability grants only this additional command;
the WebView receives no filesystem, shell or arbitrary-dialog options.

Windows uses `IFileOpenDialog` through the existing locked `windows` crate version
as an optional direct desktop dependency. A fresh STA thread owns the COM
apartment and main-window-owned dialog. Fixed options select one existing local
directory without changing the working directory. Cancellation returns `null`;
other failures return bounded errors. Paths use strict UTF-16 conversion, reject
unsupported native path forms and redirected ancestors, and are still validated
by Python when an explicit project operation runs. The backend mutex is never
held while the dialog is open. Other platforms report unavailable and retain
manual entry.

The OS dialog may offer New folder. A folder deliberately created there belongs
to the user and can remain after cancellation. Cancelling or selecting a folder
performs no automatic initialization, registration, discovery, import or provider
invocation. The shared control preserves drafts on cancellation/failure, prevents
duplicate selection and submission, and ignores stale completions. Changing an
Import path invalidates its prior review.

The implementation distinguishes the documented
[`IModalWindow::Show` cancellation result](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nf-shobjidl_core-imodalwindow-show)
from other errors, uses the fixed
[folder-dialog options](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/ne-shobjidl_core-_fileopendialogoptions),
and balances [COM apartment initialization](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-coinitializeex)
with interface and allocated-string cleanup.

## RPC v1 over stdio

Run the backend entry point as
`python -I -B -X utf8 -m corinthos.application.sidecar` with an interpreter
that already has Corinthos installed. Its stdout is reserved for protocol
messages. Transport is UTF-8, with one JSON-RPC 2.0 object per line and a
65,536-byte frame limit excluding the newline. JSON escapes embedded newlines.
Responses echo the request ID. IDs are bounded strings or safe integers.
Protocol version 1 is independent of package, SQLite and contract versions.

| Method | Params | Result |
| --- | --- | --- |
| `system.handshake` | `{"protocol_version":1}` | Protocol version, package version and supported methods. |
| `system.version` | Empty | Package version. |
| `project.status` | `{"root":"PATH"}` | Directory, initialization and storage status. |
| `providers.status` | Empty or `{"root":"PATH"}` | Configured choices and cached capabilities, optionally using project selection. |
| `system.shutdown` | Empty | `{"stopping":true}` and orderly exit. Use a request ID. |

The sidecar emits `system.ready` on startup and `system.stopping` on orderly
shutdown. Negotiate version 1 before requesting application methods.
Malformed JSON returns `-32700`. Invalid requests or oversized frames
return `-32600`, unknown methods `-32601`, invalid parameters `-32602`
and sanitized internal failures `-32603`. Negotiation errors use
`-32001` and unavailable state or an oversized result uses `-32002`.
The [versioned schemas](../../schemas/rpc/v1) describe messages and results.
An oversized input frame is discarded within a bounded recovery window.
The transport uses no HTTP service or local listener. Vite's development
server serves only the optional contributor frontend.

## Windows contributor build

Install the repository package into a contributor Python environment and set
`CORINTHOS_SIDECAR_PYTHON` to its absolute interpreter path for `tauri dev`.
The release desktop uses the private Python in `..\runtime\python.exe`
relative to its binary. The Windows source installer creates that layout.
The desktop invokes a fixed Python module. WebView requests never choose
the executable.

The desktop spike uses Tauri 2, React 19, TypeScript 7, Vite 8 and npm.
The lockfiles fix frontend and Rust dependencies. Use Node 24.22.2,
npm 11.19.1 and Rust 1.99.1. The Windows desktop build uses the MSVC toolchain,
Windows SDK and WebView2. The backend remains usable without Node or Rust.

```powershell
cd apps/desktop
npm ci
npm run typecheck
npm test
npm run tauri -- dev
```

The Rust bridge starts one owned child, performs a bounded handshake and
serializes requests. It checks response IDs and forwards lifecycle events
separately. It uses an eight-second handshake, five-second request deadline
and one-second shutdown grace period. A transport failure disconnects the panel and allows a clean
restart. Normal close requests shutdown. The bridge waits for a bounded
interval, then terminates and reaps its child if needed. EOF also ends the
sidecar.

## Current desktop diagnostics

RPC v2 advertises `system.diagnostics` with empty parameters. Its bounded result
contains `python_implementation`, `python_version` and
`supported_schema_version`. The operation reads the running interpreter and the
storage service's static supported version. It opens no project or registry and
performs no provider work.

Native snapshots include the desktop package version and configured identifier,
including during startup failure. A typed failure origin distinguishes startup
unavailability from an established transport disconnection. The UI derives
Connecting and Restarting from pending lifecycle requests and gates backend
actions while offline. Selected project/navigation state survives disconnection
and is refreshed passively when the connection returns.

Windows CI builds the desktop and exercises the Rust/Python bridge against the
checksum-verified private runtime, with repeated start/stop and provider
sentinels. A separate build with the `desktop-smoke` feature observes real
DOM values from the local WebView and exercises current-version project and
diagnostics flows. The regular desktop build excludes this test observer.
Installer checks use current installation/reinstallation, recovery and uninstall
fixtures. Ubuntu verifies the Python
protocol through the package tests. Windows is the desktop build and WebView
gate.

The original Phase 1 delivery deferred public packaging, Work Items, full Project
State, Agent Sessions, Context Projection, import, provider switching and Command
to later phases. Their implemented behavior is documented in the subsequent
phase guides.
