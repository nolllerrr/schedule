[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$configPath = Join-Path $projectDir ".deploy.json"

if (-not (Test-Path -LiteralPath $configPath)) {
    throw "Missing $configPath. Copy .deploy.example.json to .deploy.json and fill it in."
}

$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
$server = [string]$config.server
$port = [int]$config.port
$remoteDir = [string]$config.remoteDir
$keyPath = [Environment]::ExpandEnvironmentVariables([string]$config.keyPath)

if ($server -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$') {
    throw "Invalid server value in .deploy.json"
}
if ($port -lt 1 -or $port -gt 65535) {
    throw "Invalid SSH port in .deploy.json"
}
if ($remoteDir -notmatch '^/[A-Za-z0-9._/-]+$' -or $remoteDir.Contains('..')) {
    throw "Invalid remoteDir value in .deploy.json"
}
if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    throw "SSH key not found: $keyPath"
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [string]$Command,

        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    & $Command @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE"
    }
}

if (-not $SkipTests) {
    $python = Join-Path $projectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Virtual environment not found: $python"
    }

    Invoke-Checked -Command $python -ArgumentList @(
        "-m",
        "pytest",
        "--basetemp=$(Join-Path $projectDir '.pytest-tmp')"
    )
}

$sshCommon = @(
    "-i", $keyPath,
    "-o", "IdentitiesOnly=yes",
    "-o", "BatchMode=yes",
    "-p", [string]$port,
    $server
)

Invoke-Checked -Command "ssh" -ArgumentList ($sshCommon + @(
    "test -f '$remoteDir/.env' && test -d '$remoteDir/data'"
))

$files = @(
    "Dockerfile",
    "compose.yaml",
    "pyproject.toml",
    "README.md",
    ".dockerignore"
) | ForEach-Object { Join-Path $projectDir $_ }

$scpCommon = @(
    "-i", $keyPath,
    "-o", "IdentitiesOnly=yes",
    "-P", [string]$port
)

Invoke-Checked -Command "scp" -ArgumentList (
    $scpCommon + $files + @("${server}:$remoteDir/")
)
Invoke-Checked -Command "scp" -ArgumentList (
    $scpCommon + @("-r", (Join-Path $projectDir "src"), "${server}:$remoteDir/")
)

Invoke-Checked -Command "ssh" -ArgumentList ($sshCommon + @(
    "cd '$remoteDir' && docker compose config -q && docker compose up --build -d && docker compose ps && docker compose logs --tail=40 bot"
))

Write-Host "Deployment completed successfully." -ForegroundColor Green
