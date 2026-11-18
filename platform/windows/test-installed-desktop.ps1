# SPDX-License-Identifier: MPL-2.0
# Requires the disposable Windows installer smoke, which owns the user scope.

function Wait-ProductObservation([scriptblock] $Check, [string] $Description, [int] $Seconds = 25) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $Seconds) {
        $observed = & $Check
        if ($observed) { return $observed }
        Start-Sleep -Milliseconds 200
    }
    throw "Installed desktop did not show $Description."
}

function Find-ProductControl([string] $Name, $Kind = $null, [switch] $Enabled) {
    $process = Get-Process -Id $script:ProductDesktopId -ErrorAction SilentlyContinue
    if (!$process -or !$process.MainWindowHandle) { return }
    try {
        $window = [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)
        $condition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty, $Name)
        $controls = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        foreach ($control in $controls) {
            if ((!$Kind -or $control.Current.ControlType -eq $Kind) -and
                (!$Enabled -or $control.Current.IsEnabled)) { return $control }
        }
    } catch [System.Windows.Automation.ElementNotAvailableException] { }
}

function Show-ProductControl($Control) {
    if ($Control.Current.IsOffscreen) {
        $scroll = $null
        if ($Control.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref] $scroll)) {
            $scroll.ScrollIntoView()
        } else {
            $Control.SetFocus()
        }
    }
    $null = Wait-ProductObservation {
        if ($Control.Current.IsEnabled -and !$Control.Current.IsOffscreen) { $Control }
    } 'the visible, enabled selected control'
}

function Click-ProductButton([string] $Name) {
    $control = Wait-ProductObservation {
        Find-ProductControl $Name ([System.Windows.Automation.ControlType]::Button) -Enabled
    } "an enabled $Name button"
    Show-ProductControl $control
    $invoke = $null
    if ($control.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref] $invoke)) {
        $invoke.Invoke()
        return
    }
    $toggle = $null
    if ($control.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref] $toggle)) {
        $toggle.Toggle()
        return
    }
    $control.SetFocus()
    Assert-Smoke ($control.Current.HasKeyboardFocus) "Could not focus the installed button '$Name'."
    [System.Windows.Forms.SendKeys]::SendWait(' ')
}

function Set-ProductText([string] $Name, [string] $Text) {
    $control = Wait-ProductObservation {
        Find-ProductControl $Name ([System.Windows.Automation.ControlType]::Edit) -Enabled
    } "an enabled $Name field"
    Show-ProductControl $control
    $value = $null
    if ($control.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref] $value)) {
        $value.SetValue($Text)
    } else {
        # Chromium can expose multiline edits through TextPattern only.
        Assert-Smoke ($Text -cmatch '^[A-Za-z0-9 .,-]+$') "Installed field '$Name' needs plain fixture text for keyboard input."
        $control.SetFocus()
        Assert-Smoke ($control.Current.HasKeyboardFocus) "Could not focus the installed field '$Name'."
        [System.Windows.Forms.SendKeys]::SendWait('^a' + $Text)
    }
}

function Set-ProductDiagnostics([bool] $Open) {
    $disclosure = Wait-ProductObservation {
        Find-ProductControl 'About / Diagnostics' ([System.Windows.Automation.ControlType]::Button) -Enabled
    } 'the diagnostics disclosure'
    Show-ProductControl $disclosure
    $expand = $null
    Assert-Smoke ($disclosure.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref] $expand)) 'The diagnostics disclosure has no expand pattern.'
    if ($Open) { $expand.Expand() } else { $expand.Collapse() }
}

function Get-ProductDiagnosticFacts([string] $PrivatePython) {
    $probe = @'
import json, platform
import corinthos
from corinthos.contracts import ContractStore
print(json.dumps({'application_version': corinthos.__version__,
    'private_runtime': platform.python_implementation() + ' ' + platform.python_version(),
    'schema': 'v' + str(ContractStore.current_schema_version())}))
'@
    $facts = (& $PrivatePython -I -B -X utf8 -c $probe) | ConvertFrom-Json
    Assert-Smoke ($LASTEXITCODE -eq 0) 'Could not read the actual candidate diagnostic facts.'
    $configuration = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot '..\..\apps\desktop\src-tauri\tauri.conf.json') | ConvertFrom-Json
    $facts | Add-Member -NotePropertyName desktop_version -NotePropertyValue $configuration.version
    $facts | Add-Member -NotePropertyName desktop_identifier -NotePropertyValue $configuration.identifier
    return $facts
}

