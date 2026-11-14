# SPDX-License-Identifier: MPL-2.0
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Desktop
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
. (Join-Path $repo 'platform\windows\common.ps1')
. (Join-Path $repo 'platform\windows\distribution.ps1')

function Assert-Spike([bool] $Condition, [string] $Message) {
    if (!$Condition) { throw $Message }
}

function Invoke-FixtureGit([string[]] $Arguments) {
    $output = git -C $project --no-optional-locks @Arguments
    if ($LASTEXITCODE -ne 0) { throw "External fixture Git command failed: $($Arguments[0])." }
    return $output
}

function Invoke-DesktopFixture([string] $Stage) {
    $receipt = Join-Path $root "desktop-$Stage.txt"
    $env:CORINTHOS_DESKTOP_SMOKE_PROJECT = $project
    $env:CORINTHOS_DESKTOP_SMOKE_RECEIPT = $receipt
    $env:CORINTHOS_DESKTOP_SMOKE_START = if ($Stage -eq 'sessions') { '1' } else { $Stage }
    $script:desktopProcess = Start-Process -FilePath $desktopPath -WorkingDirectory $root -PassThru -WindowStyle Normal
    $deadline = [DateTime]::UtcNow.AddSeconds($(if ($Stage -eq 'import-commit') { 230 } else { 150 }))
    while (![IO.File]::Exists($receipt) -and [DateTime]::UtcNow -lt $deadline) {
        if ($desktopProcess.HasExited) { throw 'The desktop exited before producing its WebView receipt.' }
        Start-Sleep -Milliseconds 250
    }
    Assert-Spike ([IO.File]::Exists($receipt)) 'The actual WebView did not produce a receipt.'
    $observation = [IO.File]::ReadAllText($receipt).Trim()
    Assert-Spike ($observation -ceq 'pass') "The actual WebView failed its $Stage DOM checks: $observation"

    if ($Stage -eq 'sessions') {
        $providerTree = @(Get-CimInstance Win32_Process -Filter "Name = 'codex.exe'" |
            Where-Object { $_.ExecutablePath -eq $explicitExe })
        Assert-Spike ($providerTree.Count -eq 2) 'Cancel, timeout and switch should reap the earlier trees and leave one live provider with one child.'
    }
    Assert-Spike ($desktopProcess.CloseMainWindow()) 'Could not request desktop shutdown.'
    Assert-Spike ($desktopProcess.WaitForExit(22000) -and $desktopProcess.ExitCode -eq 0) 'Desktop shutdown did not complete.'
    $remaining = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'codex.exe' OR Name = 'claude.exe'" |
        Where-Object { $_.ExecutablePath -eq $python -or $_.ExecutablePath -eq $explicitExe -or $_.ExecutablePath -eq $autoExe -or $_.ExecutablePath -eq $claudeExe }
    Assert-Spike (!$remaining) 'Closing the desktop left an owned sidecar or fixture provider process running.'
    $desktopProcess.Dispose()
    $script:desktopProcess = $null
}


