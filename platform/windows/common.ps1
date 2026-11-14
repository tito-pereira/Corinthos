# SPDX-License-Identifier: MPL-2.0

function Get-CorinthosPaths {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT -or
        $env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or $PSVersionTable.PSVersion -lt [version]'5.1') {
        throw 'Use 64-bit PowerShell 5.1 or newer on x64 Windows.'
    }
    if ($env:LOCALAPPDATA -notmatch '^[A-Za-z]:[\\/][^\\/]' -or
        $env:LOCALAPPDATA -match '[;%"]') {
        throw 'LOCALAPPDATA must be an absolute local directory suitable for PATH.'
    }
    $local = [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\', '/')
    $programs = Join-Path $local 'Programs'
    $install = Join-Path $programs 'Corinthos'
    $state = Join-Path $local 'Corinthos'
    $paths = @{
        Programs = $programs
        Install = $install
        Bin = Join-Path $install 'bin'
        Previous = Join-Path $programs '.Corinthos-previous'
        State = $state
        Cache = Join-Path $state 'cache\install'
    }
    foreach ($path in $paths.Values) {
        Assert-PlainPath $path
        if ([IO.File]::Exists($path)) { throw "A file occupies the Corinthos directory $path." }
    }
    return $paths
}

function Assert-PlainPath([string] $Path) {
    for ($cursor = [IO.Path]::GetFullPath($Path); $cursor; $cursor = [IO.Path]::GetDirectoryName($cursor)) {
        try {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Corinthos refuses redirected paths. Check $cursor."
            }
        } catch [System.Management.Automation.ItemNotFoundException] {
            continue
        }
    }
}

function Enter-CorinthosSetup {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex = [Threading.Mutex]::new($false, "Global\Corinthos.Setup.$sid")
    try {
        if (!$mutex.WaitOne(0)) { throw 'Another Corinthos setup is running for this user.' }
    } catch [Threading.AbandonedMutexException] {
        # WaitOne transfers ownership when it reports an abandoned setup.
    } catch {
        $mutex.Dispose()
        throw
    }
    return $mutex
}

function Read-InstallManifest([string] $Directory) {
    $marker = Join-Path $Directory 'installation.json'
    Assert-PlainPath $marker
    if (![IO.File]::Exists($marker) -or [IO.FileInfo]::new($marker).Length -gt 256KB) {
        throw "The directory has no Corinthos installation marker. Check $Directory."
    }
    $value = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($value.owner -cne 'corinthos-core' -or $value.schema -ne 1 -or
        $value.state -cnotin @('staged', 'installed') -or $value.entries -isnot [array]) {
        throw "The directory has an unrecognized Corinthos installation marker. Check $Directory."
    }
    return $value
}

function Get-SetupInventory([string] $Path, [switch] $ProtectProjects) {
    $target = [IO.Path]::GetFullPath($Path)
    Assert-PlainPath $target
    $remaining = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
    $remaining.Push([IO.DirectoryInfo]::new($target))
    while ($remaining.Count) {
        foreach ($item in $remaining.Pop().EnumerateFileSystemInfos()) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to remove a redirected installation entry. Check $($item.FullName)."
            }
            if ($ProtectProjects -and $item.Name -in @('.corinthos', '.nukytera', '.agentforge')) {
                throw "A project exists inside the installation. Move the project to a separate directory before setup. Check $($item.FullName)."
            }
            $relative = $item.FullName.Substring($target.Length + 1)
            if ($item -is [IO.DirectoryInfo]) {
                $remaining.Push($item)
                "directory:$relative"
            } else { "file:$relative" }
        }
    }
}

function Assert-InstalledInventory([string] $Directory, $Manifest) {
    $actual = @(Get-SetupInventory $Directory -ProtectProjects)
    $extras = @(Compare-Object -ReferenceObject $Manifest.entries -DifferenceObject $actual |
        Where-Object SideIndicator -eq '=>')
    if ($extras.Count) {
        throw "The installed files include entries outside their Corinthos inventory. Check $Directory and move unrelated files out before retrying."
    }
}

function Remove-SetupTree([string] $Path, [string] $Parent) {
    $target = [IO.Path]::GetFullPath($Path)
    if ([IO.Path]::GetDirectoryName($target) -ne $Parent) {
        throw 'Refusing to remove a directory outside the Corinthos setup parent.'
    }
    Assert-PlainPath $target
    if (![IO.Directory]::Exists($target)) { return }
    [void] (Get-SetupInventory $target -ProtectProjects)
    Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction Stop
}

