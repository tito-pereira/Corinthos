# SPDX-License-Identifier: MPL-2.0
#Requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string] $OperationId)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
. (Join-Path $PSScriptRoot 'distribution.ps1')
. (Join-Path $PSScriptRoot 'release.ps1')

function Invoke-UpdateHost([string] $Action, [string[]] $Values = @()) {
    $python = Join-Path $script:UpdateRoot 'host\runtime\python.exe'
    Assert-PlainPath $python
    $result = & $python -I -B -X utf8 -m corinthos.updater $Action $OperationId @Values
    if ($LASTEXITCODE -ne 0) { throw 'The external update verifier could not complete its check.' }
    return $result
}

function Set-UpdatePhase([string] $Phase, [string] $Code = '') {
    $values = @($Phase)
    if ($Code) { $values += $Code }
    $null = Invoke-UpdateHost '_phase' $values
}

function Get-UpdateClients([string] $Install) {
    $filter = "Name='corinthos.exe' OR Name='corinthos-desktop.exe' OR Name='python.exe' OR Name='pythonw.exe'"
    foreach ($process in Get-CimInstance Win32_Process -Filter $filter -ErrorAction Stop) {
        if ($process.ExecutablePath -and $process.ExecutablePath.StartsWith("$Install\", [StringComparison]::OrdinalIgnoreCase)) {
            $process.ProcessId
        }
    }
}

function Enter-UpdateRuntime($Paths) {
    $path = Join-Path $Paths.Programs '.Corinthos-runtime.lock'
    Assert-PlainPath $path
    if ([IO.File]::Exists($path) -and [IO.FileInfo]::new($path).Length -ne 0) {
        throw 'The runtime admission file is not an empty Corinthos lock.'
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $gate = $null
        $accepted = $false
        try {
            $gate = [IO.FileStream]::new($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $accepted = !@(Get-UpdateClients $Paths.Install).Count
            if ($accepted) { return $gate }
        } catch [IO.IOException] {
            # Normal clients retain a shared admission handle until they exit.
        } finally {
            if ($gate -and !$accepted) { $gate.Dispose() }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Test-UpdateHealth([string] $Install, [string] $Version) {
    if ((Test-InstallCandidate $Install) -cne $Version) { throw 'The candidate private runtime version differs from its signed release.' }
    Test-DesktopCandidate $Install
    $command = Join-Path $Install 'bin\corinthos.exe'
    Assert-PlainPath $command
    $reported = & $command --version
    if ($LASTEXITCODE -ne 0 -or $reported -cne "corinthos $Version") {
        throw 'The candidate command did not report its signed version.'
    }
}

function Wait-UpdateDesktop([Diagnostics.Process] $Process) {
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $Process.Refresh()
        if ($Process.HasExited) { throw 'The updated desktop exited before its window started.' }
        if ($Process.MainWindowHandle) { return }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'The updated desktop did not open its native window.'
}

function Restore-UpdateRuntime($Paths, $Plan) {
    $previous = Read-InstallManifest $Paths.Previous
    if ($previous.state -cne 'installed' -or $previous.version -cne $Plan.installed_version) {
        throw 'The previous runtime does not match this update operation.'
    }
    Assert-InstalledInventory $Paths.Previous $previous
    $retired = $null
    if ([IO.Directory]::Exists($Paths.Install)) {
        $active = Read-InstallManifest $Paths.Install
        if ($active.version -cne $Plan.candidate_version) {
            throw 'The active runtime does not match the interrupted candidate.'
        }
        $retired = Move-ToRetiredSetup $Paths.Install $Paths.Programs
    }
    [IO.Directory]::Move($Paths.Previous, $Paths.Install)
    Test-UpdateHealth $Paths.Install $Plan.installed_version
    if ($retired) { Clear-RetiredSetup $retired $Paths.Programs }
}

function Invoke-CorinthosUpdate {
    $paths = Get-CorinthosPaths
    $script:UpdateRoot = Join-Path $paths.State "updates\$OperationId"
    Assert-PlainPath $script:UpdateRoot
    if ($OperationId.Length -ne 32 -or
        ![IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\').Equals((Join-Path $script:UpdateRoot 'helper'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Run only the helper belonging to this durable update operation.'
    }
    $plan = Invoke-UpdateHost '_plan' | ConvertFrom-Json
    if ($plan.operation_id -cne $OperationId -or $plan.restart_gui -isnot [bool] -or
        $plan.installed_version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+$' -or
        $plan.candidate_version -cnotmatch '^[0-9]+\.[0-9]+\.[0-9]+$') {
        throw 'The durable update identity is invalid.'
    }
    $mutex = $gate = $null
    $movedOld = $movedCandidate = $false
    $promotionStarted = $false
    $recovering = $false
    $startedDesktop = $null
    try {
        try { $mutex = Enter-CorinthosSetup }
        catch { Set-UpdatePhase 'BUSY' 'BUSY'; return }
        $gate = Enter-UpdateRuntime $paths
        if (!$gate) { Set-UpdatePhase 'BUSY' 'BUSY'; return }

        if ([IO.Directory]::Exists($paths.Previous)) {
            # A lost terminal record never authorizes discarding the recoverable incumbent.
            $recovering = $true
            Restore-UpdateRuntime $paths $plan
            Set-UpdatePhase 'ROLLED_BACK' 'INTERRUPTED'
            if ($plan.restart_gui) {
                $gate.Dispose(); $gate = $null
                $startedDesktop = Start-Process -FilePath (Join-Path $paths.Bin 'corinthos-desktop.exe') -WorkingDirectory $paths.Bin -WindowStyle Hidden -PassThru
                Wait-UpdateDesktop $startedDesktop
            }
            return
        }
        $installed = Read-InstallManifest $paths.Install
        Assert-InstalledInventory $paths.Install $installed
        if ($installed.state -cne 'installed' -or $installed.version -cne $plan.installed_version) {
            throw 'The installation changed after this update was checked.'
        }
        $verified = Invoke-UpdateHost '_verify' | ConvertFrom-Json
        if ($verified.operation_id -cne $OperationId -or
            $verified.installed_version -cne $plan.installed_version -or
            $verified.candidate_version -cne $plan.candidate_version -or
            $verified.restart_gui -cne $plan.restart_gui) {
            throw 'The verified candidate no longer matches this operation.'
        }
        $candidate = Join-Path $script:UpdateRoot 'candidate'
        $marker = Read-InstallManifest $candidate
        Assert-InstalledInventory $candidate $marker
        if ($marker.state -cne 'staged' -or $marker.version -cne $plan.candidate_version) {
            throw 'The signed candidate has an invalid staged installation marker.'
        }
        Test-UpdateHealth $candidate $plan.candidate_version
        Set-UpdatePhase 'INSTALLING'
        $promotionStarted = $true
        [IO.Directory]::Move($paths.Install, $paths.Previous)
        $movedOld = $true
        [IO.Directory]::Move($candidate, $paths.Install)
        $movedCandidate = $true
        Test-UpdateHealth $paths.Install $plan.candidate_version
        Write-InstallManifest $paths.Install 'installed' $plan.candidate_version $script:UpdateRoot
        if ($plan.restart_gui) {
            $gate.Dispose(); $gate = $null
            $startedDesktop = Start-Process -FilePath (Join-Path $paths.Bin 'corinthos-desktop.exe') -WorkingDirectory $paths.Bin -WindowStyle Hidden -PassThru
            Wait-UpdateDesktop $startedDesktop
        }
        Set-UpdatePhase 'SUCCEEDED'
        try {
            $retired = Move-ToRetiredSetup $paths.Previous $paths.Programs
            Clear-RetiredSetup $retired $paths.Programs
        } catch { Write-Warning 'The verified update is ready; the previous owned runtime remains for inspection.' }
    } catch {
        if ($recovering) {
            Set-UpdatePhase 'RECOVERY_REQUIRED' 'RECOVERY_REQUIRED'
        } elseif ($movedOld -or $movedCandidate) {
            try {
                if ($startedDesktop -and !$startedDesktop.HasExited) {
                    if (!$startedDesktop.CloseMainWindow() -or !$startedDesktop.WaitForExit(5000)) {
                        throw 'The restarted desktop could not close safely for rollback.'
                    }
                }
                if (!$gate) {
                    $gate = Enter-UpdateRuntime $paths
                    if (!$gate) { throw 'Active clients prevent safe rollback.' }
                }
                Restore-UpdateRuntime $paths $plan
                Set-UpdatePhase 'ROLLED_BACK' 'STARTUP_FAILED'
                if ($plan.restart_gui) {
                    $gate.Dispose(); $gate = $null
                    $startedDesktop = Start-Process -FilePath (Join-Path $paths.Bin 'corinthos-desktop.exe') -WorkingDirectory $paths.Bin -WindowStyle Hidden -PassThru
                    Wait-UpdateDesktop $startedDesktop
                }
            } catch { Set-UpdatePhase 'RECOVERY_REQUIRED' 'RECOVERY_REQUIRED' }
        } else {
            $cause = $_.Exception.GetBaseException()
            if ($promotionStarted -and ($cause -is [IO.IOException] -or $cause -is [UnauthorizedAccessException])) {
                $code = if ($cause -is [UnauthorizedAccessException]) { 'ACCESS_DENIED' } else { 'BUSY' }
                Set-UpdatePhase 'BUSY' $code
            } else {
                $code = if ($cause -is [UnauthorizedAccessException]) { 'ACCESS_DENIED' } else { 'VERIFICATION_FAILED' }
                Set-UpdatePhase 'FAILED' $code
            }
        }
    } finally {
        if ($gate) { $gate.Dispose() }
        if ($mutex) { $mutex.ReleaseMutex(); $mutex.Dispose() }
        if ($startedDesktop) { $startedDesktop.Dispose() }
    }
}

Invoke-CorinthosUpdate
