param(
    [Parameter(Mandatory = $true)]
    [string]$Server,

    [string]$User = "ubuntu",

    [Parameter(Mandatory = $true)]
    [string]$Key,

    [int]$Port = 22,

    # Ikut memindahkan data/monitor.db dan secrets/secret.key dari PC ini.
    [switch]$MigrateData,

    # Lewati pembuatan ulang zip (pakai dist yang sudah ada).
    [switch]$SkipPackage
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

foreach ($cmd in @("ssh", "scp")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "$cmd tidak ditemukan. Pasang OpenSSH Client di Windows."
    }
}
if (-not (Test-Path $Key)) {
    throw "Private key tidak ditemukan: $Key"
}
$Key = (Resolve-Path $Key).Path

if (-not $SkipPackage) {
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "package-deploy.ps1")
}

$zip = Join-Path $root "dist\earnapp-monitor-deploy.zip"
if (-not (Test-Path $zip)) {
    throw "Paket deploy tidak ada: $zip"
}

$target = "$User@$Server"
$sshArgs = @("-p", "$Port", "-i", $Key)
$scpArgs = @("-P", "$Port", "-i", $Key)

Write-Host ""
Write-Host "Upload paket ke ${target} ..."
& scp @scpArgs $zip "${target}:~/earnapp-monitor-deploy.zip"
if ($LASTEXITCODE -ne 0) { throw "Upload paket gagal." }

$remoteScript = @'
set -e
mkdir -p ~/earnapp-monitor
if ! command -v unzip >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y unzip
fi
unzip -o ~/earnapp-monitor-deploy.zip -d ~/earnapp-monitor >/dev/null
echo "Ekstrak selesai: ~/earnapp-monitor"
'@
& ssh @sshArgs $target $remoteScript
if ($LASTEXITCODE -ne 0) { throw "Ekstrak di server gagal." }

if ($MigrateData) {
    $db = Join-Path $root "data\monitor.db"
    if (-not (Test-Path $db)) {
        Write-Host "data\monitor.db tidak ada; migrasi data dilewati."
    }
    else {
        $python = Join-Path $root ".venv\Scripts\python.exe"
        if (-not (Test-Path $python)) { $python = "python" }

        $backup = Join-Path $env:TEMP ("earnapp-monitor-" + [guid]::NewGuid().ToString("N") + ".db")
        Write-Host "Membuat backup SQLite yang aman ..."
        & $python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $db $backup
        if ($LASTEXITCODE -ne 0) { throw "Backup database gagal." }

        & ssh @sshArgs $target "mkdir -p ~/earnapp-monitor/data ~/earnapp-monitor/secrets && chmod 700 ~/earnapp-monitor/data ~/earnapp-monitor/secrets"
        & scp @scpArgs $backup "${target}:~/earnapp-monitor/data/monitor.db"
        if ($LASTEXITCODE -ne 0) { throw "Upload database gagal." }
        Remove-Item $backup -Force -ErrorAction SilentlyContinue

        $keyFile = Join-Path $root "secrets\secret.key"
        if (Test-Path $keyFile) {
            & scp @scpArgs $keyFile "${target}:~/earnapp-monitor/secrets/secret.key"
            if ($LASTEXITCODE -ne 0) { throw "Upload secret.key gagal." }
            & ssh @sshArgs $target "chmod 600 ~/earnapp-monitor/secrets/secret.key"
        }
        else {
            Write-Host "secrets\secret.key tidak ada; cookie lama tidak akan bisa dibaca."
            Write-Host "Isi SECRET_KEY di .env server dengan kunci yang sama, atau salin manual."
        }
        Write-Host "Database dan kunci enkripsi sudah dipindahkan."
    }
}

Write-Host ""
Write-Host "Selesai upload. Sekarang jalankan setup di server (interaktif):"
Write-Host ""
Write-Host ("  ssh -p {0} -i `"{1}`" -t {2} `"cd ~/earnapp-monitor && sudo -E bash deploy/setup-server.sh`"" -f $Port, $Key, $target)
Write-Host ""
Write-Host "Script itu akan menanyakan domain (tekan Enter untuk <IP>.sslip.io) dan password."