$parent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$root = Join-Path $parent ('corinthos-desktop-' + [Guid]::NewGuid().ToString('N'))
$originalLocal = $env:LOCALAPPDATA
$originalPath = $env:PATH
$originalOverride = $env:CORINTHOS_SIDECAR_PYTHON
$originalProject = $env:CORINTHOS_SIDECAR_TEST_PROJECT
$originalSmokeProject = $env:CORINTHOS_DESKTOP_SMOKE_PROJECT
$originalSmokeReceipt = $env:CORINTHOS_DESKTOP_SMOKE_RECEIPT
$originalSmokeStart = $env:CORINTHOS_DESKTOP_SMOKE_START
$desktopProcess = $null
try {
    $desktopPath = (Resolve-Path -LiteralPath $Desktop).Path
    $env:LOCALAPPDATA = Join-Path $root 'local'
    $paths = Get-CorinthosPaths
    $work = Join-Path $root 'staging'
    foreach ($directory in @($work, $paths.Cache, $paths.Programs)) {
        [void] [IO.Directory]::CreateDirectory($directory)
    }
    $script:InstallPercent = 0
    $candidate = New-InstallCandidate $repo $paths $work (Get-LauncherCompiler)
    $null = Test-InstallCandidate $candidate
    [IO.Directory]::Move($candidate, $paths.Install)
    $python = Join-Path $paths.Install 'runtime\python.exe'
    Invoke-PrivatePython $paths.Install @('-m', 'corinthos.user_state')

    $project = Join-Path $root 'project'
    $null = Invoke-PrivatePython $paths.Install @('-m', 'corinthos', 'init', $project)
    $profile = Join-Path $project '.corinthos\bootstrap.json'
    $profileHash = (Get-FileHash -LiteralPath $profile).Hash
    $recent = Join-Path $paths.State 'recent-projects.json'
    $missing = Join-Path $root 'missing-project'
    $legacyRegistry = @{
        version = 1
        projects = @(
            @{ path = $project; last_opened_at = '2026-01-02T00:00:00Z' }
            @{ path = $missing; last_opened_at = '2026-01-01T00:00:00Z' }
        )
    } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($recent, $legacyRegistry)
    $recentHash = (Get-FileHash -LiteralPath $recent).Hash

    $baseline = '13e7bf556788e737493d767b30e12a6613c8fa73'
    $archive = Join-Path $work 'phase-three.zip'
    $legacy = Join-Path $work 'phase-three'
    git -C $repo archive --format=zip "--output=$archive" $baseline -- src/corinthos
    if ($LASTEXITCODE -ne 0) { throw 'The exact Phase 3 source is required for the desktop v8 fixture.' }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $legacy)
    $legacySource = Join-Path $legacy 'src'
    $makeV8Path = Join-Path $work 'make_v8.py'
    $makeV8 = @'
import sqlite3, sys
from contextlib import closing
sys.path.insert(0, sys.argv[1])
from corinthos.contracts import ContractStore
from corinthos.contracts import store
from corinthos.sessions.repository import SessionRepository
from corinthos.work import WorkService
assert store._SCHEMA_VERSION == 8 and store.__file__.startswith(sys.argv[1])
work = WorkService(sys.argv[2])
item = work.create(
    scope_level="TASK",
    objective="Preserve this Work Item",
    acceptance_criteria=[
        "Keep the existing Work Item state unchanged.",
        "api_key=v8-fixture-hidden-value",
    ],
)
work.transition(item.work_item_id, "ACTIVE")
sessions = SessionRepository(sys.argv[2])
legacy = sessions.create(
    item.work_item_id, provider="codex", owner_id="fixture-owner",
    timeout_seconds=120, envelope_sha256="a" * 64,
)
fence = (legacy.session_id, legacy.owner_id, legacy.run_generation)
sessions.running(*fence, provider_version="9.8.6", model="fixture-model")
sessions.observe(*fence, "OUTPUT", {"text": "Historical fixture observation"})
sessions.observe(*fence, "USAGE", {}, usage={"input_tokens": 12, "output_tokens": 1})
sessions.finish(*fence, "SUCCEEDED")
database = ContractStore(sys.argv[2]).database_path
with closing(sqlite3.connect(database)) as connection:
    assert connection.execute("PRAGMA user_version").fetchone() == (8,)
    assert connection.execute("SELECT name FROM sqlite_schema WHERE name = 'context_projections'").fetchall() == []
    assert connection.execute("SELECT count(*) FROM agent_sessions").fetchone() == (1,)