function Assert-ProductDiagnostics([string] $State, [switch] $SelectedProject) {
    Assert-Smoke ($null -ne $script:ProductDiagnosticFacts) 'Read candidate diagnostic facts before observing the UI.'
    $facts = $script:ProductDiagnosticFacts
    $expected = @('Application version', 'Desktop version', 'RPC protocol', 'Backend state',
        'Private runtime', 'Supported storage schema', 'Selected project schema', 'Desktop identifier',
        $State, $facts.desktop_identifier, $facts.desktop_version)
    if ($State -eq 'Connected') { $expected += @($facts.application_version, 'v2', $facts.private_runtime, $facts.schema) }
    if ($SelectedProject -and $State -eq 'Disconnected') { $expected += "$($facts.schema) (last observed)" }
    $null = Wait-ProductObservation {
        $section = Find-ProductControl 'Diagnostics' ([System.Windows.Automation.ControlType]::Group)
        if (!$section) { return }
        $names = @($section.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name })
        if (@($expected | Where-Object { $_ -notin $names }).Count -eq 0) { $section }
    } "explicit $State diagnostics"
}

function Open-ProductManualPath {
    $form = Wait-ProductObservation {
        Find-ProductControl 'Open project' ([System.Windows.Automation.ControlType]::Group) -Enabled
    } 'the Open project form'
    $disclosure = Wait-ProductObservation {
        $condition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty, 'Enter path manually')
        foreach ($control in $form.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
            if ($control.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and
                $control.Current.IsEnabled) { return $control }
        }
    } 'the project manual-path disclosure'
    Show-ProductControl $disclosure
    $expand = $null
    Assert-Smoke ($disclosure.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref] $expand)) 'The project manual-path disclosure has no expand pattern.'
    if ($expand.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Collapsed) {
        $expand.Expand()
    }
    $field = Wait-ProductObservation {
        $condition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty, 'Project root')
        foreach ($control in $form.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)) {
            if ($control.Current.ControlType -eq [System.Windows.Automation.ControlType]::Edit -and
                $control.Current.IsEnabled) { return $control }
        }
    } 'the visible, enabled Project root field'
    Show-ProductControl $field
}

function Select-ProductCodex([string] $Name) {
    $control = Wait-ProductObservation {
        Find-ProductControl $Name ([System.Windows.Automation.ControlType]::ComboBox) -Enabled
    } "the $Name selector"
    Show-ProductControl $control
    $expand = $null
    Assert-Smoke ($control.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref] $expand)) "Installed selector '$Name' has no expand pattern."
    $expand.Expand()
    $option = Wait-ProductObservation {
        $condition = [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty, 'codex')
        $matches = $control.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        foreach ($match in $matches) {
            if ($match.Current.ControlType -in @(
                [System.Windows.Automation.ControlType]::ListItem, [System.Windows.Automation.ControlType]::MenuItem)) {
                return $match
            }
        }
        Find-ProductControl 'codex' ([System.Windows.Automation.ControlType]::ListItem) -Enabled
    } 'the Codex provider option'
    $selection = $null
    if ($option.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref] $selection)) {
        $selection.Select()
        if ($expand.Current.ExpandCollapseState -eq [System.Windows.Automation.ExpandCollapseState]::Expanded) {
            $expand.Collapse()
        }
    } else {
        $invoke = $null
        if ($option.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref] $invoke)) {
            $invoke.Invoke()
        } else {
            $control.SetFocus()
            Assert-Smoke ($control.Current.HasKeyboardFocus) "Could not focus the installed selector '$Name'."
            [System.Windows.Forms.SendKeys]::SendWait('{HOME}c{ENTER}')
        }
    }
}

function Find-ProductProcesses([string[]] $Executables) {
    $names = @($Executables | ForEach-Object { [IO.Path]::GetFileName($_) } | Sort-Object -Unique)
    $where = ($names | ForEach-Object { "Name = '$_'" }) -join ' OR '
    Get-CimInstance Win32_Process -Filter $where | Where-Object { $_.ExecutablePath -in $Executables }
}