function Move-ToRetiredSetup([string] $Directory, [string] $Parent) {
    $manifest = Read-InstallManifest $Directory
    Assert-InstalledInventory $Directory $manifest
    $retired = Join-Path $Parent ('.Corinthos-remove-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::Move($Directory, $retired)
    return $retired
}

function Clear-RetiredSetup([string] $Directory, [string] $Parent) {
    try {
        Assert-InstalledInventory $Directory (Read-InstallManifest $Directory)
        Remove-SetupTree $Directory $Parent
    }
    catch { Write-Warning "Close Corinthos sessions and inspect the retired installation at $Directory." }
}

function Get-UserPath([Microsoft.Win32.RegistryKey] $Key) {
    $exists = $Key.GetValueNames() -contains 'Path'
    $kind = if ($exists) { $Key.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
    if ($kind -notin @([Microsoft.Win32.RegistryValueKind]::String, [Microsoft.Win32.RegistryValueKind]::ExpandString)) {
        throw 'The current user PATH has an unsupported registry type.'
    }
    return @{
        Exists = $exists
        Kind = $kind
        Text = [string] $Key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    }
}

function Test-OwnedPathEntry([string] $Entry, [string] $Bin, $Kind) {
    $entryPath = $Entry.Trim().Trim('"')
    if ($Kind -eq [Microsoft.Win32.RegistryValueKind]::ExpandString) {
        $entryPath = [Environment]::ExpandEnvironmentVariables($entryPath)
    }
    if ($entryPath -notmatch '^[A-Za-z]:[\\/]') { return $false }
    try {
        return [IO.Path]::GetFullPath($entryPath).TrimEnd('\', '/') -eq $Bin
    } catch [ArgumentException] {
        return $false
    } catch [NotSupportedException] {
        return $false
    } catch [IO.PathTooLongException] {
        return $false
    }
}

function Set-OwnedUserPath([Microsoft.Win32.RegistryKey] $Key, $Original, [string] $Bin, [switch] $Remove) {
    $entries = if ($Original.Text) { $Original.Text.Split(';') }
    $retained = @($entries | Where-Object { !(Test-OwnedPathEntry $_ $Bin $Original.Kind) })
    if (!$Remove) { $retained = @($Bin) + $retained }
    $text = $retained -join ';'
    if ($text.Length -ge 32767) { throw 'The new current user PATH is too long.' }
    if ($text -ceq $Original.Text) { return }
    if ($text.Length) { $Key.SetValue('Path', $text, $Original.Kind) }
    else { $Key.DeleteValue('Path', $false) }
}

function Restore-UserPath([Microsoft.Win32.RegistryKey] $Key, $Original) {
    if ($Original.Exists) { $Key.SetValue('Path', $Original.Text, $Original.Kind) }
    else { $Key.DeleteValue('Path', $false) }
}

function Get-SetupPathJournal($Paths) {
    return (Join-Path $Paths.Programs '.Corinthos-path-journal.json')
}

function Write-SetupPathJournal($Paths, $Original) {
    $journal = Get-SetupPathJournal $Paths
    Assert-PlainPath $journal
    $temp = Join-Path $Paths.Programs ('.Corinthos-path-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temp, ([ordered] @{
            schema = 1
            exists = [bool] $Original.Exists
            kind = $Original.Kind.ToString()
            text = $Original.Text
        } | ConvertTo-Json -Compress))
        [IO.File]::Move($temp, $journal)
    } finally { [IO.File]::Delete($temp) }
}

function Clear-SetupPathJournal($Paths) {
    $journal = Get-SetupPathJournal $Paths
    Assert-PlainPath $journal
    [IO.File]::Delete($journal)
}

