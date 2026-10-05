<#
.SYNOPSIS
    Re-run the model-and-research half of the VIGIL pipeline, ONE OS PROCESS PER STAGE.

.DESCRIPTION
    Native-Windows equivalent of scripts/rebuild_models.sh. No bash, no WSL, no Git Bash.

    Rationale: the stages are memory-heavy (sequence models build 3-D tensors); running them in a
    single long-lived process on a small machine was killed by the OOM reaper. One process per
    stage returns all memory to the OS between stages and is otherwise identical in behaviour.

    Each stage runs with --horizons 1 (the safe default) and --offline, so it rebuilds from the
    raw data cached in data/raw/ and reproduces the published artefacts without any network call.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\rebuild_models.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\rebuild_models.ps1 -StopOnError

.NOTES
    Exit code 0 only when every stage succeeded; otherwise the number of failed stages.
#>
[CmdletBinding()]
param(
    # Abort at the first failing stage instead of continuing and reporting at the end.
    [switch]$StopOnError,
    # Rebuild additional horizons (default: the safe horizon-1-only build).
    [int[]]$Horizons = @(1),
    # Allow network access during the rebuild (default: offline replay of data/raw/).
    [switch]$AllowNetwork
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# Repository root = the parent of the directory holding this script. Resolved from the script's
# own location, so the working directory the user starts from does not matter.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = (Resolve-Path (Join-Path $ScriptDir '..')).Path
Push-Location $RepoRoot

function Resolve-PythonCommand {
    foreach ($candidate in @('python', 'py', 'python3')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $cmd) {
            if ($candidate -eq 'py') { return @('py', '-3') }
            return @($candidate)
        }
    }
    return $null
}

$Python = Resolve-PythonCommand
if ($null -eq $Python) {
    Write-Error 'No Python interpreter found on PATH (tried python, py, python3). Activate your virtual environment first: .\.venv\Scripts\Activate.ps1'
    Pop-Location
    exit 127
}

$Stages = @(
    'models', 'ensemble', 'calibration', 'uncertainty', 'drift', 'backtest',
    'decision_quality', 'forecasts', 'failure_lab', 'experiments', 'report'
)

$HorizonArgs = @('--horizons') + ($Horizons | ForEach-Object { "$_" })
$OfflineArgs = if ($AllowNetwork) { @() } else { @('--offline') }

Write-Host ''
Write-Host "VIGIL staged rebuild — repository: $RepoRoot"
Write-Host ("Interpreter: {0} | horizons: {1} | network: {2}" -f ($Python -join ' '),
            ($Horizons -join ' '), $(if ($AllowNetwork) { 'allowed' } else { 'offline' }))
Write-Host ''

$Failed = @()
foreach ($stage in $Stages) {
    Write-Host "=== $stage ===" -ForegroundColor Cyan
    $argv = @('scripts/run_pipeline.py', '--stage', $stage) + $HorizonArgs + $OfflineArgs
    & $Python[0] @($Python[1..($Python.Length - 1)] + $argv)
    $rc = $LASTEXITCODE
    if ($rc -ne 0) {
        Write-Host "STAGE $stage FAILED rc=$rc" -ForegroundColor Red
        $Failed += $stage
        if ($StopOnError) { break }
    }
}

Write-Host ''
if ($Failed.Count -eq 0) {
    Write-Host "All $($Stages.Count) stages completed." -ForegroundColor Green
    Pop-Location
    exit 0
}

Write-Host ("FAILED stages ({0}): {1}" -f $Failed.Count, ($Failed -join ', ')) -ForegroundColor Red
Pop-Location
exit $Failed.Count