function Open-ProductDesktop([string] $Desktop, [string] $PrivatePython) {
    $startup = [Diagnostics.Stopwatch]::StartNew()
    $launcher = (Get-Command corinthos.exe -CommandType Application -ErrorAction Stop).Source
    $parent = Start-Process -FilePath $launcher -PassThru
    try {
        Assert-Smoke ($parent.WaitForExit(10000) -and $parent.ExitCode -eq 0) 'The installed no-argument launcher did not open the desktop.'
    } finally { $parent.Dispose() }
    $owner = Wait-ProductObservation { Find-ProductProcesses @($Desktop) } 'the installed desktop process'
    Assert-Smoke (@($owner).Count -eq 1) 'Expected one process for the installed desktop.'
    $script:ProductDesktopId = [int] $owner.ProcessId
    $null = Wait-ProductObservation { Find-ProductControl 'Connected' } 'a connected private backend' 50
    $startup.Stop()
    $processCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, $script:ProductDesktopId)
    $windows = @([System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Children, $processCondition) | Where-Object {
        $_.Current.ControlType -eq [System.Windows.Automation.ControlType]::Window -and !$_.Current.IsOffscreen
    })
    Assert-Smoke ($windows.Count -eq 1 -and @(Find-ProductProcesses @($Desktop)).Count -eq 1) 'Expected exactly one visible installed desktop window and process.'
    $sidecar = @(Find-ProductProcesses @($PrivatePython))
    Assert-Smoke ($sidecar.Count -eq 1 -and $sidecar[0].ParentProcessId -eq $script:ProductDesktopId) 'Desktop did not own exactly one adjacent private Python sidecar.'
    $null = Wait-ProductObservation { Find-ProductControl 'Recent projects' } 'Home'
    Write-Host 'Installed desktop Home and its adjacent private backend are visible.'
    return $startup.ElapsedMilliseconds
}

function Close-ProductDesktop([string[]] $ChildExecutables) {
    $process = Get-Process -Id $script:ProductDesktopId
    try {
        # Hold the live handle so .NET can read the exit code after the process exits.
        $null = $process.Handle
        Assert-Smoke ($process.CloseMainWindow()) 'Could not request shutdown of the installed desktop.'
        Assert-Smoke ($process.WaitForExit(22000)) 'Installed desktop shutdown timed out.'
        Assert-Smoke ($process.ExitCode -eq 0) 'Installed desktop exited with a nonzero status.'
        Assert-Smoke (!(Find-ProductProcesses $ChildExecutables)) 'Installed desktop left a private sidecar or provider process.'
    } finally { $process.Dispose() }
    $script:ProductDesktopId = 0
}

