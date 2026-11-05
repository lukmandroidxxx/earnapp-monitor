#requires -Version 5.1
# EarnApp Monitor - public HTTPS without opening any router port.
# Runs a Cloudflare "quick tunnel" (no account, no card) in front of the app
# and registers it as a boot task. The public URL is written to
# <InstallDir>\data\public_url.txt and printed in the console.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup-tunnel.ps1
#   powershell -ExecutionPolicy Bypass -File setup-tunnel.ps1 -Port 8080

param(
    [int]$Port = 8080,
    [string]$InstallDir = "$env:USERPROFILE\earnapp-monitor"
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($m) { Write-Host "`n>>> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Step "Checking the dashboard"
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/ping" -UseBasicParsing -TimeoutSec 5
    if ($r.Content -notmatch 'pong') { throw "unexpected body" }
    Ok "app is running on port $Port"
} catch {
    throw "The dashboard is not answering on 127.0.0.1:$Port. Run setup-app.ps1 first."
}

$tools = Join-Path $InstallDir 'tools'
New-Item -ItemType Directory -Path $tools -Force | Out-Null
$cf = Join-Path $tools 'cloudflared.exe'

Step "Downloading cloudflared"
if (-not (Test-Path $cf)) {
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe?t=" + [DateTime]::UtcNow.Ticks
    Invoke-WebRequest -Uri $url -OutFile $cf -UseBasicParsing
}
Ok "$cf (v$(& $cf --version 2>&1))"

Step "Starting the tunnel"
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
$outLog = Join-Path $tools 'cf.out.log'
$errLog = Join-Path $tools 'cf.err.log'
Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
Start-Process -FilePath $cf -ArgumentList @('tunnel', '--no-autoupdate', '--url', "http://127.0.0.1:$Port", '--loglevel', 'info') `
    -WorkingDirectory $tools -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog

$publicUrl = $null
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 3
    $text = ''
    foreach ($f in @($outLog, $errLog)) { if (Test-Path $f) { $text += (Get-Content $f -Raw) } }
    $m = [regex]::Match($text, 'https://[a-z0-9-]+\.trycloudflare\.com')
    if ($m.Success) { $publicUrl = $m.Value; break }
}
if (-not $publicUrl) { throw "cloudflared did not report a public URL - see $errLog" }
Ok $publicUrl
$publicUrl | Out-File -FilePath (Join-Path $InstallDir 'data\public_url.txt') -Encoding ascii

Step "Registering boot task"
$bat = Join-Path $tools 'tunnel.bat'
$batText = @"
@echo off
cd /d "$tools"
"$cf" tunnel --no-autoupdate --url http://127.0.0.1:$Port --loglevel info >> "$tools\cloudflared-run.log" 2>&1
"@
$batText = ($batText -replace "`r`n", "`n") -replace "`n", "`r`n"
[System.IO.File]::WriteAllText($bat, $batText, (New-Object System.Text.UTF8Encoding($false)))

$taskName = "EarnAppMonitorTunnel"
cmd /c "schtasks /Delete /TN $taskName /F >nul 2>&1"
if ($admin) {
    $create = 'schtasks /Create /TN ' + $taskName + ' /TR "\"' + $bat + '\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0001:00 /F >nul 2>&1'
    cmd /c $create
    if ($LASTEXITCODE -eq 0) { Ok "tunnel starts at boot" } else { Warn "could not create the tunnel boot task" }
} else {
    Warn "not Administrator: tunnel will not start at boot"
}

Step "Verifying from the internet"
$ok = $false
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 3
    try {
        $r = Invoke-WebRequest -Uri "$publicUrl/ping" -UseBasicParsing -TimeoutSec 15
        if ($r.Content -match 'pong') { $ok = $true; break }
    } catch { }
}
if ($ok) { Ok "$publicUrl/ping -> pong" } else { Warn "not reachable yet - wait a minute and open $publicUrl/ping" }

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Public URL : $publicUrl"
Write-Host " Note       : quick tunnels get a new random URL after a reboot."
Write-Host "              The current URL is saved in $InstallDir\data\public_url.txt"
Write-Host " Stable URL : use Tailscale Funnel or forward TCP 80/443 and run setup-caddy.ps1"
Write-Host "==================================================" -ForegroundColor Cyan
