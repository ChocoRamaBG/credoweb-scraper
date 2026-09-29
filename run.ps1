param(
    [switch]$Sample,
    [switch]$ExportOnly,
    [switch]$DirectoryOnly,
    [switch]$EnrichOnly,
    [switch]$QuickExport,
    [switch]$Background,
    [string]$Output = "",
    [double]$Delay = 1.5,
    [int]$RecoveryRequests = 200,
    [int]$ExportInterval = 120,
    [double]$RefreshDays = 0,
    [int]$MaxRuntimeSeconds = 0,
    [int]$DiscoveryBudgetSeconds = 0
)
$ErrorActionPreference = "Stop"
$bundledPython = Join-Path $env:USERPROFILE ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe"
$pythonCommand = $null
$pythonPrefix = @()
if (Test-Path -LiteralPath $bundledPython) {
    $pythonCommand = $bundledPython
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonCommand = (Get-Command py).Source
    $pythonPrefix = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCommand = (Get-Command python).Source
} else {
    throw "Python 3.10 or newer is required. Install Python and run this script again."
}
if (-not $Output) {
    $Output = if ($Sample) { "sample" } else { "output" }
}
if (-not [IO.Path]::IsPathRooted($Output)) {
    $Output = Join-Path $PSScriptRoot $Output
}
$scriptArguments = @((Join-Path $PSScriptRoot "credoweb_scraper.py"), "--output", $Output, "--delay", $Delay.ToString([Globalization.CultureInfo]::InvariantCulture))
if ($Sample) {
    $scriptArguments += @("--max-pages", "2", "--max-profiles", "6", "--max-section-pages", "2")
}
if ($ExportOnly) { $scriptArguments += "--export-only" }
if ($DirectoryOnly) { $scriptArguments += "--directory-only" }
if ($EnrichOnly) { $scriptArguments += "--enrich-only" }
if ($QuickExport) { $scriptArguments += "--quick-export" }
$scriptArguments += @("--recovery-requests", $RecoveryRequests.ToString(), "--export-interval", $ExportInterval.ToString())
if ($RefreshDays -gt 0) { $scriptArguments += @("--refresh-days", $RefreshDays.ToString([Globalization.CultureInfo]::InvariantCulture)) }
if ($MaxRuntimeSeconds -gt 0) { $scriptArguments += @("--max-runtime-seconds", $MaxRuntimeSeconds.ToString()) }
if ($DiscoveryBudgetSeconds -gt 0) { $scriptArguments += @("--discovery-budget-seconds", $DiscoveryBudgetSeconds.ToString()) }
if ($Background) {
    New-Item -ItemType Directory -Force -Path $Output | Out-Null
    # Windows Start-Process joins ArgumentList, so quote every argument explicitly.
    $allArguments = @($pythonPrefix) + @($scriptArguments)
    $quotedArguments = $allArguments | ForEach-Object {
        '"' + ([string]$_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $collector = Start-Process -FilePath $pythonCommand -ArgumentList ($quotedArguments -join ' ') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $Output "job-$stamp.stdout.log") -RedirectStandardError (Join-Path $Output "job-$stamp.stderr.log") -PassThru
    @{ pid = $collector.Id; started_at = (Get-Date).ToString("o"); output = $Output } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Output "background-job.json") -Encoding UTF8
    Write-Output "Collector started in the background. PID: $($collector.Id)"
    Write-Output "Report: $(Join-Path $Output 'report.html')"
    Write-Output "Structured CSV (updated automatically): $(Join-Path $Output 'merge/profiles.csv')"
    exit 0
}
& $pythonCommand @pythonPrefix @scriptArguments
exit $LASTEXITCODE
