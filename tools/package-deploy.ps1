param(
    [string]$Output = "dist\earnapp-monitor-deploy.zip"
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$outputPath = if ([IO.Path]::IsPathRooted($Output)) { $Output } else { Join-Path $root $Output }
$staging = Join-Path ([IO.Path]::GetTempPath()) ("earnapp-deploy-" + [guid]::NewGuid().ToString("N"))

New-Item -ItemType Directory -Path $staging | Out-Null
try {
    $items = @(
        "app",
        "config",
        "deploy",
        "Dockerfile",
        ".dockerignore",
        "docker-compose.yml",
        "requirements.txt",
        "run.py",
        ".env.example",
        "README.md"
    )
    foreach ($item in $items) {
        $source = Join-Path $root $item
        if (-not (Test-Path $source)) { continue }
        Copy-Item -Path $source -Destination (Join-Path $staging $item) -Recurse -Force
    }

    Get-ChildItem $staging -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem $staging -Recurse -File -Include "*.pyc", "*.pyo" |
        Remove-Item -Force -ErrorAction SilentlyContinue

    # Folder kosong wajib ada supaya bind mount Docker tidak dibuat root-owned.
    New-Item -ItemType Directory -Path (Join-Path $staging "data") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $staging "secrets") -Force | Out-Null
    Set-Content -Path (Join-Path $staging "data\.keep") -Value "" -NoNewline
    Set-Content -Path (Join-Path $staging "secrets\.keep") -Value "" -NoNewline

    $parent = Split-Path $outputPath -Parent
    if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    if (Test-Path $outputPath) { Remove-Item $outputPath -Force }
    Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $outputPath -Force
    Write-Output "Arsip deploy: $outputPath"
} finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}
