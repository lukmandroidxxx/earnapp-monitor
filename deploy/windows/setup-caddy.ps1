#requires -Version 5.1
# EarnApp Monitor - public HTTPS in front of the app, using Caddy + sslip.io.
# No domain needed: <your-public-ip>.sslip.io resolves to your IP.
# Requires Administrator (firewall + boot task) and inbound TCP 80 + 443 open.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup-caddy.ps1
#   powershell -ExecutionPolicy Bypass -File setup-caddy.ps1 -Domain "1.2.3.4.sslip.io" -Email "you@mail.com" -Port 8080

param(
    [string]$Domain = "",
    [string]$Email = "",
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
if (-not $admin) { Warn "not Administrator: firewall + boot task will be skipped" }

# ---------------------------------------------------------------- target
if (-not $Domain) {
    $pub = $null
    try { $pub = (Invoke-RestMethod -Uri "https://api.ipify.org?format=json" -TimeoutSec 10).ip } catch { }
    if (-not $pub) { $pub = Read-Host "Public IP of this server" }
    $Domain = "$pub.sslip.io"
}
Write-Host "Domain: $Domain"
Write-Host "Upstream: http://127.0.0.1:$Port"

# make sure the app is alive first
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/ping" -UseBasicParsing -TimeoutSec 5
    if ($r.Content -notmatch 'pong') { throw "unexpected body" }
    Ok "app is running locally"
} catch {
    throw "The dashboard is not answering on 127.0.0.1:$Port. Run setup-app.ps1 first."
}

# ---------------------------------------------------------------- caddy
Step "Downloading Caddy (Windows amd64)"
$tools = Join-Path $InstallDir "tools\caddy"
New-Item -ItemType Directory -Path $tools -Force | Out-Null
$caddyExe = Join-Path $tools "caddy.exe"
if (-not (Test-Path $caddyExe)) {
    $zip = Join-Path $env:TEMP "caddy_windows.zip"
    Invoke-WebRequest -Uri "https://caddyserver.com/api/download?os=windows&arch=amd64" -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tools -Force
    if (-not (Test-Path $caddyExe)) { throw "caddy.exe not found in archive" }
}
Ok $caddyExe

# ---------------------------------------------------------------- config
Step "Writing Caddyfile"
$caddyfile = Join-Path $tools "Caddyfile"
$caddyGlobal = "{" + "`r`n"
if ($Email) { $caddyGlobal += "    email $Email`r`n" }
$caddyGlobal += "    admin off`r`n}" + "`r`n"
$site = @"
$Domain {
    encode gzip
    reverse_proxy 127.0.0.1:$Port
}
"@
[System.IO.File]::WriteAllText($caddyfile, $caddyGlobal + $site, (New-Object System.Text.UTF8Encoding($false)))
Ok $caddyfile

& $caddyExe validate --config $caddyfile --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw "Caddyfile is invalid" }

# ---------------------------------------------------------------- firewall
if ($admin) {
    Step "Opening firewall TCP 80/443"
    foreach ($p in @(80, 443)) {
        Get-NetFirewallRule -DisplayName "Caddy HTTPS $p" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
        New-NetFirewallRule -DisplayName "Caddy HTTPS $p" -Direction Inbound -Protocol TCP -LocalPort $p -Action Allow -Profile Any | Out-Null
    }
    Ok "80/443 open"
}

# ---------------------------------------------------------------- run
Step "Starting Caddy"
$running = @(Get-Process caddy -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) { $running | Stop-Process -Force; Start-Sleep -Seconds 2 }

$bat = Join-Path $tools "caddy-run.bat"
$batText = "@echo off`r`ncd /d `"$tools`"`r`n`"$caddyExe`" run --config `"$caddyfile`" --adapter caddyfile >> `"$tools\caddy.log`" 2>&1`r`n"
[System.IO.File]::WriteAllText($bat, $batText, (New-Object System.Text.UTF8Encoding($false)))

Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$bat`"" -WorkingDirectory $tools -WindowStyle Hidden
Start-Sleep -Seconds 8

# ---------------------------------------------------------------- boot task
$taskName = "EarnAppMonitorCaddy"
cmd /c "schtasks /Delete /TN $taskName /F >nul 2>&1"
if ($admin) {
    Step "Registering Caddy boot task"
    $create = 'schtasks /Create /TN ' + $taskName + ' /TR "\"' + $bat + '\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:45 /F >nul 2>&1'
    cmd /c $create
    if ($LASTEXITCODE -eq 0) { Ok "Caddy starts at boot" } else { Warn "could not create the Caddy boot task (exit $LASTEXITCODE)" }
}

# ---------------------------------------------------------------- verify
Step "Verifying HTTPS"
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 6
    try {
        $r = Invoke-WebRequest -Uri "https://$Domain/ping" -UseBasicParsing -TimeoutSec 10
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {
        $msg = $_.Exception.Message
    }
}
if ($ok) {
    Ok "https://$Domain/ping -> 200"
    Write-Host ""
    Write-Host "Dashboard: https://$Domain/" -ForegroundColor Green
} else {
    Warn "HTTPS not ready yet: $msg"
    Write-Host "Check log: $tools\caddy.log" -ForegroundColor Yellow
    Write-Host "If the log shows ACME/port timeouts, inbound 80/443 is blocked by the provider." -ForegroundColor Yellow
    Write-Host "In that case use Tailscale Funnel instead." -ForegroundColor Yellow
}