function Restore-SetupPathJournal($Paths, [Microsoft.Win32.RegistryKey] $Key) {
    $journal = Get-SetupPathJournal $Paths
    Assert-PlainPath $journal
    if (![IO.File]::Exists($journal)) { return $false }
    if ([IO.FileInfo]::new($journal).Length -gt 256KB) { throw 'The setup PATH journal is oversized.' }
    $original = Get-Content -LiteralPath $journal -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($original.schema -ne 1 -or $original.exists -isnot [bool] -or $original.text -isnot [string] -or
        $original.kind -cnotin @('String', 'ExpandString') -or $original.text.Length -ge 32767 -or
        (!$original.exists -and $original.text)) {
        throw 'The setup PATH journal is invalid.'
    }
    $kind = [Microsoft.Win32.RegistryValueKind] $original.kind
    $current = Get-UserPath $Key
    $oldUnowned = @($original.text.Split(';') | Where-Object { $_ -and !(Test-OwnedPathEntry $_ $Paths.Bin $kind) })
    $newUnowned = @($current.Text.Split(';') | Where-Object { $_ -and !(Test-OwnedPathEntry $_ $Paths.Bin $current.Kind) })
    if (($oldUnowned -join ';') -ceq ($newUnowned -join ';') -and $current.Kind -eq $kind) {
        Restore-UserPath $Key @{ Exists = $original.exists; Kind = $kind; Text = $original.text }
    } elseif (!@($original.text.Split(';') | Where-Object { Test-OwnedPathEntry $_ $Paths.Bin $kind }).Count) {
        Set-OwnedUserPath $Key $current $Paths.Bin -Remove
    }
    Clear-SetupPathJournal $Paths
    Send-UserPathChange
    return $true
}

function Clear-AbandonedInstallStages($Paths) {
    if (![IO.Directory]::Exists($Paths.Programs)) { return }
    foreach ($entry in Get-ChildItem -LiteralPath $Paths.Programs -Directory -Force -Filter '.Corinthos-stage-*') {
        if ($entry.Name -cnotmatch '^\.Corinthos-stage-[0-9a-f]{32}$' -or
            ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) { continue }
        $marker = Join-Path $entry.FullName 'corinthos-stage.marker'
        Assert-PlainPath $marker
        if ([IO.File]::Exists($marker) -and [IO.FileInfo]::new($marker).Length -eq 23 -and
            [IO.File]::ReadAllText($marker) -ceq 'corinthos-core-stage-v1') {
            Remove-SetupTree $entry.FullName $Paths.Programs
        }
    }
}

function Send-UserPathChange {
    try {
        if (!('CorinthosSetup.EnvironmentChange' -as [type])) {
            Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace CorinthosSetup {
    public static class EnvironmentChange {
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern IntPtr SendMessageTimeout(IntPtr window, uint message,
            UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);
        public static void Notify() {
            UIntPtr result;
            SendMessageTimeout(new IntPtr(0xffff), 0x001a, UIntPtr.Zero,
                "Environment", 2, 500, out result);
        }
    }
}
'@
        }
        [CorinthosSetup.EnvironmentChange]::Notify()
    } catch {
        Write-Warning 'The PATH is saved. Sign out and back in if a new terminal still uses the old PATH.'
    }
}

function Restore-PreviousInstall($Paths, [Microsoft.Win32.RegistryKey] $Key) {
    $previousExists = [IO.Directory]::Exists($Paths.Previous)
    if ($previousExists) {
        $previous = Read-InstallManifest $Paths.Previous
        if ($previous.state -ne 'installed') { throw 'The saved previous installation has an unexpected state.' }
        Assert-InstalledInventory $Paths.Previous $previous
    }
    $retired = $null
    if ([IO.Directory]::Exists($Paths.Install)) {
        $active = Read-InstallManifest $Paths.Install
        Assert-InstalledInventory $Paths.Install $active
        if ($active.state -eq 'installed') {
            Clear-SetupPathJournal $Paths
            if ($previousExists) {
                $retired = Move-ToRetiredSetup $Paths.Previous $Paths.Programs
                Clear-RetiredSetup $retired $Paths.Programs
            }
            return
        }
        $retired = Move-ToRetiredSetup $Paths.Install $Paths.Programs
        if (!(Restore-SetupPathJournal $Paths $Key) -and !$previousExists) {
            Set-OwnedUserPath $Key (Get-UserPath $Key) $Paths.Bin -Remove
            Send-UserPathChange
        }
    } else {
        [void] (Restore-SetupPathJournal $Paths $Key)
    }
    if ($previousExists) { [IO.Directory]::Move($Paths.Previous, $Paths.Install) }
    if ($retired) { Clear-RetiredSetup $retired $Paths.Programs }
}