'@
    [IO.File]::WriteAllText($makeV8Path, $makeV8)
    Invoke-PrivatePython $paths.Install @($makeV8Path, $legacySource, $project)
    $database = Join-Path $project '.corinthos\state.db'
    $databaseHash = (Get-FileHash -LiteralPath $database).Hash

    $providers = Join-Path $root 'providers'
    $configured = Join-Path $root 'configured'
    foreach ($directory in @($providers, $configured)) {
        [void] [IO.Directory]::CreateDirectory($directory)
    }
    $sentinelSource = Join-Path $root 'provider.cs'
    [IO.File]::WriteAllText($sentinelSource, @'
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;

internal static class Provider {
    private static int Main(string[] args) {
        if (args.Length == 1 && args[0] == "fixture-child") {
            Thread.Sleep(120000);
            return 0;
        }
        string log = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "probe.log");
        if (args.Length == 1 && args[0] == "--version") {
            File.AppendAllText(log, "--version" + Environment.NewLine);
            Console.WriteLine("codex-cli 9.8.7");
        } else if (Array.IndexOf(args, "--help") >= 0) {
            File.AppendAllText(log, String.Join(" ", args) + Environment.NewLine);
            Console.WriteLine("--sandbox read-only --json stdin SESSION_ID");
        } else {
            if (args.Length == 0 || args[0] != "exec" || Array.IndexOf(args, "--json") < 0) return 2;
            string input = Console.In.ReadToEnd();
            if (input.Contains("v8-fixture-hidden-value") ||
                !input.Contains("projection_id") ||
                !input.Contains("projection_digest")) return 3;
            if (input.Contains("Adaptive fixture: add one local check")) {
                File.AppendAllText(log, "adaptive-launch" + Environment.NewLine);
                Console.WriteLine("{\"type\":\"thread.started\",\"thread_id\":\"cccccccc-cccc-4ccc-8ccc-cccccccccccc\"}");
                Console.WriteLine("{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Adaptive fixture observation\"}}");
                Console.WriteLine("{\"type\":\"turn.completed\",\"usage\":{\"output_tokens\":1}}");
                return 0;
            }
            if (input.Contains("Import fixture: review payments")) {
                if (!input.Contains("PHASE7_PAYMENTS_REPOSITORY_MARKER") ||
                    input.Contains("PHASE7_UNRELATED_CALENDAR_MARKER")) return 4;
                File.AppendAllText(log, "import-change-launch" + Environment.NewLine);
                Console.WriteLine("{\"type\":\"thread.started\",\"thread_id\":\"dddddddd-dddd-4ddd-8ddd-dddddddddddd\"}");
                Console.WriteLine("{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Observed the imported payments context marker.\"}}");
                Console.WriteLine("{\"type\":\"turn.completed\",\"usage\":{\"output_tokens\":1}}");
                return 0;
            }
            File.AppendAllText(log, "launch" + Environment.NewLine);
            using (var child = Process.Start(new ProcessStartInfo {
                FileName = Assembly.GetExecutingAssembly().Location,
                Arguments = "fixture-child",
                UseShellExecute = false,
                CreateNoWindow = true,
            })) {
                Console.WriteLine("{\"type\":\"thread.started\",\"thread_id\":\"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa\"}");
                Console.WriteLine("{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"Deterministic session output\"}}");
                Console.Out.Flush();
                Thread.Sleep(120000);
            }
        }
        return 0;
    }
}
'@)
    $claudeSource = Join-Path $root 'claude.cs'
    [IO.File]::WriteAllText($claudeSource, @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;

internal static class ClaudeFixture {
    private static int Main(string[] args) {
        string log = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "claude.log");
        if (args.Length == 1 && args[0] == "--version") {
            File.AppendAllText(log, "--version" + Environment.NewLine);
            Console.WriteLine("claude 1.2.3");
            return 0;
        }
        if (args.Length == 1 && args[0] == "--help") {
            File.AppendAllText(log, "--help" + Environment.NewLine);
            Console.WriteLine("--print --output-format stream-json --verbose --resume --tools --disallowedTools");
            return 0;
        }
        if (Array.IndexOf(args, "--print") < 0 || Array.IndexOf(args, "stream-json") < 0 ||
            Array.IndexOf(args, "--resume") >= 0 ||
            Array.IndexOf(args, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") >= 0) return 2;
        string input = Console.In.ReadToEnd();
        if (input.Contains("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa") || input.Contains("v8-fixture-hidden-value")) return 3;
        var json = new JavaScriptSerializer();
        var outer = json.Deserialize<Dictionary<string, object>>(input.Substring(input.IndexOf('\n') + 1));
        var context = (Dictionary<string, object>)outer["context"];
        var objective = (string)context["objective"];
        var envelope = json.Deserialize<Dictionary<string, object>>(objective.Substring(objective.IndexOf("\n{") + 1));
        var projection = (Dictionary<string, object>)envelope["projection"];
        string work = (string)projection["work_item_id"];
        string handoff = (string)projection["required_handoff_id"];
        if ((string)projection["target_provider"] != "claude" || !work.StartsWith("work-") ||
            !handoff.StartsWith("handoff-")) return 4;
        bool sourceObserved = false;
        foreach (Dictionary<string, object> source in (System.Collections.IEnumerable)projection["sources"]) {
            if ((string)source["identity"] != handoff || (string)source["kind"] != "SESSION_HANDOFF" ||
                (string)source["authority"] != "DERIVED") continue;
            var capture = (Dictionary<string, object>)source["capture"];
            var sourceSession = (Dictionary<string, object>)capture["source_session"];
            if ((string)sourceSession["work_item_id"] != work || (string)sourceSession["provider"] != "codex" ||
                (string)sourceSession["state"] != "CANCELLED") return 5;
            foreach (Dictionary<string, object> entry in (System.Collections.IEnumerable)capture["observations"]) {
                var payload = (Dictionary<string, object>)entry["payload"];
                if ((string)entry["kind"] == "OUTPUT" && payload.ContainsKey("text") &&
                    (string)payload["text"] == "Deterministic session output") sourceObserved = true;
            }
        }
        if (!sourceObserved) return 6;
        File.AppendAllText(log, "launch" + Environment.NewLine);
        const string nativeId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
        Console.WriteLine(json.Serialize(new { type = "system", subtype = "init", session_id = nativeId }));
        Console.WriteLine(json.Serialize(new {
            type = "result", subtype = "success", is_error = false, session_id = nativeId,
            result = "Verified switch continuity from the source Handoff",
            usage = new { input_tokens = 12, output_tokens = 2 },
        }));
        return 0;
    }
}
'@)
    $claudeExe = Join-Path $configured 'claude.exe'
    & (Get-LauncherCompiler) /nologo /target:exe /platform:x64 /reference:System.Web.Extensions.dll "/out:$claudeExe" $claudeSource
    if ($LASTEXITCODE -ne 0) { throw 'Could not compile the independent Claude fixture.' }
    $claudeLog = Join-Path $configured 'claude.log'
    $autoExe = Join-Path $providers 'codex.exe'
    $explicitExe = Join-Path $configured 'codex.exe'
    & (Get-LauncherCompiler) /nologo /target:exe /platform:x64 "/out:$autoExe" $sentinelSource
    if ($LASTEXITCODE -ne 0) { throw 'Could not compile the synthetic provider.' }
    [IO.File]::Copy($autoExe, $explicitExe)
    $autoLog = Join-Path $providers 'probe.log'
    $configuredLog = Join-Path $configured 'probe.log'
    $env:PATH = "$providers;$originalPath"
    $registry = Join-Path $paths.State 'providers.json'
    $manifest = Join-Path $repo 'apps\desktop\src-tauri\Cargo.toml'
    $env:CORINTHOS_SIDECAR_PYTHON = $null
    $env:CORINTHOS_SIDECAR_TEST_PROJECT = $project
    cargo test --manifest-path $manifest --locked --no-default-features private_runtime_repeated_cycles -- --ignored --nocapture
    if ($LASTEXITCODE -ne 0) { throw 'Private runtime lifecycle integration failed.' }
    Assert-Spike (![IO.File]::Exists($registry) -and ![IO.File]::Exists($autoLog)) 'Passive bridge started a PATH provider or wrote its registry.'
    Assert-Spike ((Get-FileHash -LiteralPath $database).Hash -eq $databaseHash) 'Passive bridge migrated the v8 project.'
    Assert-Spike ((Get-FileHash -LiteralPath $recent).Hash -eq $recentHash) 'Passive bridge changed the v1 registry.'

    $fixture = @{
        version = 1
        providers = @{
            codex = @{ executable = $explicitExe; observed = $null }
            claude = @{ executable = $claudeExe; observed = $null }
        }
    } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($registry, $fixture)
    $registryHash = (Get-FileHash -LiteralPath $registry).Hash
    $env:CORINTHOS_SIDECAR_PYTHON = $null
    Invoke-DesktopFixture 'passive'
    Assert-Spike (![IO.File]::Exists($autoLog) -and ![IO.File]::Exists($configuredLog) -and ![IO.File]::Exists($claudeLog)) 'Passive Context, Agents or switch status launched a provider.'
    Assert-Spike ((Get-FileHash -LiteralPath $registry).Hash -eq $registryHash) 'Context or passive Agents rendering rewrote provider state.'
    Write-Host 'Desktop WebView displayed the migrated v9 project, verified an immutable projection and its redacted source provenance, and inspected legacy Agents without native probes.'

    Invoke-DesktopFixture 'sessions'
    Assert-Spike (![IO.File]::Exists($autoLog) -and [IO.File]::Exists($configuredLog)) 'Explicit sessions did not select only the configured provider.'
    Assert-Spike (@([IO.File]::ReadAllLines($configuredLog) | Where-Object { $_ -eq 'launch' }).Count -eq 4) 'Expected four supervised Codex invocations.'
    Assert-Spike ((Get-FileHash -LiteralPath $registry).Hash -eq $registryHash) 'Starting sessions rewrote the configured provider registry.'
    Assert-Spike (@([IO.File]::ReadAllLines($claudeLog) | Where-Object { $_ -eq 'launch' }).Count -eq 1) 'Switch replay should reuse the single Claude session.'
    Assert-Spike ((Get-FileHash -LiteralPath $profile).Hash -eq $profileHash) 'Desktop changed the project bootstrap.'
    Write-Host 'Actual WebView switched Codex to an independent Claude fixture after source cleanup, verified continuity and replay, and closed with an active provider. The sidecar and all fixture provider trees were reaped.'


    $savedRegistry = Get-Content -Raw -LiteralPath $recent | ConvertFrom-Json
    Assert-Spike ($savedRegistry.version -eq 2 -and $savedRegistry.projects.Count -eq 2 -and $savedRegistry.projects[0].root -eq $project) 'Explicit project open lost legacy registry entries.'
    $backups = @(Get-ChildItem -File -LiteralPath (Split-Path $database) -Filter 'state.pre-v9-*.db')
    Assert-Spike ($backups.Count -eq 1) 'Explicit project open did not preserve exactly one v8 backup.'
    $checkMigrationPath = Join-Path $work 'check_migration.py'
    $checkMigration = @'
import json, sqlite3, sys
from contextlib import closing
from pathlib import Path

def inspect(database):
    with closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        work = db.execute("SELECT * FROM work_items").fetchall()
        assert db.execute("SELECT objective, status FROM work_items").fetchall() == [
            ("Preserve this Work Item", "ACTIVE")
        ]
        legacy_session = db.execute(
            "SELECT session_id, state, provider_version, model, input_tokens, output_tokens "
            "FROM agent_sessions WHERE provider_version = '9.8.6'"
        ).fetchone()
        assert legacy_session[1:] == ("SUCCEEDED", "9.8.6", "fixture-model", 12, 1)
        legacy_events = db.execute(
            "SELECT * FROM agent_session_events WHERE session_id = ? ORDER BY sequence",
            (legacy_session[0],),
        ).fetchall()
        if version == 9:
            assert db.execute("SELECT context_projection_id FROM agent_sessions WHERE session_id = ?",
                              (legacy_session[0],)).fetchone() == (None,)
            states = db.execute(
                "SELECT a.session_id, a.state, p.projection_digest, a.provider, "
                "a.work_item_id, a.native_session_id, p.projection_id, p.canonical_payload "
                "FROM agent_sessions a "
                "JOIN context_projections p ON p.projection_id = a.context_projection_id "
                "ORDER BY a.created_at, a.session_id"
            ).fetchall()
            assert [(row[1], row[3]) for row in states] == [
                ("CANCELLED", "codex"), ("TIMED_OUT", "codex"), ("CANCELLED", "codex"),
                ("SUCCEEDED", "claude"), ("INTERRUPTED", "codex"),
            ], states
            source, target = states[2:4]
            assert source[0] != target[0]
            [(work_item_id,)] = db.execute("SELECT work_item_id FROM work_items").fetchall()
            assert all(row[4] == work_item_id for row in states)
            assert source[5] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            assert target[5] == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            for row in states:
                session_id, state, digest = row[:3]
                assert len(digest) == 64
                events = db.execute(
                    "SELECT kind, payload FROM agent_session_events WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
                assert any(kind == "OUTPUT" for kind, _ in events)
                terminal_kind = "SESSION_FINISHED" if state == "SUCCEEDED" else state
                assert any(kind == terminal_kind for kind, _ in events), (state, events)
                if session_id == target[0]:
                    assert any("Verified switch continuity from the source Handoff" in payload
                               for kind, payload in events if kind == "OUTPUT")
            handoffs = db.execute(
                "SELECT handoff_id, source_session_id, event_cutoff FROM session_handoffs"
            ).fetchall()
            assert {row[1] for row in handoffs} == {states[0][0], source[0]} and len(handoffs) == 2
            for _, session_id, event_cutoff in handoffs:
                cutoff = db.execute(
                    "SELECT MAX(sequence) FROM agent_session_events WHERE session_id = ?", (session_id,)
                ).fetchone()[0]
                assert event_cutoff == cutoff
            [source_handoff] = [row[0] for row in handoffs if row[1] == source[0]]
            target_projection = json.loads(target[7])
            assert target_projection["required_handoff_id"] == source_handoff
            assert target_projection["target_provider"] == "claude"
            assert "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" not in target[7]
            assert db.execute(
                "SELECT kind, authority FROM context_projection_sources "
                "WHERE projection_id = ? AND identity = ?",
                (target[6], source_handoff),
            ).fetchall() == [("SESSION_HANDOFF", "DERIVED")]

            for table in ("context_projections", "context_projection_sources", "session_handoffs"):
                captures = db.execute(f"SELECT * FROM {table}").fetchall()
                assert captures and "v8-fixture-hidden-value" not in repr(captures)
        return version, work, legacy_session, legacy_events

current, backup = map(inspect, sys.argv[1:])
assert current[0] == 9 and backup[0] == 8
assert current[1:] == backup[1:]
'@
    [IO.File]::WriteAllText($checkMigrationPath, $checkMigration)
    Invoke-PrivatePython $paths.Install @($checkMigrationPath, $database, $backups[0].FullName)

    $project = Join-Path $root 'adaptive-project'
    $null = Invoke-PrivatePython $paths.Install @('-m', 'corinthos', 'init', $project)
    Invoke-PrivatePython $paths.Install @('-c', 'import sys; from corinthos.contracts import ContractStore; ContractStore(sys.argv[1])', $project)
    $codexBefore = (Get-FileHash -LiteralPath $configuredLog).Hash
    $claudeBefore = (Get-FileHash -LiteralPath $claudeLog).Hash
    Invoke-DesktopFixture 'adaptive-passive'
    Assert-Spike (((Get-FileHash -LiteralPath $configuredLog).Hash -eq $codexBefore) -and
        ((Get-FileHash -LiteralPath $claudeLog).Hash -eq $claudeBefore) -and ![IO.File]::Exists($autoLog)) 'Reading adaptive gates and report started a native probe.'
    Invoke-DesktopFixture 'adaptive'
    Assert-Spike (@([IO.File]::ReadAllLines($configuredLog) | Where-Object { $_ -eq 'adaptive-launch' }).Count -eq 1 -and
        ((Get-FileHash -LiteralPath $claudeLog).Hash -eq $claudeBefore) -and ![IO.File]::Exists($autoLog)) 'Adaptive start did not launch exactly the selected TASK provider.'
    $assertAdaptive = @'
import sqlite3, sys
from contextlib import closing
from pathlib import Path

with closing(sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True)) as db:
    assert db.execute("PRAGMA user_version").fetchone() == (9,)
    assert db.execute("SELECT COUNT(*) FROM contracts WHERE contract_type = 'WORKFLOW'").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM wf_executions").fetchone() == (0,)
    assert db.execute(
        "SELECT w.scope_level, w.status, a.provider, a.state, a.cleanup_unconfirmed, LENGTH(p.projection_digest) "
        "FROM agent_sessions a JOIN work_items w USING(work_item_id) "
        "JOIN context_projections p ON p.projection_id = a.context_projection_id"
    ).fetchall() == [("TASK", "OPEN", "codex", "SUCCEEDED", 0, 64)]
    assert db.execute("SELECT COUNT(*) FROM agent_sessions").fetchone() == (1,)
'@
    $adaptiveCheck = Join-Path $work 'check_adaptive.py'
    [IO.File]::WriteAllText($adaptiveCheck, $assertAdaptive)
    Invoke-PrivatePython $paths.Install @($adaptiveCheck, (Join-Path $project '.corinthos\state.db'))
    Write-Host 'Actual WebView displayed passive PROJECT planning and TASK scope/report without probing. Explicit TASK execution reused Context and Agent Sessions and produced a report without publishing a Workflow.'

    $project = Join-Path $root 'external-project'
    foreach ($name in @('src', 'tests')) {
        [void] [IO.Directory]::CreateDirectory((Join-Path $project $name))
    }
    $files = @{
        'README.md' = '# Payments'
        'pyproject.toml' = "[project]`nname = `"payment-fixture`"`nversion = `"0.1.0`""
        'src/payments.py' = 'MARKER = "PHASE7_PAYMENTS_REPOSITORY_MARKER"'
        'tests/test_payments.py' = 'def test_payments(): assert True'
        'src/calendar.py' = 'MARKER = "PHASE7_UNRELATED_CALENDAR_MARKER"'
    }
    foreach ($entry in $files.GetEnumerator()) {
        [IO.File]::WriteAllText((Join-Path $project $entry.Key), $entry.Value)
    }
    $null = Invoke-FixtureGit @('init', '--initial-branch=main')
    $null = Invoke-FixtureGit @('add', '--', '.')
    $null = Invoke-FixtureGit @('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-m', 'External fixture baseline')
    [IO.File]::AppendAllText((Join-Path $project 'README.md'), "`nReview the payments service.")
    $private = Join-Path $project '.corinthos'
    $index = Join-Path $project '.git\index'
    $config = Join-Path $project '.git\config'
    $headBefore = Invoke-FixtureGit @('rev-parse', 'HEAD')
    $gitBefore = (Invoke-FixtureGit @('status', '--porcelain=v1', '--untracked-files=all')) -join "`n"
    $indexBefore = (Get-FileHash -LiteralPath $index).Hash
    $configBefore = (Get-FileHash -LiteralPath $config).Hash
    $fileHashes = @{}
    foreach ($name in $files.Keys) {
        $fileHashes[$name] = (Get-FileHash -LiteralPath (Join-Path $project $name)).Hash
    }
    $registryBefore = (Get-FileHash -LiteralPath $recent).Hash
    $codexBefore = (Get-FileHash -LiteralPath $configuredLog).Hash
    $claudeBefore = (Get-FileHash -LiteralPath $claudeLog).Hash
    Assert-Spike ($gitBefore -ceq ' M README.md' -and ![IO.Directory]::Exists($private)) 'Unexpected external fixture baseline.'

    Invoke-DesktopFixture 'import-preview'
    Assert-Spike (![IO.Directory]::Exists($private) -and
        ((Invoke-FixtureGit @('rev-parse', 'HEAD')) -ceq $headBefore) -and
        ((Get-FileHash -LiteralPath $index).Hash -eq $indexBefore) -and
        ((Get-FileHash -LiteralPath $config).Hash -eq $configBefore) -and
        (((Invoke-FixtureGit @('status', '--porcelain=v1', '--untracked-files=all')) -join "`n") -ceq $gitBefore) -and
        ((Get-FileHash -LiteralPath $recent).Hash -eq $registryBefore)) 'Read-only import preview changed the external project or its registry.'
    foreach ($name in $files.Keys) {
        Assert-Spike ((Get-FileHash -LiteralPath (Join-Path $project $name)).Hash -eq $fileHashes[$name]) 'Import preview changed tracked bytes.'
    }

    Invoke-DesktopFixture 'import-commit'
    Assert-Spike ([IO.Directory]::Exists($private) -and
        ((Invoke-FixtureGit @('rev-parse', 'HEAD')) -ceq $headBefore) -and
        ((Get-FileHash -LiteralPath $index).Hash -eq $indexBefore) -and
        ((Get-FileHash -LiteralPath $config).Hash -eq $configBefore) -and
        (((Invoke-FixtureGit @('status', '--porcelain=v1', '--untracked-files=no')) -join "`n") -ceq $gitBefore)) 'Import commit changed tracked Git state.'
    $untracked = @(Invoke-FixtureGit @('ls-files', '--others', '--exclude-standard'))
    Assert-Spike ($untracked.Count -gt 0 -and @($untracked | Where-Object { $_ -notlike '.corinthos/*' }).Count -eq 0) 'Import commit wrote outside private state.'
    foreach ($name in $files.Keys) {
        Assert-Spike ((Get-FileHash -LiteralPath (Join-Path $project $name)).Hash -eq $fileHashes[$name]) 'Import commit changed tracked bytes.'
    }
    Assert-Spike (((Get-FileHash -LiteralPath $configuredLog).Hash -eq $codexBefore) -and
        ((Get-FileHash -LiteralPath $claudeLog).Hash -eq $claudeBefore) -and ![IO.File]::Exists($autoLog)) 'Import inspection or confirmation probed a native provider.'

    Invoke-DesktopFixture 'import-execute'
    Assert-Spike (@([IO.File]::ReadAllLines($configuredLog) | Where-Object { $_ -eq 'import-change-launch' }).Count -eq 1 -and
        ((Get-FileHash -LiteralPath $claudeLog).Hash -eq $claudeBefore) -and ![IO.File]::Exists($autoLog)) 'Imported CHANGE did not reach exactly the selected provider with relevant repository context.'
    $assertImport = @'
import sqlite3, sys
from contextlib import closing
from pathlib import Path

with closing(sqlite3.connect(Path(sys.argv[1]).as_uri() + "?mode=ro", uri=True)) as db:
    assert db.execute("PRAGMA user_version").fetchone() == (9,)
    assert db.execute("SELECT COUNT(*) FROM contracts").fetchone() == (0,)
    assert db.execute("SELECT COUNT(*) FROM wf_executions").fetchone() == (0,)
    assert db.execute(
        "SELECT w.scope_level, w.status, a.provider, a.state, a.cleanup_unconfirmed, LENGTH(p.projection_digest) "
        "FROM agent_sessions a JOIN work_items w USING(work_item_id,project_id) "
        "JOIN context_projections p ON p.projection_id = a.context_projection_id "
        "AND p.work_item_id = w.work_item_id AND p.project_id = w.project_id"
    ).fetchall() == [("CHANGE", "OPEN", "codex", "SUCCEEDED", 0, 64)]
    assert db.execute("SELECT COUNT(*) FROM agent_sessions").fetchone() == (1,)
'@
    $importCheck = Join-Path $work 'check_import.py'
    [IO.File]::WriteAllText($importCheck, $assertImport)
    Invoke-PrivatePython $paths.Install @($importCheck, (Join-Path $private 'state.db'))
    Write-Host 'Actual WebView previewed and confirmed a dirty external Git repository, preserved tracked bytes and Git state, then executed a CHANGE with imported payments context and reaped all fixture processes.'

    $prior = [IO.File]::ReadAllLines($configuredLog).Length
    $null = Invoke-PrivatePython $paths.Install @('-m','corinthos','adapters','refresh')
    Assert-Spike ([IO.File]::Exists($configuredLog) -and ![IO.File]::Exists($autoLog)) 'Explicit refresh did not select the configured provider.'
    $probeList = ([IO.File]::ReadAllLines($configuredLog) | Select-Object -Skip $prior) -join '|'
    Assert-Spike ($probeList -ceq '--version|--help|exec --help|exec resume --help') 'Unexpected native probe sequence.'
    Write-Host 'Private CPython 3.14.10 lifecycle, passive provider sentinels and explicit refresh passed.'
} finally {
    if ($desktopProcess) {
        if (!$desktopProcess.HasExited) {
            [void] $desktopProcess.CloseMainWindow()
            if (!$desktopProcess.WaitForExit(5000)) { Stop-Process -Id $desktopProcess.Id -ErrorAction SilentlyContinue }
        }
        $desktopProcess.Dispose()
    }
    $env:LOCALAPPDATA = $originalLocal
    $env:PATH = $originalPath
    $env:CORINTHOS_SIDECAR_PYTHON = $originalOverride
    $env:CORINTHOS_SIDECAR_TEST_PROJECT = $originalProject
    $env:CORINTHOS_DESKTOP_SMOKE_PROJECT = $originalSmokeProject
    $env:CORINTHOS_DESKTOP_SMOKE_RECEIPT = $originalSmokeReceipt
    $env:CORINTHOS_DESKTOP_SMOKE_START = $originalSmokeStart
    if ([IO.Directory]::Exists($root)) { Remove-SetupTree $root $parent }
}