function Invoke-InstalledDesktopSmoke([string] $Desktop, [string] $PrivatePython, [string] $Project, [string] $Provider, [string] $ProviderLog) {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
    $script:ProductDesktopId = 0
    $script:ProductDiagnosticFacts = Get-ProductDiagnosticFacts $PrivatePython
    $providerBefore = if ([IO.File]::Exists($ProviderLog)) { [IO.File]::ReadAllLines($ProviderLog).Length } else { 0 }
    $objective = 'Create the installed desktop fixture'
    $cliObjective = 'Create a second fixture through CLI'
    $projectName = [IO.Path]::GetFileName($Project)
    $children = @($PrivatePython, $Provider)
    try {
        $firstStartup = Open-ProductDesktop $Desktop $PrivatePython
        Click-ProductButton 'Command'
        $null = Wait-ProductObservation { Find-ProductControl 'Refresh Command' } 'Command'
        $null = Wait-ProductObservation { Find-ProductControl $projectName } 'the project previously recorded by CLI'
        Write-Host 'Installed Command observed a CLI project.'
        Assert-Smoke (![IO.File]::Exists($ProviderLog) -or [IO.File]::ReadAllLines($ProviderLog).Length -eq $providerBefore) 'Installed Home or Command executed a provider.'
        Click-ProductButton 'Projects'
        Click-ProductButton 'Open existing project'
        Open-ProductManualPath
        Set-ProductText 'Project root' $Project
        Click-ProductButton 'Open project'
        $null = Wait-ProductObservation { Find-ProductControl 'Project Summary' } 'the opened project'
        Click-ProductButton 'Settings'
        Set-ProductDiagnostics $true
        Assert-ProductDiagnostics 'Connected' -SelectedProject
        Set-ProductDiagnostics $false
        $scope = Wait-ProductObservation {
            Find-ProductControl 'Configure providers for the selected project' ([System.Windows.Automation.ControlType]::CheckBox) -Enabled
        } 'the project provider setting'
        $scope.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Toggle()
        Select-ProductCodex 'Provider'
        Set-ProductText 'Absolute executable path' $Provider
        Click-ProductButton 'Save provider'
        $null = Wait-ProductObservation { Find-ProductControl 'Provider selection saved.' } 'confirmation of the explicit provider setting'
        Write-Host 'Installed Settings saved the explicit provider choice.'
        $adapter = (& corinthos adapters show --root $Project --provider codex) -join [Environment]::NewLine
        Assert-Smoke ($LASTEXITCODE -eq 0 -and $adapter.Contains($Provider)) 'CLI did not observe the provider setting saved in the GUI.'
        $settingsDirectory = [IO.Path]::GetDirectoryName($Provider)
        $redirectedDirectory = Join-Path ([IO.Path]::GetDirectoryName($settingsDirectory)) 'redirected provider'
        try {
            $null = New-Item -ItemType Junction -Path $redirectedDirectory -Target $settingsDirectory
            $beforeRedirect = (Get-FileHash -LiteralPath $ProviderLog).Hash
            $null = & {
                $ErrorActionPreference = 'Continue'
                corinthos adapters configure codex --root $Project --executable (Join-Path $redirectedDirectory 'codex.exe') 2>&1
            }
            Assert-Smoke ($LASTEXITCODE -ne 0 -and (Get-FileHash -LiteralPath $ProviderLog).Hash -eq $beforeRedirect) 'Installed CLI accepted or launched a redirected provider executable.'
        } finally {
            if ([IO.Directory]::Exists($redirectedDirectory)) { [IO.Directory]::Delete($redirectedDirectory) }
        }
        Click-ProductButton 'Projects'
        Click-ProductButton 'Work'
        Set-ProductText 'Objective' $objective
        Set-ProductText 'Acceptance criteria, one per line' 'Observe the exact session and context through both surfaces.'
        Click-ProductButton 'Create Work Item'
        $null = Wait-ProductObservation { Find-ProductControl $objective } 'the Work Item created in the GUI'
        $listed = (& corinthos work list --root $Project) | ConvertFrom-Json
        Assert-Smoke ($LASTEXITCODE -eq 0) 'Could not list Work Items through the installed CLI.'
        $work = @($listed.items | Where-Object { $_.objective -ceq $objective })
        Assert-Smoke ($work.Count -eq 1 -and $work[0].scope_level -ceq 'TASK') 'Installed CLI did not observe the exact TASK created in the GUI.'
        $cliWork = (& corinthos work create --root $Project --scope TASK --objective $cliObjective --criterion 'Inspect the installed Work Item tree.') | ConvertFrom-Json
        Assert-Smoke ($LASTEXITCODE -eq 0 -and $cliWork.work_item_id) 'Could not create the second Work Item through the installed CLI.'
        Click-ProductButton 'Refresh Work Items'
        $null = Wait-ProductObservation { Find-ProductControl $cliObjective } 'the Work Item created in the CLI'
        Click-ProductButton $objective
        $sidecar = @(Find-ProductProcesses @($PrivatePython))
        Assert-Smoke ($sidecar.Count -eq 1 -and $sidecar[0].ParentProcessId -eq $script:ProductDesktopId) 'Expected one owned sidecar before the reconnect test.'
        Stop-Process -Id $sidecar[0].ProcessId
        $null = Wait-ProductObservation { Find-ProductControl 'Disconnected' } 'a disconnected state after sidecar exit'
        Set-ProductDiagnostics $true
        Assert-ProductDiagnostics 'Disconnected' -SelectedProject
        Set-ProductDiagnostics $false
        Click-ProductButton 'Reconnect'
        $null = Wait-ProductObservation { Find-ProductControl 'Connected' } 'a new private backend handshake'
        $replacement = @(Find-ProductProcesses @($PrivatePython))
        Assert-Smoke ($replacement.Count -eq 1 -and $replacement[0].ProcessId -ne $sidecar[0].ProcessId -and
            $replacement[0].ParentProcessId -eq $script:ProductDesktopId) 'Reconnect did not create one new owned private sidecar.'
        $null = Wait-ProductObservation { Find-ProductControl $objective } 'the retained Work Item after reconnect'
        $null = Wait-ProductObservation { Find-ProductControl $Project } 'the retained project root after reconnect'
        $null = Wait-ProductObservation { Find-ProductControl $work[0].work_item_id } 'the retained selected Work Item identifier after reconnect'
        $null = Wait-ProductObservation { Find-ProductControl 'Start adaptive workflow' ([System.Windows.Automation.ControlType]::Button) -Enabled } 'the retained selected Work Item view after reconnect'
        Write-Host 'Installed diagnostics remained available offline; reconnect restored the project, Work view and selected Work Item.'
        Select-ProductCodex 'Execution provider'
        Click-ProductButton 'Start adaptive workflow'
        $tree = @(Wait-ProductObservation {
            $running = @(Find-ProductProcesses @($Provider))
            if ($running.Count -eq 2) { $running }
        } 'the explicit provider and its child')
        Assert-Smoke (@($tree | Where-Object { $_.ParentProcessId -in $tree.ProcessId }).Count -eq 1) 'Expected one direct child of the explicitly configured provider.'
        $sessions = (& corinthos session list --root $Project) | ConvertFrom-Json
        Assert-Smoke ($LASTEXITCODE -eq 0 -and @($sessions.sessions).Count -eq 1) 'Installed CLI did not observe the exact GUI-launched Session.'
        $session = $sessions.sessions[0]
        Assert-Smoke ($session.work_item_id -ceq $work[0].work_item_id -and $session.state -ceq 'RUNNING' -and $session.provider -ceq 'codex') 'Installed Session did not retain its GUI Work Item or provider.'
        $binding = (& corinthos context for-session $session.session_id --root $Project) | ConvertFrom-Json
        $projection = $binding.projection
        Assert-Smoke ($LASTEXITCODE -eq 0 -and $projection.work_item_id -ceq $work[0].work_item_id -and
            $projection.target_provider -ceq 'codex' -and $projection.projection_digest -match '^[a-f0-9]{64}$') 'Installed CLI and Session disagreed on the exact Context Projection.'
        $events = Wait-ProductObservation {
            $transcript = (& corinthos session events $session.session_id --root $Project) -join [Environment]::NewLine
            if ($LASTEXITCODE -eq 0 -and $transcript.Contains('Fixture provider claimed VERIFIED.')) { $transcript }
        } 'the fixture provider observation'
        Assert-Smoke (!$events.Contains('installed-fixture-hidden-value')) 'Installed Session event leaked the fixture secret.'
        $verifyFlow = (& corinthos flow status --work $work[0].work_item_id --root $Project) | ConvertFrom-Json
        Assert-Smoke ($LASTEXITCODE -eq 0 -and $verifyFlow.latest_session.session_id -ceq $session.session_id -and
            $verifyFlow.validation_state -ceq 'NOT_VERIFIED' -and !$verifyFlow.can_complete) 'Provider text manufactured independent Validation.'
        Click-ProductButton 'Refresh workflow'
        $null = Wait-ProductObservation { Find-ProductControl $session.session_id } 'the exact active Session'
        $null = Wait-ProductObservation { Find-ProductControl 'NOT_VERIFIED' } 'the independent Validation state'
        Click-ProductButton 'Open Context Projection'
        $null = Wait-ProductObservation { Find-ProductControl $projection.projection_digest } 'the same Context Projection digest'
        Close-ProductDesktop $children

        $null = Open-ProductDesktop $Desktop $PrivatePython
        Click-ProductButton 'Command'
        $null = Wait-ProductObservation { Find-ProductControl 'INTERRUPTED' } 'the interrupted Session state'
        Click-ProductButton 'Open Agent Session'
        $null = Wait-ProductObservation { Find-ProductControl $session.session_id } 'the persisted Session from Command'
        $restored = (& corinthos session show $session.session_id --root $Project) | ConvertFrom-Json
        Assert-Smoke ($LASTEXITCODE -eq 0 -and $restored.state -ceq 'INTERRUPTED' -and
            !$restored.cleanup_unconfirmed) 'Installed CLI did not preserve the cleanly interrupted Session.'
        $restoredContext = (& corinthos context for-session $session.session_id --root $Project) | ConvertFrom-Json
        Assert-Smoke ($LASTEXITCODE -eq 0 -and $restoredContext.projection.projection_id -ceq $projection.projection_id) 'Installed CLI did not preserve the interrupted Session Context.'
        Close-ProductDesktop $children
        Write-Host 'Installed Home and Command, GUI/CLI Work Item and provider settings, exact Session and Context, process cleanup and reopening passed.'
        return $firstStartup
    } finally {
        if ($script:ProductDesktopId) {
            $remaining = Get-Process -Id $script:ProductDesktopId -ErrorAction SilentlyContinue
            if ($remaining) {
                [void] $remaining.CloseMainWindow()
                if (!$remaining.WaitForExit(22000)) {
                    $remaining.Kill()
                    [void] $remaining.WaitForExit(5000)
                }
                $remaining.Dispose()
            }
        }
    }
}
