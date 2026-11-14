# SPDX-License-Identifier: MPL-2.0
#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
if ($env:GITHUB_ACTIONS -ne 'true' -or !$env:RUNNER_TEMP -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'Run this real HKCU PATH smoke on a disposable GitHub-hosted Windows runner.'
}
. (Join-Path $PSScriptRoot 'common.ps1')
. (Join-Path $PSScriptRoot 'distribution.ps1')

function Assert-Smoke([bool] $Condition, [string] $Message) {
    if (!$Condition) { throw $Message }
}

function Invoke-TestInstall([string] $Script, [string] $ExpectedError = '') {
    $lines = [Collections.Generic.List[string]]::new()
    $failure = ''
    try { & $Script 6>&1 | ForEach-Object { $lines.Add($_.ToString()) } }
    catch { $failure = $_.Exception.Message }
    $percentages = @(
        foreach ($line in $lines) {
            Write-Host $line
            if ($line -match '^\[.*\] ([0-9]{1,3})% ') { [int] $Matches[1] }
        }
    )
    Assert-Smoke ($percentages.Count -gt 0 -and $percentages[0] -eq 0) 'Installer did not start progress at 0%.'
    for ($index = 1; $index -lt $percentages.Count; $index++) {
        Assert-Smoke ($percentages[$index] -ge $percentages[$index - 1]) 'Installer progress decreased.'
    }
    if ($ExpectedError) {
        Assert-Smoke ($failure -like "*$ExpectedError*") "Expected installer error '$ExpectedError', got '$failure'."
        Assert-Smoke (100 -notin $percentages) 'A failed install printed 100%.'
    } else {
        Assert-Smoke (!$failure) "Unexpected installer failure: $failure"
        Assert-Smoke (
            $percentages[-1] -eq 100 -and
            @($percentages | Where-Object { $_ -eq 100 }).Count -eq 1 -and
            95 -in $percentages
        ) 'Completion must follow verification and be printed exactly once.'
    }
    return $lines
}

function Assert-UninstallRefused([string] $Script, [string] $ExpectedError) {
    $failure = ''
    try { & $Script } catch { $failure = $_.Exception.Message }
    Assert-Smoke ($failure -like "*$ExpectedError*") "Unsafe uninstall was not refused: $failure"
}

function New-ProbeSentinel([string] $Directory) {
    [void] [IO.Directory]::CreateDirectory($Directory)
    $sourceFile = Join-Path $Directory 'sentinel.cs'
    $executable = Join-Path $Directory 'codex.exe'
    [IO.File]::WriteAllText($sourceFile, @'
using System;
using System.IO;

internal static class Sentinel
{
    public static int Main(string[] args)
    {
        string log = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "probe.log");
        File.AppendAllText(log, String.Join(" ", args) + Environment.NewLine);
        if (args.Length == 1 && args[0] == "--version")
        {
            Console.WriteLine("codex-cli 9.8.7");
            return 0;
        }
        if (args.Length > 0 && args[args.Length - 1] == "--help")
        {
            Console.WriteLine("--sandbox read-only --json stdin SESSION_ID");
            return 0;
        }
        return 2;
    }
}
'@)
    & (Get-LauncherCompiler) /nologo /target:exe /platform:x64 "/out:$executable" $sourceFile | Out-Host
    Assert-Smoke ($LASTEXITCODE -eq 0 -and [IO.File]::Exists($executable)) 'Could not compile the synthetic provider.'
    return $executable
}

$runnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
$sandbox = Join-Path $runnerTemp ('corinthos-install-' + [guid]::NewGuid().ToString('N'))
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$source = Join-Path $sandbox 'source checkout'
$other = Join-Path $sandbox 'unrelated workspace'
$originalLocal = $env:LOCALAPPDATA
$originalProcessPath = $env:PATH
$originalLocation = Get-Location
$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
$originalUserPath = Get-UserPath $key
$env:LOCALAPPDATA = Join-Path $sandbox 'local'
$systemPath = "$env:SystemRoot\System32"
$env:PATH = $systemPath
$pathFixture = '%SystemRoot%\System32;C:\corinthos-smoke-unrelated'
try {
    $paths = Get-CorinthosPaths
    foreach ($directory in @($source, $other)) {
        [void] [IO.Directory]::CreateDirectory($directory)
    }
    foreach ($name in @('install.ps1', 'uninstall.ps1', 'platform', 'src', 'LICENSE')) {
        Copy-Item -LiteralPath (Join-Path $repo $name) -Destination $source -Recurse
    }
    $key.SetValue('Path', $pathFixture, [Microsoft.Win32.RegistryValueKind]::ExpandString)
    Assert-Smoke (!$(
        Get-Command -Name @('python.exe', 'py.exe', 'pip.exe', 'codex.exe', 'claude.exe') -ErrorAction SilentlyContinue
    )) 'Smoke PATH must exclude system Python and providers.'
    Set-Location -LiteralPath $other
    $null = Invoke-TestInstall (Join-Path $source 'install.ps1')
    $expectedPath = "$($paths.Bin);$pathFixture"
    $registry = Join-Path $paths.State 'providers.json'
    Assert-Smoke (![IO.File]::Exists($registry)) 'A clean installation must leave providers unobserved.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $expectedPath) 'Install did not add only the owned user PATH entry.'
    Assert-Smoke ((Get-UserPath $key).Kind -eq [Microsoft.Win32.RegistryValueKind]::ExpandString) 'Install changed PATH registry type.'
    Assert-Smoke ((Read-InstallManifest $paths.Install).state -ceq 'installed') 'Expected a verified installed manifest.'

    & (Join-Path $paths.Install 'uninstall.ps1')
    Assert-Smoke (![IO.Directory]::Exists($paths.Install) -and (Get-UserPath $key).Text -ceq $pathFixture) 'Could not prepare a fresh application install for the PATH sentinel.'
    $autoDirectory = Join-Path $sandbox 'provider on PATH'
    $autoExecutable = New-ProbeSentinel $autoDirectory
    $autoLog = Join-Path $autoDirectory 'probe.log'
    $env:PATH = "$autoDirectory;$systemPath"
    Assert-Smoke ((Get-Command codex.exe).Source -ceq $autoExecutable) 'The synthetic provider is not on the controlled PATH.'
    $null = Invoke-TestInstall (Join-Path $source 'install.ps1')
    Assert-Smoke (![IO.File]::Exists($autoLog)) 'Fresh installation executed the provider on PATH.'
    Assert-Smoke (![IO.File]::Exists($registry)) 'Fresh installation recorded a provider observation.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $expectedPath) 'Fresh installation with a provider changed unrelated user PATH entries.'

    $movedSource = Join-Path $sandbox 'moved source'
    [IO.Directory]::Move($source, $movedSource)
    $env:PATH = [Environment]::ExpandEnvironmentVariables((Get-UserPath $key).Text) + ";$systemPath"
    Assert-Smoke ((Get-Command corinthos).Source -eq (Join-Path $paths.Bin 'corinthos.exe')) 'Command did not resolve through the persisted user PATH.'
    $version = & corinthos --version
    Assert-Smoke ($LASTEXITCODE -eq 0 -and $version -like 'corinthos *') 'Installed command failed after the checkout moved.'
    $homeOutput = '0' | & corinthos
    Assert-Smoke ($LASTEXITCODE -eq 0 -and 'CORINTHOS' -in $homeOutput) 'The command did not forward stdin and stdout to Home.'
    $desktop = Join-Path $paths.Bin 'corinthos-desktop.exe'
    $desktopSource = Join-Path $sandbox 'desktop-sentinel.cs'
    $desktopReceipt = Join-Path $paths.Bin 'desktop-sentinel.txt'
    [IO.File]::WriteAllText($desktopSource, @'
using System;
using System.IO;

internal static class DesktopSentinel
{
    public static void Main(string[] args)
    {
        string output = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "desktop-sentinel.txt");
        File.WriteAllText(output + ".tmp", args.Length + ":" + System.Diagnostics.Process.GetCurrentProcess().Id);
        File.Move(output + ".tmp", output);
    }
}
'@)
    & (Get-LauncherCompiler) /nologo /target:winexe /platform:x64 "/out:$desktop" $desktopSource | Out-Host
    Assert-Smoke ($LASTEXITCODE -eq 0) 'Could not compile the disposable desktop sentinel.'
    Assert-Smoke ((& corinthos --version) -ceq $version -and ![IO.File]::Exists($desktopReceipt)) 'CLI arguments unexpectedly started the desktop.'
    & corinthos
    Assert-Smoke ($LASTEXITCODE -eq 0) 'The no-argument launcher failed to start the adjacent desktop.'
    $desktopDeadline = [Diagnostics.Stopwatch]::StartNew()
    while (![IO.File]::Exists($desktopReceipt) -and $desktopDeadline.Elapsed.TotalSeconds -lt 10) {
        Start-Sleep -Milliseconds 100
    }
    Assert-Smoke ([IO.File]::Exists($desktopReceipt) -and [IO.File]::ReadAllText($desktopReceipt) -match '^0:([1-9][0-9]*)$') 'The launcher did not run the adjacent desktop with no arguments.'
    $desktopProcess = Get-Process -Id ([int] $Matches[1]) -ErrorAction SilentlyContinue
    if ($desktopProcess) {
        Assert-Smoke ($desktopProcess.WaitForExit(5000)) 'The disposable desktop sentinel did not exit.'
    }
    [IO.File]::Delete($desktopReceipt)
    [IO.File]::Delete($desktop)
    $errorOutput = & {
        $ErrorActionPreference = 'Continue'
        & corinthos --installer-smoke-invalid-option 2>&1
    }
    Assert-Smoke ($LASTEXITCODE -eq 2 -and ($errorOutput -join [Environment]::NewLine) -match 'unrecognized arguments') 'The command did not forward stderr and the CLI exit code.'

    $projects = @('Project A & notes', 'Project B')
    foreach ($name in $projects) {
        $root = Join-Path $other $name
        [void] [IO.Directory]::CreateDirectory($root)
        $null = & corinthos init $root
        Assert-Smoke ($LASTEXITCODE -eq 0 -and [IO.File]::Exists((Join-Path $root '.corinthos\bootstrap.json'))) 'One installed command failed to initialize an independent project.'
        Assert-Smoke (![IO.Directory]::Exists((Join-Path $root '.venv'))) 'Project initialization created a private project venv.'
        Set-Location -LiteralPath $root
        $null = @('0', '0') | & corinthos .
        Assert-Smoke ($LASTEXITCODE -eq 0) 'Installed command failed to open an existing project.'
    }
    Set-Location -LiteralPath $other
    $recent = Join-Path $paths.State 'recent-projects.json'
    Assert-Smoke ((Get-Content -LiteralPath $recent -Raw | ConvertFrom-Json).projects.Count -eq 2) 'Installed Home did not record both projects.'
    $config = Join-Path $paths.State 'config.json'
    $configHash = (Get-FileHash -LiteralPath $config).Hash
    $recentHash = (Get-FileHash -LiteralPath $recent).Hash

    $installer = Join-Path $movedSource 'install.ps1'
    $configuredDirectory = Join-Path $sandbox 'configured provider'
    [void] [IO.Directory]::CreateDirectory($configuredDirectory)
    $configuredExecutable = Join-Path $configuredDirectory 'codex.exe'
    $configuredLog = Join-Path $configuredDirectory 'probe.log'
    [IO.File]::Copy($autoExecutable, $configuredExecutable)
    $registryFixture = @{
        version = 1
        providers = @{ codex = @{ executable = $configuredExecutable; observed = $null } }
    } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($registry, $registryFixture)
    $registryHash = (Get-FileHash -LiteralPath $registry).Hash
    $env:PATH += ";$autoDirectory"
    $key.SetValue('Path', "$expectedPath;$($paths.Bin)\", [Microsoft.Win32.RegistryValueKind]::ExpandString)
    $reinstall = Invoke-TestInstall $installer
    Assert-Smoke (!($reinstall -match 'Downloading ')) 'Reinstall did not reuse the verified cache.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $expectedPath) 'Reinstall did not deduplicate the owned PATH.'
    Assert-Smoke ((Get-FileHash -LiteralPath $config).Hash -ceq $configHash -and (Get-FileHash -LiteralPath $recent).Hash -ceq $recentHash) 'Reinstall changed compatible user settings or recent projects.'
    Assert-Smoke ((Get-FileHash -LiteralPath $registry).Hash -ceq $registryHash) 'Reinstall changed compatible provider configuration or observations.'
    Assert-Smoke (![IO.File]::Exists($configuredLog) -and ![IO.File]::Exists($autoLog)) 'Reinstall executed an optional provider.'

    $null = & corinthos adapters refresh
    Assert-Smoke ($LASTEXITCODE -eq 0 -and [IO.File]::Exists($configuredLog)) 'Explicit refresh did not probe the configured provider.'
    $actualProbes = [IO.File]::ReadAllLines($configuredLog) -join '|'
    Assert-Smoke ($actualProbes -ceq '--version|--help|exec --help|exec resume --help') 'Explicit refresh did not perform the bounded native probes.'
    Assert-Smoke (![IO.File]::Exists($autoLog)) 'Explicit refresh ignored the configured provider in favor of PATH.'
    $codex = (Get-Content -LiteralPath $registry -Raw | ConvertFrom-Json).providers.codex
    Assert-Smoke (
        $codex.executable -ceq $configuredExecutable -and $codex.observed.executable -ceq $configuredExecutable -and
        $codex.observed.discovery -ceq 'explicit' -and $codex.observed.version -ceq '9.8.7' -and
        $codex.observed.available -and $codex.observed.json_events -and $codex.observed.stdin_prompt -and
        $codex.observed.native_resume -and $codex.observed.read_only
    ) 'Explicit refresh did not record the observed native provider capabilities.'
    Write-Host 'Fresh install and configured reinstall executed no provider. Explicit refresh verified the configured synthetic provider.'

    $manifest = Get-Content -LiteralPath (Join-Path $movedSource 'platform\windows\artifacts.json') -Raw | ConvertFrom-Json
    foreach ($pin in @($manifest.python, $manifest.wheels[0])) {
        $artifact = Join-Path $paths.Cache $pin.filename
        $goodBytes = [IO.File]::ReadAllBytes($artifact)
        $badBytes = [byte[]] $goodBytes.Clone()
        $badBytes[0] = $badBytes[0] -bxor 1
        try {
            [IO.File]::WriteAllBytes($artifact, $badBytes)
            $null = Invoke-TestInstall $installer 'Cached artifact SHA-256 verification failed'
            Assert-Smoke ((Read-InstallManifest $paths.Install).state -ceq 'installed') 'A corrupt artifact displaced the previous installation.'
        } finally { [IO.File]::WriteAllBytes($artifact, $goodBytes) }
    }
    $stateModule = Join-Path $movedSource 'src\corinthos\user_state.py'
    [IO.File]::AppendAllText($stateModule, @'

if __name__ == '__main__':
    raise SystemExit(77)
'@)
    $installedStateModule = Join-Path $paths.Install 'app\corinthos\user_state.py'
    $healthyHash = (Get-FileHash -LiteralPath $installedStateModule).Hash
    $null = Invoke-TestInstall $installer 'Private Python check failed (exit 77)'
    Assert-Smoke ((Get-FileHash -LiteralPath $installedStateModule).Hash -ceq $healthyHash) 'Post-swap health failure did not restore the previous installation.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $expectedPath) 'Post-swap health failure modified PATH.'
    Assert-Smoke ((& corinthos --version) -ceq $version -and $LASTEXITCODE -eq 0) 'Rollback did not restore a working command.'

    $uninstaller = Join-Path $paths.Install 'uninstall.ps1'
    $unrelatedFile = Join-Path $paths.Install 'owner-notes.txt'
    [IO.File]::WriteAllText($unrelatedFile, 'preserve')
    Assert-UninstallRefused $uninstaller 'entries outside their Corinthos inventory'
    Assert-Smoke ([IO.File]::ReadAllText($unrelatedFile) -ceq 'preserve') 'Uninstall changed an unrelated file.'
    [IO.File]::Delete($unrelatedFile)
    $embeddedProject = Join-Path $paths.Install 'project\.corinthos'
    [void] [IO.Directory]::CreateDirectory($embeddedProject)
    $projectFile = Join-Path $embeddedProject 'state.db'
    [IO.File]::WriteAllText($projectFile, 'project state')
    $null = Invoke-TestInstall $installer 'project exists inside the installation'
    Assert-UninstallRefused $uninstaller 'project exists inside the installation'
    Assert-Smoke ([IO.File]::ReadAllText($projectFile) -ceq 'project state') 'Uninstall changed nested project state.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $expectedPath) 'Refused uninstall changed PATH.'
    [IO.Directory]::Move((Join-Path $paths.Install 'project'), (Join-Path $other 'preserved project'))

    & $uninstaller
    Assert-Smoke (![IO.Directory]::Exists($paths.Install)) 'Uninstall left the installed application.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $pathFixture) 'Uninstall changed unrelated PATH entries.'
    foreach ($name in $projects) {
        Assert-Smoke ([IO.File]::Exists((Join-Path (Join-Path $other $name) '.corinthos\bootstrap.json'))) 'Uninstall removed project state.'
    }
    Assert-Smoke ((Get-FileHash -LiteralPath $config).Hash -ceq $configHash -and (Get-FileHash -LiteralPath $recent).Hash -ceq $recentHash) 'Uninstall removed or changed global user state.'
    Write-Host 'Fresh install, PATH, no-system-Python command, project isolation, cache, rollback and uninstall smoke passed.'
} finally {
    try { Restore-UserPath $key $originalUserPath; Send-UserPathChange }
    finally {
        $key.Dispose()
        $env:LOCALAPPDATA = $originalLocal
        $env:PATH = $originalProcessPath
        Set-Location -LiteralPath $originalLocation.Path
        Remove-SetupTree $sandbox $runnerTemp
    }
}
