# Akses dashboard dari luar lewat Cloudflare Quick Tunnel (gratis, tanpa akun).
# Pemakaian:
#   .\tools\remote.ps1          -> start tunnel dan tampilkan URL publik
#   .\tools\remote.ps1 -Status  -> cek status + URL
#   .\tools\remote.ps1 -Stop    -> hentikan tunnel
#   .\tools\remote.ps1 -Protocol http2  -> paksa HTTP/2 kalau QUIC sering putus
#
# Kalau URL menampilkan Cloudflare error 1033, artinya cloudflared sedang tidak
# terhubung ke Cloudflare (biasanya Wi-Fi/Internet sempat putus). cloudflared
# mencoba menyambung ulang otomatis; cek dengan -Status.
#
# CATATAN: dashboard tetap berjalan di 127.0.0.1 saja. Tunnel yang menghubungkan
# ke internet. Pastikan DASHBOARD_PASSWORD sudah diisi di .env.

[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Status,
    [int]$Port = 8080,
    [ValidateSet("auto", "quic", "http2")]
    [string]$Protocol = "auto"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }

$exe     = Join-Path $PSScriptRoot "cloudflared.exe"
$data    = Join-Path $root "data"
$outLog  = Join-Path $data "cloudflared.out.log"
$errLog  = Join-Path $data "cloudflared.err.log"
$pidFile = Join-Path $data "cloudflared.pid"
$urlFile = Join-Path $data "remote_url.txt"

New-Item -ItemType Directory -Force -Path $data | Out-Null

function Read-FileShared([string]$Path) {
    # cloudflared menahan file log tetap terbuka, jadi Get-Content bisa gagal.
    if (-not (Test-Path $Path)) { return "" }
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        try {
            $reader = New-Object System.IO.StreamReader($stream)
            return $reader.ReadToEnd()
        } finally {
            $stream.Dispose()
        }
    } catch {
        return ""
    }
}

function Get-TunnelUrl {
    foreach ($file in @($errLog, $outLog)) {
        $text = Read-FileShared $file
        if ([string]::IsNullOrEmpty($text)) { continue }
        $match = [regex]::Match($text, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($match.Success) { return $match.Value }
    }
    return $null
}

function Get-SavedPid {
    if (Test-Path $pidFile) {
        try { return [int](Get-Content $pidFile -Raw).Trim() } catch { return $null }
    }
    return $null
}

function Show-Credentials {
    $envPath = Join-Path $root ".env"
    if (Test-Path $envPath) {
        $user = (Select-String -Path $envPath -Pattern '^DASHBOARD_USER=' | Select-Object -First 1).Line
        $pass = (Select-String -Path $envPath -Pattern '^DASHBOARD_PASSWORD=' | Select-Object -First 1).Line
        if ($user) { Write-Host ("Username : " + $user.Split('=', 2)[1]) }
        if ($pass) {
            $value = $pass.Split('=', 2)[1]
            if ([string]::IsNullOrWhiteSpace($value)) {
                Write-Host "Password : (kosong - dashboard TANPA login, jangan diekspos!)" -ForegroundColor Yellow
            } else {
                Write-Host ("Password : " + $value)
            }
        }
    }
}

if ($Stop) {
    $savedPid = Get-SavedPid
    if ($savedPid) {
        Get-Process -Id $savedPid -ErrorAction SilentlyContinue | Stop-Process -Force
    }
    Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
    Remove-Item $pidFile, $urlFile -Force -ErrorAction SilentlyContinue
    Write-Host "Tunnel dihentikan."
    return
}

$savedPid = Get-SavedPid
$running = if ($savedPid) { Get-Process -Id $savedPid -ErrorAction SilentlyContinue } else { $null }

if ($running) {
    Write-Host ("Tunnel berjalan (PID {0})." -f $running.Id)
    $url = Get-TunnelUrl
    if (-not $url) {
        $deadline = (Get-Date).AddSeconds(30)
        while (-not $url -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 1
            $url = Get-TunnelUrl
        }
    }
    if ($url) {
        Write-Host ("URL publik: {0}" -f $url)
        $url | Set-Content -Path $urlFile -Encoding ascii
    } else {
        Write-Host "URL belum muncul di log. Cek data\cloudflared.err.log." -ForegroundColor Yellow
    }
    Show-Credentials
    return
}

if ($Status) {
    Write-Host "Tunnel tidak berjalan."
    return
}

if (-not (Test-Path $exe)) {
    Write-Host "Mengunduh cloudflared (sekali saja)..." -ForegroundColor Cyan
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
}

Remove-Item $outLog, $errLog, $urlFile -Force -ErrorAction SilentlyContinue

function Start-Tunnel([string[]]$ExtraArgs) {
    $arguments = @("tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate") + $ExtraArgs
    return Start-Process -FilePath $exe -ArgumentList $arguments `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
        -WindowStyle Hidden -PassThru
}

function Wait-TunnelUrl([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $url = Get-TunnelUrl
        if ($url) { return $url }
        Start-Sleep -Seconds 1
    }
    return $null
}

Write-Host "Menjalankan Cloudflare Quick Tunnel..." -ForegroundColor Cyan
$protocolArgs = @()
if ($Protocol -ne "auto") { $protocolArgs = @("--protocol", $Protocol) }
$process = Start-Tunnel $protocolArgs
$process.Id | Set-Content -Path $pidFile -Encoding ascii
$publicUrl = Wait-TunnelUrl -Seconds 35

if (-not $publicUrl) {
    Write-Host "Tunnel belum tersambung, mencoba mode HTTP/2..." -ForegroundColor Yellow
    $process | Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item $outLog, $errLog -Force -ErrorAction SilentlyContinue
    $process = Start-Tunnel @("--protocol", "http2")
    $process.Id | Set-Content -Path $pidFile -Encoding ascii
    $publicUrl = Wait-TunnelUrl -Seconds 45
}

if (-not $publicUrl) {
    Write-Host "Gagal mendapatkan URL tunnel. Log terakhir:" -ForegroundColor Red
    if (Test-Path $errLog) { Get-Content $errLog -Tail 25 }
    exit 1
}

$publicUrl | Set-Content -Path $urlFile -Encoding ascii

Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host " Dashboard bisa diakses dari luar:" -ForegroundColor Green
Write-Host (" URL      : {0}" -f $publicUrl)
Show-Credentials
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "URL ini acak dan berubah setiap tunnel di-restart."
Write-Host "Hentikan dengan: .\tools\remote.ps1 -Stop"
