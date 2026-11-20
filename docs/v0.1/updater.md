# Explicit signed product updates

The Windows x64 per-user installation has one updater shared by Settings and
the CLI. It accepts newer stable `vMAJOR.MINOR.PATCH` releases from
[`tito-pereira/Corinthos`](https://github.com/tito-pereira/Corinthos/releases),
an Ed25519-signed manifest verified against the application's pinned public
key, and the same supported project storage schema (currently **9**).
The installation marker format is separately versioned as schema **1**.

Opening the application or Settings reads saved status only. It does not check
the network, download a release, install anything, invoke providers or open a
project database. Source checkouts and ordinary Python environments report
that the shared Windows installation is required.

## Settings and CLI

In **Settings > Corinthos updates**, choose **Check for updates**. If a verified
compatible release exists, choose **Update Corinthos** to download and stage
its complete payload. Close other Corinthos clients and finish active work,
then choose **Restart to finish**. The initiating desktop exits; the
external helper verifies the stage again, replaces and checks the installation,
and reopens the desktop. The relaunched Settings page reads the same operation.

The scriptable equivalent is:

```powershell
corinthos update check
corinthos update status
corinthos update start <operation-id>
corinthos update status <operation-id>
corinthos update finish <operation-id>
```

Check and start return promptly with an operation ID. Poll status until the
phase is `AVAILABLE` before start and `READY` before finish. Bare
`corinthos update` reads status. CLI finish exits naturally and updates the same
installation; it does not open a desktop. Both entry points report installed
and candidate versions, progress, outcomes and actionable errors. A busy retry
uses the same ID. A stale checked operation cannot start alongside a newer one.

## Replacement and recovery

The verified candidate, copied incumbent verifier and durable status live under
`%LOCALAPPDATA%\Corinthos\updates\<operation-id>`, outside the installation.
The helper runs from that copied incumbent runtime. Setup locking serializes
installation changes; a shared runtime admission file prevents active clients
or new mutating clients from entering replacement. Other clients produce a
busy result; the helper never kills unrelated processes. Its health-only
sidecar accepts only handshake, version and shutdown, even while ordinary
clients are excluded.

The complete signed inventory, hashes, lengths and plain paths are checked
before replacement. ZIP links, unsafe paths, alias collisions and unlisted
files are rejected. The previous executable/runtime set remains recoverable
until private Python, CLI, desktop/backend version and startup checks pass.
GUI-initiated replacement also retains it until the restarted native window
opens. Startup failure restores the prior set when safe; uncertain recovery
retains it and reports `RECOVERY_REQUIRED` rather than discarding it.

Project directories, `.corinthos`, approvals, history, preferences, recent
projects, provider configuration and the existing user PATH are outside the
replacement payload. This initial updater performs no project-schema migration.

If another client or locked file blocks replacement, close that client and
retry **finish** for the same ID. If the helper was interrupted, read status
and explicitly finish the operation to recover. When the active installation
is absent or cannot start, use its retained external host:

```powershell
& "$env:LOCALAPPDATA\Corinthos\updates\<operation-id>\host\runtime\python.exe" `
  -I -B -X utf8 -m corinthos update finish <operation-id>
```

If recovery remains uncertain, preserve the operation directory and previous
installation, close all clients and rerun the reviewed source installer.
Do not manually delete the previous runtime while recovery is pending.

## Release preparation and trust

Release publication and deployment require separate owner authorization.
Implementing this updater does not create a GitHub Release or claim that a live
signed candidate is already available. A missing official release reports no
update; unavailable networking and invalid signatures produce explicit errors.

The pinned public key is `src/corinthos/resources/update-public-key.json`.
The dedicated private release key must remain outside the checkout, build,
profile, context, task and release-output directories, in a restricted
user-owned directory. Never copy it into the installed payload or print it.
Back it up securely offline. To generate a new key only when deliberately
establishing a new trust input, first create an empty dedicated private
directory and run:

```powershell
python -m corinthos.update_release generate-key `
  --private-key <restricted-directory>\release-ed25519.key `
  --public-key src\corinthos\resources\update-public-key.json
```

Generation refuses existing key/public outputs or a nonempty key directory,
checks its owner and replaces directory and key-file DACLs with protected
owner-only access. Key
rotation requires a separately reviewed application public-key change;
release metadata cannot choose a new trusted key.

Prepare and independently health-check a complete staged installation with
coherent Python, desktop and release versions. Then create its signed bundle:

```powershell
python -m corinthos.update_release bundle `
  --candidate <verified-staged-installation> `
  --private-key <restricted-directory>\release-ed25519.key `
  --output <new-release-output-directory>
```

The tool creates `corinthos-update.json`, a raw 64-byte
`corinthos-update.sig`, and `Corinthos-<version>-windows-x64.zip`. It binds
the exact manifest bytes to product, stable version, Windows x64, project schema,
archive length/hash and every payload file's path, length and SHA-256. Upload
these three assets only through a separately authorized release-publication
workflow. Existing release checksums and Authenticode signing status remain
separate from this detached-manifest authentication. The verifier is packaged
with the pinned private-runtime dependency closure and retains shipped notices.

## Verification limits

Development uses isolated current-source signed candidates and test keys, with
coherent future-version fixture binaries. These fixtures prove local updater
behavior; they are not public releases or a production version bump. A live
official signed update requires a separately published newer release. Native
window observation, backend startup health and human product acceptance remain
distinct forms of evidence.
