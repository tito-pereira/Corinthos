# SPDX-License-Identifier: MPL-2.0
# Helpers for the disposable Windows installer verification.

function Find-ProductProcesses([string[]] $Executables) {
    $names = @($Executables | ForEach-Object { [IO.Path]::GetFileName($_) } | Sort-Object -Unique)
    $where = ($names | ForEach-Object { "Name = '$_'" }) -join ' OR '
    Get-CimInstance Win32_Process -Filter $where | Where-Object { $_.ExecutablePath -in $Executables }
}

function Invoke-InstalledDesktopSmoke([string] $Desktop, [string] $PrivatePython, [string] $Project) {
    # Exercise the shipping native health boundary; the interactive GUI journey is separate.
    Assert-PlainPath $Desktop
    Assert-PlainPath $PrivatePython
    $marker = Join-Path $Project 'context_agents\corinthos\state.json'
    $before = (Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $Desktop -ArgumentList '--installation-check' -PassThru -WindowStyle Hidden
    try {
        if (!$process.WaitForExit(30000)) {
            $process.Kill()
            [void] $process.WaitForExit(5000)
            throw 'The shipping desktop did not complete its adjacent private-runtime handshake.'
        }
        Assert-Smoke ($process.ExitCode -eq 0) 'The shipping desktop failed its private-runtime health handshake.'
    } finally { $process.Dispose() }
    $watch.Stop()
    Assert-Smoke (!(Find-ProductProcesses @($Desktop, $PrivatePython))) 'The health check left an owned desktop or sidecar process running.'
    Assert-Smoke ((Get-FileHash -LiteralPath $marker -Algorithm SHA256).Hash -eq $before) 'Desktop health changed project identity.'
    Write-Host 'Shipping desktop verified its adjacent Python peer and reaped it; visual GUI acceptance is recorded separately.'
    return $watch.ElapsedMilliseconds
}
