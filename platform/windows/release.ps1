# SPDX-License-Identifier: MPL-2.0

function Write-ReleaseJson([string] $Path, $Value) {
    [IO.File]::WriteAllText($Path, (($Value | ConvertTo-Json -Depth 12 -Compress) + "`n"))
}

function Get-ReleaseHash([string] $Path) {
    Assert-PlainPath $Path
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-ReleaseFiles {
    @(
        'bin/corinthos-desktop.exe', 'bin/corinthos.exe', 'release-manifest.json',
        'resources/artifacts.json', 'resources/toolchain-artifacts.json',
        'resources/third-party-notices.json', 'resources/RELEASE-NOTES.md', 'resources/LICENSE'
    )
}

function Get-LockedDependencyNames([string] $Candidate, [string] $NpmLock, [string] $CargoLock) {
    foreach ($path in @($NpmLock, $CargoLock)) {
        Assert-PlainPath $path
        if ([IO.FileInfo]::new($path).Length -gt 8MB) { throw 'A desktop lockfile exceeds the release inventory budget.' }
    }
    $readLocks = @'
import json, sys, tomllib
from pathlib import Path

npm_lock, cargo_lock = map(Path, sys.argv[1:])
npm = json.loads(npm_lock.read_text(encoding='utf-8'))['packages']
cargo = tomllib.loads(cargo_lock.read_text(encoding='utf-8'))['package']
packages = [dict(package=name, version=entry['version']) for name, entry in sorted(npm.items()) if name]
crates = [dict(package=entry['name'], version=entry['version']) for entry in cargo]
assert packages and crates
print(json.dumps(dict(npm=packages, cargo=crates)))
'@
    return Invoke-PrivatePython $Candidate @('-c', $readLocks, $NpmLock, $CargoLock) | ConvertFrom-Json
}

function Write-CorinthosReleaseRecord([string] $Source, [string] $Candidate, $Provenance) {
    $desktopSource = Join-Path $Source 'apps\desktop'
    $resources = Join-Path $Candidate 'resources'
    $npmLock = Join-Path $desktopSource 'package-lock.json'
    $cargoLock = Join-Path $desktopSource 'src-tauri\Cargo.lock'
    $provenanceTools = [ordered] @{}
    foreach ($key in @('node', 'npm', 'rustc', 'cargo', 'tauri_cli', 'msvc', 'windows_sdk', 'webview2')) {
        $value = $Provenance[$key]
        if ($value -isnot [string] -or $value -cnotmatch '^[A-Za-z0-9 .+()_-]{1,128}$') {
            throw 'The desktop toolchain provenance contains an invalid value.'
        }
        $provenanceTools[$key] = $value
    }
    $prerequisites = [ordered] @{}
    foreach ($key in @('msvc_signer', 'webview2_signer')) {
        $signer = $Provenance[$key]
        if ($null -ne $signer -and $signer -cne 'Microsoft Corporation') {
            throw 'The desktop prerequisite provenance contains an invalid publisher.'
        }
        $prerequisites[$key] = $signer
    }
    foreach ($key in @('source_commit', 'source_tree')) {
        if ($Provenance[$key] -cnotmatch '^[0-9a-f]{40}([0-9a-f]{24})?$') { throw 'Could not verify release source identity.' }
    }
    $notes = Join-Path $Source 'docs\release-notes.md'
    Assert-PlainPath $notes
    [IO.File]::Copy($notes, (Join-Path $resources 'RELEASE-NOTES.md'))
    $locks = Get-LockedDependencyNames $Candidate $npmLock $cargoLock
    $pins = Get-Content -LiteralPath (Join-Path $resources 'artifacts.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-ReleaseJson (Join-Path $resources 'third-party-notices.json') ([ordered] @{
        schema = 1
        product_license = 'MPL-2.0'
        python_runtime = 'CPython 3.14.10. License text is shipped in runtime/LICENSE.txt.'
        python_wheels = @($pins.wheels | ForEach-Object filename)
        rust_packages = $locks.cargo
        frontend_packages = $locks.npm
        platform_dependencies = @('Microsoft Edge WebView2 Runtime', 'Windows system libraries')
        interpretation = 'Lockfiles contain build, target-specific and transitive dependencies. The inventory does not assert that each dependency ships or shares the product license. Inspect upstream license terms and distributed wheel license files.'
    })
    $payloadBytes = [long] 0
    foreach ($entry in Get-SetupInventory $Candidate -ProtectProjects) {
        if ($entry.StartsWith('file:')) { $payloadBytes += [IO.FileInfo]::new((Join-Path $Candidate $entry.Substring(5))).Length }
    }
    $manifestPath = Join-Path $Candidate 'release-manifest.json'
    Write-ReleaseJson $manifestPath ([ordered] @{
        schema = 1
        product_version = '0.1.0'
        source_commit = $Provenance.source_commit
        source_tree = $Provenance.source_tree
        build_platform = 'windows'
        architecture = 'x86_64-pc-windows-msvc'
        desktop_identifier = 'org.corinthos.desktop'
        python_runtime = '3.14.10'
        python_package_version = '0.1.0'
        rpc_protocol = 2
        workspace_format = 1
        desktop_binary_sha256 = Get-ReleaseHash (Join-Path $Candidate 'bin\corinthos-desktop.exe')
        launcher_sha256 = Get-ReleaseHash (Join-Path $Candidate 'bin\corinthos.exe')
        artifact_manifest_sha256 = Get-ReleaseHash (Join-Path $resources 'artifacts.json')
        toolchain_manifest_sha256 = Get-ReleaseHash (Join-Path $resources 'toolchain-artifacts.json')
        package_lock_sha256 = Get-ReleaseHash $npmLock
        cargo_lock_sha256 = Get-ReleaseHash $cargoLock
        build_tools = $provenanceTools
        prerequisite_bootstrap_signers = $prerequisites
        signing_state = 'UNSIGNED'
        auto_updater = 'EXPLICIT_SIGNED_MANIFEST'
        real_provider_validation = 'NOT_AVAILABLE'
        installed_payload_bytes = $payloadBytes
    })
    $lines = @(Get-ReleaseFiles | ForEach-Object { (Get-ReleaseHash (Join-Path $Candidate $_)) + "  $_" })
    [IO.File]::WriteAllText((Join-Path $Candidate 'SHA256SUMS'), ($lines -join "`n") + "`n")
}

function Test-CorinthosReleaseRecord([string] $Install) {
    $path = Join-Path $Install 'SHA256SUMS'
    Assert-PlainPath $path
    if (![IO.File]::Exists($path) -or [IO.FileInfo]::new($path).Length -gt 8KB) {
        throw 'The candidate release checksums are missing or invalid.'
    }
    $expected = @(Get-ReleaseFiles | ForEach-Object { (Get-ReleaseHash (Join-Path $Install $_)) + "  $_" })
    $actual = [IO.File]::ReadAllText($path)
    if ($actual -cne (($expected -join "`n") + "`n")) { throw 'The candidate release checksums did not verify.' }
}
