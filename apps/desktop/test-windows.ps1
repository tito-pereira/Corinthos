# SPDX-License-Identifier: MPL-2.0
#Requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)] [string] $Desktop)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
. (Join-Path $repo 'platform\windows\common.ps1')
. (Join-Path $repo 'platform\windows\distribution.ps1')
. (Join-Path $repo 'platform\windows\test-installed-desktop.ps1')

function Assert-Smoke([bool] $Condition, [string] $Message) {
    if (!$Condition) { throw $Message }
}

$parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$root = Join-Path $parent ('corinthos-desktop-' + [Guid]::NewGuid().ToString('N'))
$originalLocal = $env:LOCALAPPDATA
try {
    $desktopPath = (Resolve-Path -LiteralPath $Desktop).Path
    $env:LOCALAPPDATA = Join-Path $root 'local'
    $paths = Get-CorinthosPaths
    $work = Join-Path $root 'staging'
    foreach ($directory in @($work, $paths.Cache, $paths.Programs)) {
        [void] [IO.Directory]::CreateDirectory($directory)
    }
    $script:InstallPercent = 0
    $candidate = New-PrivateRuntimeCandidate $repo $paths $work (Get-LauncherCompiler)
    $version = Test-InstallCandidate $candidate
    [IO.File]::Copy($desktopPath, (Join-Path $candidate 'bin\corinthos-desktop.exe'))
    Write-InstallManifest $candidate 'staged' $version $work
    [IO.Directory]::Move($candidate, $paths.Install)
    Write-InstallManifest $paths.Install 'installed' $version $work
    $python = Join-Path $paths.Install 'runtime\python.exe'
    Invoke-PrivatePython $paths.Install @('-m', 'corinthos.user_state')
    $project = Join-Path $root 'project'
    $null = Invoke-PrivatePython $paths.Install @('-m', 'corinthos', 'project', 'init', '--root', $project)
    $null = Invoke-InstalledDesktopSmoke (Join-Path $paths.Install 'bin\corinthos-desktop.exe') $python $project
    Write-Host 'Private-runtime packaging and shipping native transport/health passed. GUI journey evidence is recorded separately.'
} finally {
    $env:LOCALAPPDATA = $originalLocal
    if ([IO.Directory]::Exists($root)) {
        Assert-PlainPath $root
        Assert-Smoke ([IO.Path]::GetDirectoryName($root) -ceq $parent -and
            [IO.Path]::GetFileName($root) -cmatch '^corinthos-desktop-[a-f0-9]{32}$') 'Refusing cleanup outside the exact desktop fixture.'
        [void] (Get-SetupInventory $root)
        Remove-Item -LiteralPath $root -Recurse -Force
    }
}
