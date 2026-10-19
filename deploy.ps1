[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$Plan,
    [string]$Revision = "HEAD",
    [string]$ConfigPath = "",
    [string]$PackageDirectory = "",
    [string]$RollbackRelease = "",
    [switch]$RestoreDatabase
)

$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
$python = Join-Path $projectDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = "python"
}
$script = Join-Path $projectDir "scripts\release_tools.py"
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $projectDir ".deploy.json"
}

function Invoke-Checked {
    param([string[]]$Arguments)
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Release operation failed (exit $LASTEXITCODE)."
    }
}

Push-Location -LiteralPath $projectDir
try {
    if ($Plan) {
        Invoke-Checked @($script, "plan", "--repo", $projectDir, "--revision", $Revision)
    }
    elseif ($PackageDirectory) {
        Invoke-Checked @($script, "package", "--repo", $projectDir, "--revision", $Revision, "--output", $PackageDirectory)
    }
    elseif ($RollbackRelease) {
        $arguments = @($script, "rollback", "--config", $ConfigPath, "--release", $RollbackRelease)
        if ($RestoreDatabase) { $arguments += "--restore-database" }
        Invoke-Checked $arguments
    }
    else {
        if (-not $SkipTests) {
            Invoke-Checked @("-m", "ruff", "check", "--select", "F", "src", "tests", "scripts")
            Invoke-Checked @("-m", "pytest", "--basetemp=.pytest-tmp-deploy")
        }
        Invoke-Checked @($script, "deploy", "--repo", $projectDir, "--revision", $Revision, "--config", $ConfigPath)
    }
}
finally {
    Pop-Location
}
