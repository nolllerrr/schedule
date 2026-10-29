param(
    [string]$Python = '',
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        $Python = $venvPython
    } else {
        $entryPoint = Get-Command schedule-bot -ErrorAction SilentlyContinue
        if ($entryPoint) {
            $installedPython = Join-Path (Split-Path -Parent (Split-Path -Parent $entryPoint.Source)) 'python.exe'
            if (Test-Path -LiteralPath $installedPython) { $Python = $installedPython }
        }
        if (-not $Python) { $Python = (Get-Command python -ErrorAction Stop).Source }
    }
}

$previousPythonPath = $env:PYTHONPATH
Push-Location $projectRoot
try {
    # An editable installation may point at a different Git checkout.
    $env:PYTHONPATH = Join-Path $projectRoot 'src'
    if ($previousPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $previousPythonPath }
    if ($Check) {
        & $Python -c 'import schedule_bot.repository as r; print(r.__file__); print("Schema:", r.ScheduleRepository.SCHEMA_VERSION)'
    } else {
        & $Python -m schedule_bot run
    }
    if ($LASTEXITCODE -ne 0) { throw "Local bot exited with code $LASTEXITCODE" }
} finally {
    $env:PYTHONPATH = $previousPythonPath
    Pop-Location
}
