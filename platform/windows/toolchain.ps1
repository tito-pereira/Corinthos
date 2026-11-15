# SPDX-License-Identifier: MPL-2.0

$script:CorinthosDesktopTarget = 'x86_64-pc-windows-msvc'
$script:CorinthosVcComponent = 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64'
$script:CorinthosSdkComponent = 'Microsoft.VisualStudio.Component.Windows11SDK.26100'
$script:CorinthosWebViewKey = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'

function Invoke-SetupNative([string] $Executable, [string[]] $Arguments, [string] $Operation, [switch] $Quiet) {
    if (!$Quiet) {
        & $Executable @Arguments | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "$Operation failed (exit $LASTEXITCODE)." }
        return
    }
    $result = @(& $Executable @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) { throw "$Operation failed (exit $LASTEXITCODE)." }
    return $result
}

function Get-CorinthosSourceGit([string] $Source) {
    foreach ($command in Get-Command git.exe -CommandType Application -All -ErrorAction SilentlyContinue) {
        $path = [IO.Path]::GetFullPath($command.Source)
        if ($path -notmatch '^[a-zA-Z]:\\' -or
            $path.StartsWith("$Source\", [StringComparison]::OrdinalIgnoreCase)) { continue }
        Assert-PlainPath $path
        if ([IO.File]::Exists($path)) { return $path }
    }
    throw 'Git for Windows is required to verify and export the exact source checkout.'
}

function Get-CorinthosLineEndingOptions([string] $Git, [string] $Source) {
    $settings = [ordered]@{
        'core.autocrlf' = '^(true|false|input)$'
        'core.eol' = '^(lf|crlf|native)$'
    }
    foreach ($name in $settings.Keys) {
        # Read only scalar settings that can affect clone line endings. Includes
        # and repository commands must stay disabled in this preliminary query.
        $args = @('-C', $Source, 'config', '--no-includes')
        if ($name -eq 'core.autocrlf') { $args += '--type=bool-or-str' }
        $value = @(& $Git @args --get $name 2>$null)
        if ($LASTEXITCODE -eq 1 -and $value.Count -eq 0) { continue }
        if ($LASTEXITCODE -ne 0 -or $value.Count -ne 1 -or $value[0] -notmatch $settings[$name]) {
            throw 'The source checkout has an unsupported Git line-ending setting.'
        }
        '-c'
        "$name=$($value[0].ToLowerInvariant())"
    }
}

function Read-CorinthosSourceIdentity([string] $Git, [string] $Source, [string[]] $LineEndings = @()) {
    $root = Invoke-SetupNative $Git @('-C', $Source, 'rev-parse', '--show-toplevel') 'Git source root' -Quiet
    if (@($root).Count -ne 1 -or [IO.Path]::GetFullPath($root) -ne $Source) {
        throw 'Run the source installer from the root of its Git checkout.'
    }
    $commit = Invoke-SetupNative $Git @('-C', $Source, 'rev-parse', '--verify', 'HEAD^{commit}') 'Git commit' -Quiet
    $tree = Invoke-SetupNative $Git @('-C', $Source, 'rev-parse', '--verify', "$commit^{tree}") 'Git tree' -Quiet
    if ($commit -isnot [string] -or $tree -isnot [string]) { throw 'Git returned an invalid source identity.' }
    if ($commit -cnotmatch '^(?:[0-9a-f]{40}|[0-9a-f]{64})$' -or $tree -cnotmatch "^[0-9a-f]{$($commit.Length)}$") {
        throw 'Git could not verify the exact Corinthos source identity.'
    }
    $dirty = Invoke-SetupNative $Git (@(
        '-C', $Source, '-c', 'core.fsmonitor=false', '--no-optional-locks'
    ) + $LineEndings + @(
        'status', '--porcelain=v1', '--untracked-files=normal', '--ignore-submodules=none'
    )) 'Git source cleanliness' -Quiet
    if (@($dirty).Count) { throw 'Commit or remove local checkout changes before installing Corinthos.' }
    $entries = Invoke-SetupNative $Git @('-C', $Source, 'ls-tree', '-r', $tree) 'Git source modes' -Quiet
    if (!@($entries).Count -or @($entries | Where-Object { $_ -notmatch '^100(644|755) blob ' }).Count) {
        throw 'The release source must contain only regular tracked files.'
    }
    return @{ Commit = $commit; Tree = $tree }
}

function Invoke-CorinthosSourceRead([string] $Git, [string] $Source, [scriptblock] $Operation) {
    $environment = @{}
    foreach ($item in [Environment]::GetEnvironmentVariables('Process').GetEnumerator()) {
        if ($item.Key -ilike 'GIT_*') {
            $environment[$item.Key] = $item.Value
            [Environment]::SetEnvironmentVariable($item.Key, $null, 'Process')
        }
    }
    $guarded = @{
        GIT_CONFIG_NOSYSTEM = '1'; GIT_CONFIG_GLOBAL = 'NUL'; GIT_NO_REPLACE_OBJECTS = '1'
        GIT_ALLOW_PROTOCOL = ''; GIT_NO_LAZY_FETCH = '1'; GIT_TERMINAL_PROMPT = '0'
        GIT_OPTIONAL_LOCKS = '0'
    }
    try {
        $lineEndings = @(Get-CorinthosLineEndingOptions $Git $Source)
        foreach ($name in $guarded.Keys) {
            [Environment]::SetEnvironmentVariable($name, $guarded[$name], 'Process')
        }
        & $Operation $lineEndings
    } finally {
        foreach ($name in $guarded.Keys) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
        foreach ($name in $environment.Keys) { [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process') }
    }
}

function Copy-CorinthosSourceSnapshot([string] $Source, [string] $Work) {
    $git = Get-CorinthosSourceGit $Source
    return Invoke-CorinthosSourceRead $git $Source {
        param($lineEndings)
        $identity = Read-CorinthosSourceIdentity $git $Source $lineEndings
        $archive = Join-Path $Work 'release-source.zip'
        Invoke-SetupNative $git @(
            '-C', $Source, 'archive', '--format=zip', "--output=$archive", $identity.Commit
        ) 'Git source export'
        if (![IO.File]::Exists($archive) -or [IO.FileInfo]::new($archive).Length -gt 64MB) {
            throw 'The release source archive is missing or oversized.'
        }
        $snapshot = Join-Path $Work 'source'
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $snapshot)
        [void] (Get-SetupInventory $snapshot -ProtectProjects)
        if ((Read-CorinthosSourceIdentity $git $Source $lineEndings).Commit -cne $identity.Commit) {
            throw 'The source checkout changed during export.'
        }
        return @{ Git = $git; Identity = $identity; Directory = $snapshot }
    }
}

function Confirm-CorinthosSourceIdentity([string] $Git, [string] $Source) {
    return Invoke-CorinthosSourceRead $Git $Source {
        param($lineEndings)
        Read-CorinthosSourceIdentity $Git $Source $lineEndings
    }
}

function Read-CorinthosToolchainManifest([string] $Path) {
    Assert-PlainPath $Path
    if (![IO.File]::Exists($Path) -or [IO.FileInfo]::new($Path).Length -gt 16KB) {
        throw 'The Windows toolchain manifest is missing or oversized.'
    }
    $pin = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $pin -or @(Compare-Object @('schema','architecture','node','npm','cargo','rustc','rust-std','microsoft') @($pin.PSObject.Properties.Name) -CaseSensitive).Count -or
        $pin.schema -ne 1 -or $pin.architecture -cne $script:CorinthosDesktopTarget) {
        throw 'The Windows toolchain manifest has an unrecognized schema.'
    }
    $expected = @{
        node = @('24.22.2', 'nodejs.org', 'https://nodejs.org/dist/v24.22.2/node-v24.22.2-win-x64.zip')
        npm = @('11.19.1', 'registry.npmjs.org', 'https://registry.npmjs.org/npm/-/npm-11.19.1.tgz')
    }
    foreach ($name in @('cargo', 'rustc', 'rust-std')) {
        $expected[$name] = @('1.99.1', 'static.rust-lang.org',
            "https://static.rust-lang.org/dist/2026-10-15/$name-1.99.1-$script:CorinthosDesktopTarget.tar.xz")
    }
    foreach ($name in $expected.Keys) {
        $value = $pin.$name
        $version, $hostName, $url = $expected[$name]
        if ($null -eq $value -or @(Compare-Object @('version','filename','url','host','sha256','bytes') @($value.PSObject.Properties.Name) -CaseSensitive).Count -or
            $value.version -cne $version -or $value.host -cne $hostName -or $value.url -cne $url -or
            $value.filename -cne [IO.Path]::GetFileName(([uri]$url).AbsolutePath) -or
            $value.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            ($value.bytes -isnot [int] -and $value.bytes -isnot [long]) -or $value.bytes -lt 1 -or $value.bytes -gt 128MB) {
            throw 'The Windows toolchain manifest contains an invalid immutable pin.'
        }
    }
    $microsoft = $pin.microsoft
    if ($null -eq $microsoft -or @(Compare-Object @('build_tools','webview2','publisher') @($microsoft.PSObject.Properties.Name) -CaseSensitive).Count -or
        $microsoft.build_tools -cne 'https://aka.ms/vs/17/release/vs_buildtools.exe' -or
        $microsoft.webview2 -cne 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -or
        $microsoft.publisher -cne 'Microsoft Corporation') {
        throw 'The Windows toolchain manifest contains an invalid Microsoft prerequisite source.'
    }
    return $pin
}

function Expand-CorinthosToolchains($Manifest, [string] $Cache, [string] $Work) {
    Assert-PlainPath $Cache
    [void] [IO.Directory]::CreateDirectory($Cache)
    $scratch = Join-Path $Work 'toolchains'
    [void] [IO.Directory]::CreateDirectory($scratch)
    $tar = Join-Path ([Environment]::SystemDirectory) 'tar.exe'
    Assert-PlainPath $tar
    if (![IO.File]::Exists($tar)) { throw 'The Windows tar utility is required to unpack the verified toolchains.' }
    $pins = @{}
    foreach ($name in @('node', 'npm', 'rustc', 'cargo', 'rust-std')) {
        $pin = $Manifest.$name
        $pins[$name] = Get-PinnedArtifact $pin $pin.host $Cache $scratch 12 32 -MaxBytes 128MB
    }
    [IO.Compression.ZipFile]::ExtractToDirectory($pins.node, $scratch)
    $npm = Join-Path $scratch 'npm'
    [void] [IO.Directory]::CreateDirectory($npm)
    Invoke-SetupNative $tar @('-xf', $pins.npm, '-C', $npm) 'Verified npm extraction'
    $rust = Join-Path $scratch 'rust'
    [void] [IO.Directory]::CreateDirectory($rust)
    foreach ($name in @('rustc', 'cargo', 'rust-std')) {
        Invoke-SetupNative $tar @('-xf', $pins[$name], '-C', $scratch) 'Verified Rust component extraction'
        $componentName = if ($name -eq 'rust-std') { "rust-std-$script:CorinthosDesktopTarget" } else { $name }
        $component = Join-Path $scratch "$name-1.99.1-$script:CorinthosDesktopTarget\$componentName"
        $manifestPath = Join-Path $component 'manifest.in'
        Assert-PlainPath $manifestPath
        if (![IO.File]::Exists($manifestPath) -or [IO.FileInfo]::new($manifestPath).Length -gt 64KB) {
            throw 'A verified Rust component has an unsupported installation manifest.'
        }
        foreach ($entry in Get-Content -LiteralPath $manifestPath -Encoding UTF8) {
            if ($entry -cnotmatch '^(file|dir):([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)$') {
                throw 'A verified Rust component has an unsupported installation entry.'
            }
            $kind = $Matches[1]
            $relative = $Matches[2]
            if (@($relative.Split('/') | Where-Object { $_ -in @('.', '..') -or $_.EndsWith('.') }).Count) {
                throw 'A Rust component entry could escape its private toolchain.'
            }
            $from = Join-Path $component $relative
            $to = Join-Path $rust $relative
            Assert-PlainPath $from
            Assert-PlainPath $to
            [void] [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($to))
            if ($kind -eq 'dir') {
                [void] (Get-SetupInventory $from)
                Copy-Item -LiteralPath $from -Destination $to -Recurse -ErrorAction Stop
            } else { [IO.File]::Copy($from, $to) }
        }
    }
    $node = Join-Path $scratch 'node-v24.22.2-win-x64\node.exe'
    $npmCli = Join-Path $npm 'package\bin\npm-cli.js'
    $cargo = Join-Path $rust 'bin\cargo.exe'
    $rustc = Join-Path $rust 'bin\rustc.exe'
    foreach ($file in @($node, $npmCli, $cargo, $rustc)) {
        Assert-PlainPath $file
        if (![IO.File]::Exists($file)) { throw 'A verified private toolchain entry is missing.' }
    }
    return @{ Node = $node; Npm = $npmCli; Cargo = $cargo; Rustc = $rustc }
}

function Get-WindowsSdkBuild {
    $roots = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots')
    if (!$roots) { return }
    try { $sdk = [string] $roots.GetValue('KitsRoot10', '') }
    finally { $roots.Dispose() }
    if ($sdk -notmatch '^[A-Za-z]:\\' -or $sdk -match '[;%"]') { return }
    Assert-PlainPath $sdk
    $bin = Join-Path $sdk 'bin'
    if (![IO.Directory]::Exists($bin)) { return }
    $versions = Get-ChildItem -LiteralPath $bin -Directory | Where-Object Name -Match '^10\.0\.\d+\.\d+$' |
        Sort-Object -Property { [version] $_.Name } -Descending
    foreach ($version in $versions) {
        if ([version] $version.Name -lt [version]'10.0.22621.0') { continue }
        $sdkBin = Join-Path $version.FullName 'x64'
        $library = Join-Path $sdk "Lib\$($version.Name)"
        $include = Join-Path $sdk "Include\$($version.Name)"
        $required = @(
            (Join-Path $sdkBin 'rc.exe'), (Join-Path $sdkBin 'mt.exe'),
            (Join-Path $library 'um\x64\kernel32.lib'), (Join-Path $library 'ucrt\x64\ucrt.lib'),
            (Join-Path $include 'um\windows.h'), (Join-Path $include 'ucrt\stdlib.h')
        )
        if (@($required | Where-Object { ![IO.File]::Exists($_) }).Count) { continue }
        foreach ($path in $required) { Assert-PlainPath $path }
        return @{ Bin = $sdkBin; Lib = $library; Include = $include; Version = $version.Name }
    }
}

function Find-CorinthosMsvc {
    $installer = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Microsoft Visual Studio\Installer'
    $vswhere = Join-Path $installer 'vswhere.exe'
    Assert-PlainPath $vswhere
    if (![IO.File]::Exists($vswhere)) { return }
    $json = Invoke-SetupNative $vswhere @(
        '-products', '*', '-version', '[17.0,19.0)', '-requires', $script:CorinthosVcComponent,
        '-format', 'json', '-utf8'
    ) 'Visual Studio toolset discovery' -Quiet
    $instances = @(($json -join "`n") | ConvertFrom-Json | Sort-Object -Property { [version] $_.installationVersion } -Descending)
    foreach ($instance in $instances) {
        $root = [string] $instance.installationPath
        if ($root -notmatch '^[A-Za-z]:\\' -or $root -match '[;%"]') { continue }
        Assert-PlainPath $root
        $versionFile = Join-Path $root 'VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt'
        Assert-PlainPath $versionFile
        if (![IO.File]::Exists($versionFile) -or [IO.FileInfo]::new($versionFile).Length -gt 128) { continue }
        $version = [IO.File]::ReadAllText($versionFile).Trim()
        if ($version -notmatch '^14\.\d+\.\d+$') { continue }
        $tool = Join-Path $root "VC\Tools\MSVC\$version"
        $bin = Join-Path $tool 'bin\Hostx64\x64'
        $required = @(
            (Join-Path $bin 'cl.exe'), (Join-Path $bin 'link.exe'), (Join-Path $bin 'lib.exe'),
            (Join-Path $tool 'lib\x64\vcruntime.lib'), (Join-Path $tool 'include\vcruntime.h')
        )
        if (@($required | Where-Object { ![IO.File]::Exists($_) }).Count) { continue }
        foreach ($path in $required) { Assert-PlainPath $path }
        return @{
            Bin = $bin; Lib = Join-Path $tool 'lib\x64'; Include = Join-Path $tool 'include'
            Version = $version; Instance = $root; Product = [string] $instance.productId
        }
    }
}

function Get-CorinthosWebView2 {
    $locations = @(
        @([Microsoft.Win32.RegistryHive]::LocalMachine, [Microsoft.Win32.RegistryView]::Registry32),
        @([Microsoft.Win32.RegistryHive]::CurrentUser, [Microsoft.Win32.RegistryView]::Default)
    )
    $versions = foreach ($location in $locations) {
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey($location[0], $location[1])
        try {
            $key = $base.OpenSubKey($script:CorinthosWebViewKey)
            if ($key) {
                try {
                    $pv = [string] $key.GetValue('pv', '')
                    if ($pv -match '^\d+\.\d+\.\d+\.\d+$' -and [version]$pv -gt [version]'0.0.0.0') { [version] $pv }
                } finally { $key.Dispose() }
            }
        } finally { $base.Dispose() }
    }
    return $versions | Sort-Object -Descending | Select-Object -First 1
}

function Assert-MicrosoftSetupSignature([string] $Path, [string] $Publisher) {
    Assert-PlainPath $Path
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate -or
        $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -cne $Publisher -or
        $signature.SignerCertificate.Subject -cnotmatch '^CN=Microsoft Corporation, O=Microsoft Corporation, ') {
        throw 'Microsoft prerequisite authenticity verification failed.'
    }
    return $Publisher
}

function Receive-MicrosoftSetup([uri] $SourceUri, [string] $Work, [string] $Kind, [string] $Publisher) {
    $vs = $Kind -eq 'build_tools'
    $filename = if ($vs) { 'vs_BuildTools.exe' } else { 'MicrosoftEdgeWebview2Setup.exe' }
    $hostName = if ($vs) { 'download.visualstudio.microsoft.com' } else { 'msedge.sf.dl.delivery.mp.microsoft.com' }
    $uuid = '[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    $pathPattern = if ($vs) { "^/download/pr/$uuid/[0-9a-f]{64}/vs_buildtools\.exe$" }
        else { "^/filestreamingservice/files/$uuid/microsoftedgewebview2setup\.exe$" }
    $target = Join-Path $Work $filename
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $cancel = [Threading.CancellationTokenSource]::new([TimeSpan]::FromMinutes(2))
    try {
        $response = $client.GetAsync($SourceUri, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult()
        try {
            $redirect = $response.Headers.Location
            if ([int] $response.StatusCode -notin @(301, 302, 307, 308) -or !$redirect -or
                !$redirect.IsAbsoluteUri -or $redirect.Scheme -cne 'https' -or $redirect.Host -cne $hostName -or
                !$redirect.IsDefaultPort -or $redirect.UserInfo -or $redirect.Query -or $redirect.Fragment -or
                $redirect.AbsolutePath -notmatch $pathPattern) {
                throw 'The Microsoft prerequisite download redirected outside its verified distribution path.'
            }
        } finally { $response.Dispose() }
        $response = $client.GetAsync($redirect, [Net.Http.HttpCompletionOption]::ResponseHeadersRead, $cancel.Token).GetAwaiter().GetResult()
        try {
            $limit = 16MB
            $length = $response.Content.Headers.ContentLength
            if ([int] $response.StatusCode -ne 200 -or ($null -ne $length -and ($length -lt 1 -or $length -gt $limit))) {
                throw 'The official Microsoft prerequisite bootstrapper could not be downloaded.'
            }
            $stream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew)
            try {
                $buffer = [byte[]]::new(65536)
                $total = 0L
                while ($count = $stream.ReadAsync($buffer, 0, $buffer.Length, $cancel.Token).GetAwaiter().GetResult()) {
                    $total += $count
                    if ($total -gt $limit) { throw 'Microsoft prerequisite download exceeded the maximum size.' }
                    $output.Write($buffer, 0, $count)
                }
                if ($total -lt 1 -or ($null -ne $length -and $total -ne $length)) {
                    throw 'Microsoft prerequisite download is incomplete.'
                }
            } finally { $output.Dispose() }
        } finally { $response.Dispose() }
    } finally {
        $cancel.Dispose()
        $client.Dispose()
    }
    $signer = Assert-MicrosoftSetupSignature $target $Publisher
    return @{ Executable = $target; Signer = $signer }
}

function Get-CorinthosDesktopPrerequisites($Microsoft, [string] $Work) {
    $msvc = Find-CorinthosMsvc
    $sdk = Get-WindowsSdkBuild
    $msvcSigner = $null
    if (!$msvc -or !$sdk) {
        $download = Receive-MicrosoftSetup $Microsoft.build_tools $Work 'build_tools' $Microsoft.publisher
        $msvcSigner = $download.Signer
        $installPath = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Microsoft Visual Studio\2022\BuildTools'
        $args = @('--quiet', '--wait', '--norestart', '--nocache')
        if ($msvc -and $msvc.Product -eq 'Microsoft.VisualStudio.Product.BuildTools' -and $msvc.Instance -like '*\Microsoft Visual Studio\2022\BuildTools') {
            $args = @('modify', '--installPath', ('"' + $msvc.Instance + '"')) + $args
        } else {
            $args = @('--installPath', ('"' + $installPath + '"')) + $args
        }
        $args += @('--add', 'Microsoft.VisualStudio.Workload.VCTools',
            '--add', $script:CorinthosVcComponent, '--add', $script:CorinthosSdkComponent)
        try {
            $process = Start-Process -FilePath $download.Executable -ArgumentList $args -Verb RunAs -WindowStyle Hidden -PassThru -Wait
        } catch {
            throw 'The required Microsoft C++ Build Tools and Windows SDK could not be installed. Approve their UAC prompt and retry.'
        }
        if ($process.ExitCode -notin @(0, 3010)) { throw "Microsoft C++ Build Tools installation failed (exit $($process.ExitCode))." }
        $msvc = Find-CorinthosMsvc
        $sdk = Get-WindowsSdkBuild
        if (!$msvc -or !$sdk) { throw 'The required Microsoft C++ toolset or Windows SDK could not be verified after setup.' }
    }
    $webview = Get-CorinthosWebView2
    $webviewSigner = $null
    if (!$webview) {
        $download = Receive-MicrosoftSetup $Microsoft.webview2 $Work 'webview2' $Microsoft.publisher
        $webviewSigner = $download.Signer
        $process = Start-Process -FilePath $download.Executable -ArgumentList @('/silent', '/install') -WindowStyle Hidden -PassThru -Wait
        $webview = Get-CorinthosWebView2
        if ($process.ExitCode -ne 0 -or !$webview) { throw 'The official Microsoft WebView2 Evergreen Runtime could not be verified after setup.' }
    }
    return @{ Msvc = $msvc; Sdk = $sdk; WebView = [string]$webview; MsvcSigner = $msvcSigner; WebViewSigner = $webviewSigner }
}

function Invoke-CorinthosBuildNative(
    [string] $Executable, [string[]] $Arguments, [string] $Directory, $Environment,
    [string] $Operation, [int] $Minutes = 2, [switch] $Capture
) {
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.WorkingDirectory = $Directory
    $quoted = foreach ($argument in $Arguments) {
        if ($argument -match '[\r\n"]' -or $argument.EndsWith('\')) { throw 'Invalid build argument.' }
        '"' + $argument + '"'
    }
    $start.Arguments = $quoted -join ' '
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.Encoding]::UTF8
    $start.StandardErrorEncoding = [Text.Encoding]::UTF8
    $start.EnvironmentVariables.Clear()
    foreach ($name in $Environment.Keys) { $start.EnvironmentVariables[$name] = $Environment[$name] }
    $process = [Diagnostics.Process]::Start($start)
    try {
        # Drain both streams concurrently. Build output can contain local paths
        # or machine configuration, so only selected version output is returned.
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (!$process.WaitForExit([TimeSpan]::FromMinutes($Minutes).TotalMilliseconds)) {
            $taskkill = Join-Path ([Environment]::SystemDirectory) 'taskkill.exe'
            & $taskkill /F /T /PID $process.Id 2>$null | Out-Null
            if (!$process.WaitForExit(5000)) { $process.Kill() }
            throw "$Operation exceeded its build time limit."
        }
        if ($process.ExitCode -ne 0) { throw "$Operation failed (exit $($process.ExitCode))." }
        if (!$output.Wait(5000) -or !$errors.Wait(5000)) { throw "$Operation left a build stream open." }
        if ($Capture) {
            $result = $output.Result.Trim()
            if (!$result -or $result.Length -gt 4096 -or $result -match '[\x00-\x09\x0b\x0c\x0e-\x1f\x7f\uFFFD]') {
                throw "$Operation produced invalid output."
            }
            return $result
        }
    } finally {
        $process.Dispose()
    }
}

function New-CorinthosBuildEnvironment($Tools, $Prerequisites, [string] $Work, [string] $Git) {
    $homePath = Join-Path $Work 'build-home'
    $temp = Join-Path $Work 'build-temp'
    $cargo = Join-Path $homePath '.cargo'
    $npmCache = Join-Path $homePath 'npm-cache'
    $target = Join-Path $Work 'cargo-target'
    foreach ($path in @($homePath, $temp, $cargo, $npmCache, $target)) { [void] [IO.Directory]::CreateDirectory($path) }
    $config = Join-Path $homePath '.npmrc'
    $globalConfig = Join-Path $homePath 'npm-global.conf'
    [IO.File]::WriteAllText($config, '')
    [IO.File]::WriteAllText($globalConfig, '')
    $sys = [Environment]::SystemDirectory
    $windows = [IO.Path]::GetDirectoryName($sys)
    $msvc = $Prerequisites.Msvc
    $sdk = $Prerequisites.Sdk
    $pathList = @(
        [IO.Path]::GetDirectoryName($Tools.Node), [IO.Path]::GetDirectoryName($Tools.Cargo),
        $msvc.Bin, $sdk.Bin, [IO.Path]::GetDirectoryName($Git), $sys, $windows
    )
    return @{
        PATH = $pathList -join ';'; SystemRoot = $windows; windir = $windows; ComSpec = Join-Path $sys 'cmd.exe'
        PATHEXT = '.COM;.EXE;.BAT;.CMD'; TEMP = $temp; TMP = $temp; HOME = $homePath
        # Windows HTTP creates compatibility junctions when USERPROFILE points at the build stage.
        USERPROFILE = [Environment]::GetFolderPath('UserProfile')
        LOCALAPPDATA = [Environment]::GetFolderPath('LocalApplicationData')
        APPDATA = [Environment]::GetFolderPath('ApplicationData')
        PROGRAMDATA = [Environment]::GetFolderPath('CommonApplicationData')
        PROCESSOR_ARCHITECTURE = 'AMD64'
        NUMBER_OF_PROCESSORS = [string][Environment]::ProcessorCount
        INCLUDE = @($msvc.Include, (Join-Path $sdk.Include 'ucrt'), (Join-Path $sdk.Include 'shared'), (Join-Path $sdk.Include 'um'), (Join-Path $sdk.Include 'winrt')) -join ';'
        LIB = @($msvc.Lib, (Join-Path $sdk.Lib 'ucrt\x64'), (Join-Path $sdk.Lib 'um\x64')) -join ';'
        CC = Join-Path $msvc.Bin 'cl.exe'; CXX = Join-Path $msvc.Bin 'cl.exe'; AR = Join-Path $msvc.Bin 'lib.exe'
        CARGO_HOME = $cargo; CARGO_TARGET_DIR = $target; RUSTC = $Tools.Rustc
        CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER = Join-Path $msvc.Bin 'link.exe'
        CARGO_REGISTRIES_CRATES_IO_PROTOCOL = 'sparse'
        NPM_CONFIG_USERCONFIG = $config; NPM_CONFIG_GLOBALCONFIG = $globalConfig; NPM_CONFIG_CACHE = $npmCache
        NPM_CONFIG_REGISTRY = 'https://registry.npmjs.org/'; NPM_CONFIG_UPDATE_NOTIFIER = 'false'
    }
}

function Assert-CorinthosBuildLocks([string] $Desktop, [string] $Node, $Environment) {
    $checkNpm = @'
const lock = require('./package-lock.json');
if (lock.lockfileVersion !== 3 || !lock.packages || !lock.packages['']) process.exit(1);
for (const [name, entry] of Object.entries(lock.packages)) {
  if (!name) continue;
  if (typeof entry.resolved !== 'string' || !entry.resolved.startsWith('https://registry.npmjs.org/')) process.exit(2);
  const uri = new URL(entry.resolved);
  if (uri.origin !== 'https://registry.npmjs.org' || uri.username || uri.password || uri.search || uri.hash ||
      !/^sha512-[A-Za-z0-9+/]{86}==$/.test(entry.integrity ?? '')) process.exit(2);
}
'@
    $payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($checkNpm))
    Invoke-CorinthosBuildNative $Node @(
        '-e', "eval(Buffer.from('$payload','base64').toString('utf8'))"
    ) $Desktop $Environment 'Official locked npm dependency verification'
    $lock = Join-Path $Desktop 'src-tauri\Cargo.lock'
    $records = ([IO.File]::ReadAllText($lock) -split '(?m)^\[\[package\]\]\r?$') | Select-Object -Skip 1
    foreach ($record in $records) {
        if ($record -notmatch '(?m)^source = "registry\+https://github\.com/rust-lang/crates\.io-index"\r?$' -and
            $record -notmatch '(?m)^name = "corinthos-desktop"\r?$') {
            throw 'A locked Rust dependency is outside the official crates.io registry.'
        }
        if ($record -match '(?m)^source = ' -and $record -notmatch '(?m)^checksum = "[0-9a-f]{64}"\r?$') {
            throw 'A locked Rust dependency lacks its exact registry checksum.'
        }
    }
}

function New-CorinthosDesktopBuild([string] $Source, $Paths, [string] $Work) {
    if (!('System.IO.Compression.ZipFile' -as [type])) { Add-Type -AssemblyName System.IO.Compression.FileSystem }
    if (!('System.Net.Http.HttpClient' -as [type])) { Add-Type -AssemblyName System.Net.Http }
    $snapshot = Copy-CorinthosSourceSnapshot $Source $Work
    $manifestPath = Join-Path $snapshot.Directory 'platform\windows\toolchain-artifacts.json'
    $manifest = Read-CorinthosToolchainManifest $manifestPath
    $desktop = Join-Path $snapshot.Directory 'apps\desktop'
    $tauri = Get-Content -LiteralPath (Join-Path $desktop 'src-tauri\tauri.conf.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($tauri.version -cne '0.1.0' -or $tauri.identifier -cne 'org.corinthos.desktop' -or $tauri.bundle.active) {
        throw 'The checked-out Tauri configuration does not describe the supported release.'
    }
    $dependencies = Get-Content -LiteralPath (Join-Path $desktop 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $tauriVersion = [string]$dependencies.devDependencies.'@tauri-apps/cli'
    if ($tauriVersion -cnotmatch '^\d+\.\d+\.\d+$') { throw 'Tauri CLI requires an exact version pin.' }
    $lockPaths = @((Join-Path $desktop 'package-lock.json'), (Join-Path $desktop 'src-tauri\Cargo.lock'))
    $locks = @($lockPaths | ForEach-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash.ToLowerInvariant() })
    Show-InstallProgress 11 'Checking the Microsoft build and WebView2 prerequisites'
    $prerequisites = Get-CorinthosDesktopPrerequisites $manifest.microsoft $Work
    $tools = Expand-CorinthosToolchains $manifest (Join-Path $Paths.Cache 'toolchains') $Work
    $environment = New-CorinthosBuildEnvironment $tools $prerequisites $Work $snapshot.Git
    $node = Invoke-CorinthosBuildNative $tools.Node @('--version') $desktop $environment 'Node version check' -Capture
    $npm = Invoke-CorinthosBuildNative $tools.Node @($tools.Npm, '--version') $desktop $environment 'npm version check' -Capture
    $rustc = Invoke-CorinthosBuildNative $tools.Rustc @('--version', '--verbose') $desktop $environment 'Rust compiler version check' -Capture
    $cargo = Invoke-CorinthosBuildNative $tools.Cargo @('--version') $desktop $environment 'Cargo version check' -Capture
    if ($node -cne 'v24.22.2' -or $npm -cne '11.19.1' -or $rustc -cnotmatch '(?m)^rustc 1\.99\.1 \([\x20-\x7e]+\)\r?$' -or
        $rustc -cnotmatch '(?m)^host: x86_64-pc-windows-msvc\r?$' -or $cargo -cnotmatch '^cargo 1\.99\.1 \([\x20-\x7e]+\)$') {
        throw 'The staged tools do not report the pinned versions and Windows target.'
    }
    $targetLib = Invoke-CorinthosBuildNative $tools.Rustc @('--print', 'target-libdir', '--target', $script:CorinthosDesktopTarget) $desktop $environment 'Rust target standard library check' -Capture
    if ($targetLib.StartsWith('\\?\', [StringComparison]::Ordinal)) { $targetLib = $targetLib.Substring(4) }
    $targetLib = [IO.Path]::GetFullPath($targetLib)
    Assert-PlainPath $targetLib
    if (!$targetLib.StartsWith(([IO.Path]::GetDirectoryName([IO.Path]::GetDirectoryName($tools.Rustc)) + '\'), [StringComparison]::OrdinalIgnoreCase) -or
        !@(Get-ChildItem -LiteralPath $targetLib -Filter 'libstd-*.rlib' -File).Count) {
        throw 'The pinned Rust compiler does not resolve its target standard library.'
    }
    Assert-CorinthosBuildLocks $desktop $tools.Node $environment
    Show-InstallProgress 34 'Installing locked frontend build dependencies'
    Invoke-CorinthosBuildNative $tools.Node @($tools.Npm, 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--registry=https://registry.npmjs.org/') $desktop $environment 'Locked npm dependency install' 10
    $tauriCli = Join-Path $desktop 'node_modules\@tauri-apps\cli\tauri.js'
    $actualCli = Invoke-CorinthosBuildNative $tools.Node @($tauriCli, '--version') $desktop $environment 'Tauri CLI version check' -Capture
    if ($actualCli -cne "tauri-cli $tauriVersion") { throw 'The installed Tauri CLI does not report its exact lockfile version.' }
    foreach ($step in @(@('run','typecheck'), @('test'), @('run','build'))) {
        Invoke-CorinthosBuildNative $tools.Node (@($tools.Npm) + @($step)) $desktop $environment ("Frontend " + ($step -join ' ')) 10
    }
    Show-InstallProgress 50 'Building the production Windows desktop from locked Rust dependencies'
    $cargoManifest = Join-Path $desktop 'src-tauri\Cargo.toml'
    Invoke-CorinthosBuildNative $tools.Cargo @(
        'build', '--manifest-path', $cargoManifest, '--locked', '--release', '--target',
        $script:CorinthosDesktopTarget, '--no-default-features', '--features', 'desktop,custom-protocol'
    ) ([Environment]::SystemDirectory) $environment 'Locked desktop release build' 35
    $exe = Join-Path $environment.CARGO_TARGET_DIR "$script:CorinthosDesktopTarget\release\corinthos-desktop.exe"
    Assert-PlainPath $exe
    if (![IO.File]::Exists($exe)) { throw 'The expected production desktop executable is missing.' }
    for ($i = 0; $i -lt $lockPaths.Count; $i++) {
        if ((Get-FileHash -LiteralPath $lockPaths[$i] -Algorithm SHA256).Hash.ToLowerInvariant() -cne $locks[$i]) {
            throw 'A build lockfile changed during setup.'
        }
    }
    $after = Confirm-CorinthosSourceIdentity $snapshot.Git $Source
    if ($after.Commit -cne $snapshot.Identity.Commit -or $after.Tree -cne $snapshot.Identity.Tree) {
        throw 'The source checkout changed during the desktop build.'
    }
    Show-InstallProgress 55 'Production desktop and source provenance verified'
    return @{
        Executable = $exe; Git = $snapshot.Git; SourceSnapshot = $snapshot.Directory; ManifestPath = $manifestPath
        Provenance = [ordered]@{
            source_commit = $snapshot.Identity.Commit; source_tree = $snapshot.Identity.Tree
            architecture = $script:CorinthosDesktopTarget
            node = $node.Substring(1); npm = $npm; rustc = ($rustc -split '\r?\n')[0]; cargo = $cargo
            tauri_cli = $tauriVersion; msvc = $prerequisites.Msvc.Version; webview2 = $prerequisites.WebView
            windows_sdk = $prerequisites.Sdk.Version
            msvc_signer = $prerequisites.MsvcSigner; webview2_signer = $prerequisites.WebViewSigner
            package_lock_sha256 = $locks[0]; cargo_lock_sha256 = $locks[1]
            desktop_sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
}
