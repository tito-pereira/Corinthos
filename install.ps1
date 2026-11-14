# SPDX-License-Identifier: MPL-2.0
#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'platform\windows\common.ps1')
. (Join-Path $PSScriptRoot 'platform\windows\distribution.ps1')
. (Join-Path $PSScriptRoot 'platform\windows\toolchain.ps1')
. (Join-Path $PSScriptRoot 'platform\windows\release.ps1')

$script:InstallPercent = 0
$mutex = $key = $work = $null
Show-InstallProgress 0 'Checking Windows and current user setup'
try {
    $paths = Get-CorinthosPaths
    $compiler = Get-LauncherCompiler
    $source = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
    Assert-PlainPath $source
    foreach ($managed in @($paths.Install, $paths.Previous, $paths.State)) {
        if ($source -eq $managed -or $source.StartsWith("$managed\", [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Run setup from a source checkout outside the Corinthos installation and user state.'
        }
    }
    $mutex = Enter-CorinthosSetup
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    if ($null -eq $key) { throw 'The current user Environment registry key is unavailable.' }
    [void] (Get-UserPath $key)
    Restore-PreviousInstall $paths $key
    if ([IO.Directory]::Exists($paths.Install)) {
        Assert-InstalledInventory $paths.Install (Read-InstallManifest $paths.Install)
    }
    Clear-AbandonedInstallStages $paths
    [void] [IO.Directory]::CreateDirectory($paths.Cache)
    [void] [IO.Directory]::CreateDirectory($paths.Programs)
    $work = Join-Path $paths.Programs ('.Corinthos-stage-' + [guid]::NewGuid().ToString('N'))
    [void] [IO.Directory]::CreateDirectory($work)
    [IO.File]::WriteAllText((Join-Path $work 'corinthos-stage.marker'), 'corinthos-core-stage-v1')
    Show-InstallProgress 10 'Prepared per-user installation and cache'

    $tls = [Net.ServicePointManager]::SecurityProtocol
    try {
        if ($PSVersionTable.PSEdition -eq 'Desktop') {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        }
        $candidate = New-InstallCandidate $source $paths $work $compiler
    } finally { [Net.ServicePointManager]::SecurityProtocol = $tls }
    $version = Test-InstallCandidate $candidate
    Test-DesktopCandidate $candidate
    Write-InstallManifest $candidate 'staged' $version $work
    Show-InstallProgress 82 'Verified staged application'

    $movedCandidate = $false
    $changedPath = $false
    try {
        if ([IO.Directory]::Exists($paths.Install)) {
            Assert-InstalledInventory $paths.Install (Read-InstallManifest $paths.Install)
            [IO.Directory]::Move($paths.Install, $paths.Previous)
        }
        [IO.Directory]::Move($candidate, $paths.Install)
        $movedCandidate = $true
        $installedVersion = Test-InstallCandidate $paths.Install
        if ($installedVersion -cne $version) { throw 'The installed application version changed.' }
        Test-DesktopCandidate $paths.Install
        # Provider probes require a separate user action.
        Invoke-PrivatePython $paths.Install @('-m', 'corinthos.user_state')
        Show-InstallProgress 90 'Initialized Corinthos user state'
        $launcher = Join-Path $paths.Bin 'corinthos.exe'
        $reported = & $launcher --version
        if ($LASTEXITCODE -ne 0 -or $reported -cne "corinthos $version") {
            throw 'The installed corinthos command did not report its expected version.'
        }
        Show-InstallProgress 95 'Verified the installed command'
        $originalPath = Get-UserPath $key
        Write-SetupPathJournal $paths $originalPath
        $changedPath = $true
        Set-OwnedUserPath $key $originalPath $paths.Bin
        Assert-InstalledInventory $paths.Install (Read-InstallManifest $paths.Install)
        Write-InstallManifest $paths.Install 'installed' $version $work
    } catch {
        $installationError = $_
        try {
            if ($changedPath) { [void] (Restore-SetupPathJournal $paths $key) }
            $retired = if ($movedCandidate) { Move-ToRetiredSetup $paths.Install $paths.Programs }
            if ([IO.Directory]::Exists($paths.Previous)) { [IO.Directory]::Move($paths.Previous, $paths.Install) }
            if ($retired) { Clear-RetiredSetup $retired $paths.Programs }
        } catch {
            throw "Installation stopped: $installationError Recovery could not finish: $_ Close Corinthos terminals and rerun setup."
        }
        throw $installationError
    }
    Send-UserPathChange
    try {
        Clear-SetupPathJournal $paths
        if ([IO.Directory]::Exists($paths.Previous)) {
            $retired = Move-ToRetiredSetup $paths.Previous $paths.Programs
            Clear-RetiredSetup $retired $paths.Programs
        }
    }
    catch { Write-Warning 'The verified installation is ready. Close older Corinthos sessions and rerun setup to finish cleanup.' }
    Show-InstallProgress 100 'Corinthos installed successfully'
    Write-Host 'Open a new terminal and run corinthos.'
} catch {
    throw "Corinthos installation stopped at $script:InstallPercent%. $($_.Exception.Message)"
} finally {
    if ($work -and [IO.Directory]::Exists($work)) {
        try { Remove-SetupTree $work $paths.Programs }
        catch { Write-Warning "Could not clean staging directory $work." }
    }
    if ($key) { $key.Dispose() }
    if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
    Write-Progress -Activity 'Installing Corinthos' -Completed
}
