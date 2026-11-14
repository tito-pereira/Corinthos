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
. (Join-Path $PSScriptRoot 'test-installed-desktop.ps1')

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
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;

internal static class Sentinel
{
    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "fixture-child")
        {
            Thread.Sleep(120000);
            return 0;
        }
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
        if (args.Length > 0 && args[0] == "exec" && Array.IndexOf(args, "--json") >= 0)
        {
            string prompt = Console.In.ReadToEnd();
            if (!prompt.Contains("Create the installed desktop fixture") ||
                !prompt.Contains("projection_digest")) return 3;
            using (var child = Process.Start(new ProcessStartInfo {
                FileName = Assembly.GetExecutingAssembly().Location,
                Arguments = "fixture-child",
                UseShellExecute = false,
                CreateNoWindow = true,
            }))
            {
                Console.WriteLine("{\"type\":\"thread.started\",\"thread_id\":\"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\"}");
                Console.WriteLine("{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Fixture provider claimed VERIFIED. api_key=installed-fixture-hidden-value\"}}");
                Console.Out.Flush();
                Thread.Sleep(120000);
            }
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

function Test-ReleaseEvidence([string] $Install, [string] $Commit, [string] $Tree, [string] $Source) {
    $manifestPath = Join-Path $Install 'release-manifest.json'
    $manifestText = [IO.File]::ReadAllText($manifestPath)
    $manifest = $manifestText | ConvertFrom-Json
    Assert-Smoke ($manifest.product_version -ceq '0.1.0' -and
        $manifest.python_runtime -ceq '3.14.10' -and $manifest.rpc_protocol -eq 2 -and $manifest.sqlite_schema -eq 9 -and
        $manifest.source_commit -ceq $Commit -and $manifest.source_tree -ceq $Tree -and
        $manifest.signing_state -ceq 'UNSIGNED' -and $manifest.auto_updater -ceq 'NOT_CONFIGURED' -and
        $manifest.real_provider_validation -ceq 'NOT_AVAILABLE') 'Release manifest did not identify the exact candidate and platform capabilities.'
    Assert-Smoke (!$manifestText.Contains($Source.Replace('\', '\\')) -and
        !$manifestText.Contains($Install.Replace('\', '\\')) -and
        !$manifestText.Contains('installed-fixture-hidden-value')) 'Release manifest includes a private path or fixture secret.'
    $expected = @{
        'bin/corinthos-desktop.exe' = $manifest.desktop_binary_sha256
        'bin/corinthos.exe' = $manifest.launcher_sha256
        'resources/artifacts.json' = $manifest.artifact_manifest_sha256
        'resources/toolchain-artifacts.json' = $manifest.toolchain_manifest_sha256
        'release-manifest.json' = (Get-FileHash -LiteralPath $manifestPath).Hash
    }
    $sums = Join-Path $Install 'SHA256SUMS'
    $inventory = @{}
    foreach ($line in [IO.File]::ReadAllLines($sums)) {
        Assert-Smoke ($line -match '^([a-f0-9]{64})  ((?:bin/|resources/)?[a-z0-9][a-z0-9.-]+)$') 'Release checksum file contains a malformed or unsafe relative path.'
        $digest = $Matches[1]
        $file = $Matches[2]
        Assert-Smoke (!$inventory.ContainsKey($file)) 'Release checksum file repeats an entry.'
        $actual = (Get-FileHash -LiteralPath (Join-Path $Install $file)).Hash
        Assert-Smoke ($actual -eq $digest) "Release checksum mismatch for $file."
        $inventory[$file] = $digest
    }
    foreach ($file in $expected.Keys) {
        Assert-Smoke ($inventory.ContainsKey($file) -and $inventory[$file] -eq $expected[$file]) "Release provenance omitted or misidentified $file."
    }
    foreach ($file in @('resources/LICENSE', 'resources/third-party-notices.json', 'resources/RELEASE-NOTES.md')) {
        Assert-Smoke ($inventory.ContainsKey($file)) "Release provenance omitted $file."
    }
    return $inventory.Keys
}

function Save-ReleaseCandidate($Paths, [string] $RunnerTemp, [string[]] $Evidence) {
    if (!$env:CORINTHOS_CI_ARTIFACTS) { return }
    $artifactRoot = [IO.Path]::GetFullPath($env:CORINTHOS_CI_ARTIFACTS)
    Assert-Smoke ($artifactRoot.StartsWith("$RunnerTemp\", [StringComparison]::OrdinalIgnoreCase) -and
        ![IO.Directory]::Exists($artifactRoot)) 'The release artifact directory must be new and under the disposable runner temp.'
    Assert-PlainPath $artifactRoot
    Assert-InstalledInventory $Paths.Install (Read-InstallManifest $Paths.Install)
    [void] [IO.Directory]::CreateDirectory($artifactRoot)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archiveName = 'corinthos-windows-x64-0.1.0.zip'
    $archive = Join-Path $artifactRoot $archiveName
    [IO.Compression.ZipFile]::CreateFromDirectory($Paths.Install, $archive)
    $checksum = (Get-FileHash -LiteralPath $archive).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText((Join-Path $artifactRoot 'ARCHIVE-SHA256SUMS'), "$checksum  $archiveName" + [Environment]::NewLine)
    foreach ($relative in ($Evidence + 'SHA256SUMS')) {
        $output = Join-Path $artifactRoot $relative
        [void] [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($output))
        [IO.File]::Copy((Join-Path $Paths.Install $relative), $output)
    }
}

function Test-InstallRecovery($Paths, [Microsoft.Win32.RegistryKey] $Key, [string] $ExpectedPath) {
    $manifest = Read-InstallManifest $Paths.Install
    $checksum = (Get-FileHash -LiteralPath (Join-Path $Paths.Install 'release-manifest.json')).Hash
    [IO.Directory]::Move($Paths.Install, $Paths.Previous)
    Restore-PreviousInstall $Paths $Key
    Assert-Smoke ([IO.Directory]::Exists($Paths.Install) -and ![IO.Directory]::Exists($Paths.Previous)) 'Recovery did not restore an interrupted old-install rename.'

    [IO.Directory]::Move($Paths.Install, $Paths.Previous)
    Copy-Item -LiteralPath $Paths.Previous -Destination $Paths.Install -Recurse
    Write-InstallManifest $Paths.Install 'staged' $manifest.version $Paths.Programs
    Write-SetupPathJournal $Paths (Get-UserPath $Key)
    $key.SetValue('Path', "$ExpectedPath;$($Paths.Bin)\", [Microsoft.Win32.RegistryValueKind]::ExpandString)
    Restore-PreviousInstall $Paths $Key
    Assert-Smoke ((Read-InstallManifest $Paths.Install).state -ceq 'installed' -and
        (Get-FileHash -LiteralPath (Join-Path $Paths.Install 'release-manifest.json')).Hash -eq $checksum -and
        ![IO.Directory]::Exists($Paths.Previous) -and ![IO.File]::Exists((Get-SetupPathJournal $Paths)) -and
        (Get-UserPath $Key).Text -ceq $ExpectedPath) 'Recovery did not restore the committed install and exact registry PATH after staged promotion.'

    $marked = Join-Path $Paths.Programs ('.Corinthos-stage-' + [guid]::NewGuid().ToString('N'))
    $unowned = Join-Path $Paths.Programs ('.Corinthos-stage-' + [guid]::NewGuid().ToString('N'))
    foreach ($directory in @($marked, $unowned)) { [void] [IO.Directory]::CreateDirectory($directory) }
    [IO.File]::WriteAllText((Join-Path $marked 'corinthos-stage.marker'), 'corinthos-core-stage-v1')
    Clear-AbandonedInstallStages $Paths
    Assert-Smoke (![IO.Directory]::Exists($marked) -and [IO.Directory]::Exists($unowned)) 'Stage recovery did not distinguish owned and unmarked directories.'
    [IO.Directory]::Delete($unowned)
}

$runnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).TrimEnd('\')
$sandbox = Join-Path $runnerTemp ('corinthos-install-' + [guid]::NewGuid().ToString('N'))
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$git = (Get-Command git.exe -ErrorAction Stop).Source
$gitDirectory = [IO.Path]::GetDirectoryName($git)
$powerShell = Join-Path $PSHOME 'powershell.exe'
$baseline = '4a0ceb16e7ed4aa95c2a1123547386f45f3dbb23'
$source = Join-Path $sandbox 'source checkout'
$other = Join-Path $sandbox 'unrelated workspace'
$originalLocal = $env:LOCALAPPDATA
$originalProcessPath = $env:PATH
$originalLocation = Get-Location
$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
$originalUserPath = Get-UserPath $key
$env:LOCALAPPDATA = Join-Path $sandbox 'local'
$systemPath = "$env:SystemRoot\System32;$gitDirectory"
$env:PATH = $systemPath
$pathFixture = '%SystemRoot%\System32;C:\corinthos-smoke-unrelated'
try {
    $paths = Get-CorinthosPaths
    [void] [IO.Directory]::CreateDirectory($other)
    & $git clone --no-local --no-hardlinks --quiet -- $repo $source
    Assert-Smoke ($LASTEXITCODE -eq 0) 'Could not create the isolated release candidate clone.'
    $commit = & $git -C $repo rev-parse HEAD
    Assert-Smoke ($LASTEXITCODE -eq 0 -and $commit -match '^[a-f0-9]{40}$') 'Could not identify the exact tested commit.'
    & $git -C $source checkout --quiet --detach $commit
    Assert-Smoke ($LASTEXITCODE -eq 0) 'Could not detach the isolated clone at the exact tested commit.'
    $tree = & $git -C $source rev-parse 'HEAD^{tree}'
    Assert-Smoke ($LASTEXITCODE -eq 0 -and $tree -match '^[a-f0-9]{40}$') 'Could not identify the exact tested source tree.'
    $key.SetValue('Path', $pathFixture, [Microsoft.Win32.RegistryValueKind]::ExpandString)
    Assert-Smoke (!$(
        Get-Command -Name @('python.exe', 'py.exe', 'pip.exe', 'node.exe', 'npm.cmd', 'rustc.exe', 'cargo.exe',
            'corinthos.exe', 'codex.exe', 'claude.exe') -ErrorAction SilentlyContinue
    )) 'Smoke PATH must exclude system Python, application toolchains and providers.'
    Set-Location -LiteralPath $source
    $firstBuild = [Diagnostics.Stopwatch]::StartNew()
    $null = Invoke-TestInstall (Join-Path $source 'install.ps1')
    $firstBuild.Stop()
    Write-Host "Cold source installation took $([math]::Round($firstBuild.Elapsed.TotalSeconds)) seconds."
    $expectedPath = "$($paths.Bin);$pathFixture"
    $registry = Join-Path $paths.State 'providers.json'
    Assert-Smoke (![IO.File]::Exists($registry)) 'A clean installation must leave providers unobserved.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $expectedPath) 'Install did not add only the owned user PATH entry.'
    Assert-Smoke ((Get-UserPath $key).Kind -eq [Microsoft.Win32.RegistryValueKind]::ExpandString) 'Install changed PATH registry type.'
    Assert-Smoke ((Read-InstallManifest $paths.Install).state -ceq 'installed') 'Expected a verified installed manifest.'
    Assert-Smoke (!(& $git -C $source status --porcelain=v1 --untracked-files=normal) -and $LASTEXITCODE -eq 0) 'Installation modified the source tree or dependency lockfiles.'
    $releaseFiles = @(Test-ReleaseEvidence $paths.Install $commit $tree $source)
    foreach ($tool in @('node.exe', 'npm.cmd', 'rustup.exe', 'cargo.exe', 'rustc.exe', 'node_modules')) {
        Assert-Smoke (!@(Get-ChildItem -LiteralPath $paths.Install -Recurse -Force -Filter $tool).Count) "Installed runtime contains build tool $tool."
    }

    Set-Location -LiteralPath $other
    $autoDirectory = Join-Path $sandbox 'provider on PATH'
    $autoExecutable = New-ProbeSentinel $autoDirectory
    $autoLog = Join-Path $autoDirectory 'probe.log'
    $env:PATH = "$autoDirectory;$systemPath"
    Assert-Smoke ((Get-Command codex.exe).Source -ceq $autoExecutable) 'The synthetic provider is not on the controlled PATH.'
    $movedSource = Join-Path $sandbox 'moved source'
    [IO.Directory]::Move($source, $movedSource)
    $env:PATH = [Environment]::ExpandEnvironmentVariables((Get-UserPath $key).Text) + ";$systemPath"
    Assert-Smoke ((Get-Command corinthos).Source -eq (Join-Path $paths.Bin 'corinthos.exe')) 'Command did not resolve through the persisted user PATH.'
    $version = & $powerShell -NoProfile -NonInteractive -Command 'corinthos --version; exit $LASTEXITCODE'
    Assert-Smoke ($LASTEXITCODE -eq 0 -and $version -like 'corinthos *') 'Installed command failed after the checkout moved.'
    $desktop = Join-Path $paths.Bin 'corinthos-desktop.exe'
    $privatePython = Join-Path $paths.Install 'runtime\python.exe'
    Assert-Smoke ((& corinthos --version) -ceq $version -and !(Find-ProductProcesses @($desktop))) 'CLI arguments unexpectedly started the desktop.'
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
        $terminalCommand = @('0', '0') | & corinthos .
        Assert-Smoke ($LASTEXITCODE -eq 0 -and 'PROJECT' -in $terminalCommand -and
            'CORINTHOS' -in $terminalCommand) 'Installed command did not pass terminal input/output or open an existing project.'
    }
    Set-Location -LiteralPath $other
    $recent = Join-Path $paths.State 'recent-projects.json'
    Assert-Smoke ((Get-Content -LiteralPath $recent -Raw | ConvertFrom-Json).projects.Count -eq 2) 'Installed Home did not record both projects.'
    $uiProvider = New-ProbeSentinel (Join-Path $sandbox 'GUI configured provider')
    $uiProviderLog = Join-Path ([IO.Path]::GetDirectoryName($uiProvider)) 'probe.log'
    Invoke-InstalledDesktopSmoke $desktop $privatePython (Join-Path $other $projects[0]) $uiProvider $uiProviderLog
    Assert-Smoke (![IO.File]::Exists($autoLog)) 'Installed desktop executed the provider on PATH.'
    Save-ReleaseCandidate $paths $runnerTemp $releaseFiles
    $config = Join-Path $paths.State 'config.json'
    $configHash = (Get-FileHash -LiteralPath $config).Hash
    $recentHash = (Get-FileHash -LiteralPath $recent).Hash

    $installer = Join-Path $movedSource 'install.ps1'
    $lockfile = Join-Path $movedSource 'apps\desktop\package-lock.json'
    $originalLockfile = [IO.File]::ReadAllBytes($lockfile)
    $healthyDesktopHash = (Get-FileHash -LiteralPath $desktop).Hash
    try {
        [IO.File]::AppendAllText($lockfile, [Environment]::NewLine)
        $null = Invoke-TestInstall $installer 'checkout changes'
        Assert-Smoke ((Get-FileHash -LiteralPath $desktop).Hash -eq $healthyDesktopHash) 'A changed lockfile displaced the previous release.'
    } finally { [IO.File]::WriteAllBytes($lockfile, $originalLockfile) }

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
    $toolchain = Get-Content -LiteralPath (Join-Path $movedSource 'platform\windows\toolchain-artifacts.json') -Raw | ConvertFrom-Json
    $artifact = Join-Path (Join-Path $paths.Cache 'toolchains') $toolchain.node.filename
    $goodBytes = [IO.File]::ReadAllBytes($artifact)
    $badBytes = [byte[]] $goodBytes.Clone()
    $badBytes[0] = $badBytes[0] -bxor 1
    try {
        [IO.File]::WriteAllBytes($artifact, $badBytes)
        $null = Invoke-TestInstall $installer 'Cached artifact SHA-256 verification failed'
        Assert-Smoke ((Read-InstallManifest $paths.Install).state -ceq 'installed') 'A corrupt toolchain artifact displaced the previous installation.'
    } finally { [IO.File]::WriteAllBytes($artifact, $goodBytes) }
    . (Join-Path $PSScriptRoot 'toolchain.ps1')
    $signatureFailure = ''
    try { $null = Assert-MicrosoftSetupSignature $autoExecutable $toolchain.microsoft.publisher }
    catch { $signatureFailure = $_.Exception.Message }
    Assert-Smoke ($signatureFailure -like '*authenticity verification failed*') 'The Microsoft prerequisite verifier accepted a disposable unsigned executable.'

    $runtime = Join-Path $paths.Install 'runtime'
    $temporaryRuntime = Join-Path $sandbox 'ordinary private runtime'
    [IO.Directory]::Move($runtime, $temporaryRuntime)
    try {
        $null = New-Item -ItemType Junction -Path $runtime -Target $temporaryRuntime
        $null = & { $ErrorActionPreference = 'Continue'; corinthos --version 2>&1 }
        Assert-Smoke ($LASTEXITCODE -ne 0) 'Installed launcher accepted a junction to the same private runtime.'
        $health = Start-Process -FilePath $desktop -ArgumentList '--installation-check' -PassThru -WindowStyle Hidden
        try {
            Assert-Smoke ($health.WaitForExit(15000) -and $health.ExitCode -ne 0) 'Installed desktop accepted a junction to the same private runtime.'
        } finally { $health.Dispose() }
    } finally {
        if ([IO.Directory]::Exists($runtime)) { [IO.Directory]::Delete($runtime) }
        [IO.Directory]::Move($temporaryRuntime, $runtime)
    }

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

    # The pinned Phase 8 layout provides a real upgrade fixture and keeps legacy rollback regression coverage.
    $legacy = Join-Path $sandbox 'Phase 8 source'
    $archive = Join-Path $sandbox 'phase-8.zip'
    & $git -C $repo archive --format=zip "--output=$archive" $baseline
    Assert-Smoke ($LASTEXITCODE -eq 0) 'Could not archive the exact Phase 8 installer baseline.'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $legacy)
    $previousInstall = Join-Path $sandbox 'built candidate'
    [IO.Directory]::Move($paths.Install, $previousInstall)
    $legacyInstaller = Join-Path $legacy 'install.ps1'
    $null = Invoke-TestInstall $legacyInstaller
    Assert-Smoke (![IO.File]::Exists($desktop)) 'The Phase 8 layout fixture unexpectedly included a desktop.'
    $manifest = Get-Content -LiteralPath (Join-Path $legacy 'platform\windows\artifacts.json') -Raw | ConvertFrom-Json
    foreach ($pin in @($manifest.python, $manifest.wheels[0])) {
        $artifact = Join-Path $paths.Cache $pin.filename
        $goodBytes = [IO.File]::ReadAllBytes($artifact)
        $badBytes = [byte[]] $goodBytes.Clone()
        $badBytes[0] = $badBytes[0] -bxor 1
        try {
            [IO.File]::WriteAllBytes($artifact, $badBytes)
            $null = Invoke-TestInstall $legacyInstaller 'Cached artifact SHA-256 verification failed'
            Assert-Smoke ((Read-InstallManifest $paths.Install).state -ceq 'installed') 'A corrupt artifact displaced the previous installation.'
        } finally { [IO.File]::WriteAllBytes($artifact, $goodBytes) }
    }
    $stateModule = Join-Path $legacy 'src\corinthos\user_state.py'
    [IO.File]::AppendAllText($stateModule, @'

if __name__ == '__main__':
    raise SystemExit(77)
'@)
    $installedStateModule = Join-Path $paths.Install 'app\corinthos\user_state.py'
    $healthyHash = (Get-FileHash -LiteralPath $installedStateModule).Hash
    $null = Invoke-TestInstall $legacyInstaller 'Private Python check failed (exit 77)'
    Assert-Smoke ((Get-FileHash -LiteralPath $installedStateModule).Hash -ceq $healthyHash) 'Post-swap health failure did not restore the previous installation.'
    Assert-Smoke ((Get-UserPath $key).Text -ceq $expectedPath) 'Post-swap health failure modified PATH.'
    Assert-Smoke ((& corinthos --version) -ceq $version -and $LASTEXITCODE -eq 0) 'Rollback did not restore a working command.'

    $currentInstall = Join-Path $paths.Install 'installation.json'
    Assert-Smoke ([IO.File]::Exists($currentInstall)) 'The Phase 8 layout has no upgrade ownership receipt.'
    $beforeUpgradeRecent = (Get-FileHash -LiteralPath $recent).Hash
    $beforeUpgradeProviders = (Get-FileHash -LiteralPath $registry).Hash
    $projectDatabase = Join-Path (Join-Path $other $projects[0]) '.corinthos\state.db'
    $beforeUpgradeProject = (Get-FileHash -LiteralPath $projectDatabase).Hash
    $providerBeforeUpgrade = (Get-FileHash -LiteralPath $configuredLog).Hash
    $null = Invoke-TestInstall $installer
    Assert-Smoke ([IO.File]::Exists($desktop) -and (Get-FileHash -LiteralPath $recent).Hash -eq $beforeUpgradeRecent -and
        (Get-FileHash -LiteralPath $registry).Hash -eq $beforeUpgradeProviders -and
        (Get-FileHash -LiteralPath $projectDatabase).Hash -eq $beforeUpgradeProject -and
        (Get-FileHash -LiteralPath $configuredLog).Hash -eq $providerBeforeUpgrade -and
        ![IO.File]::Exists($autoLog)) 'The Phase 8 layout upgrade lost private state or ran a provider.'
    $releaseFiles = @(Test-ReleaseEvidence $paths.Install $commit $tree $movedSource)
    Assert-Smoke (([IO.File]::ReadAllText((Join-Path $paths.Install 'release-manifest.json')) | ConvertFrom-Json).source_commit -ceq $commit) 'Upgrade did not produce the exact source release.'
    Test-InstallRecovery $paths $key $expectedPath

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
    Assert-Smoke ((Get-FileHash -LiteralPath $projectDatabase).Hash -eq $beforeUpgradeProject) 'Uninstall modified the exact GUI Session and Work Item state.'
    Assert-Smoke ((Get-FileHash -LiteralPath $config).Hash -ceq $configHash -and (Get-FileHash -LiteralPath $recent).Hash -ceq $recentHash) 'Uninstall removed or changed global user state.'
    if ($env:CORINTHOS_CI_ARTIFACTS) {
        Write-ReleaseJson (Join-Path $env:CORINTHOS_CI_ARTIFACTS 'installed-verification.json') ([ordered] @{
            schema = 1
            source_commit = $commit
            source_tree = $tree
            platform = 'windows-x64'
            installed_product = 'PASS'
            shipping_binary_smoke = 'PASS'
            preconfigured_reinstall = 'PASS'
            phase_8_layout_upgrade = 'PASS'
            state_preserving_uninstall = 'PASS'
            signing_state = 'UNSIGNED'
            real_provider_validation = 'NOT_AVAILABLE'
        })
    }
    Write-Host 'Fresh install, PATH, no-system-Python command, project isolation, cache, rollback and uninstall smoke passed.'
} finally {
    try { Restore-UserPath $key $originalUserPath; Send-UserPathChange }
    finally {
        $key.Dispose()
        $env:LOCALAPPDATA = $originalLocal
        $env:PATH = $originalProcessPath
        Set-Location -LiteralPath $originalLocation.Path
        if ([IO.Directory]::Exists($sandbox)) {
            Assert-PlainPath $sandbox
            Assert-Smoke ([IO.Path]::GetDirectoryName($sandbox) -ceq $runnerTemp -and
                [IO.Path]::GetFileName($sandbox) -cmatch '^corinthos-install-[a-f0-9]{32}$') 'Refusing cleanup outside the exact runner fixture.'
            [void] (Get-SetupInventory $sandbox)
            Remove-Item -LiteralPath $sandbox -Recurse -Force
        }
    }
}
