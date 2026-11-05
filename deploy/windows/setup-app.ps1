#requires -Version 5.1
# EarnApp Monitor - install & run on a Windows RDP/server.
# Works without Administrator (app itself); admin only unlocks firewall + boot task.
#
# Usage (run inside the RDP, PowerShell):
#   powershell -ExecutionPolicy Bypass -File setup-app.ps1
#
# Optional parameters (otherwise it asks):
#   -DatabaseUrl "postgresql+psycopg://user:pass@host/db?sslmode=require"
#   -SecretKey   "44-char-fernet-key"
#   -Password    "dashboard-password"
#   -Port 8080

param(
    [string]$DatabaseUrl = "",
    [string]$SecretKey = "",
    [string]$Password = "",
    [string]$User = "admin",
    [string]$SheetId = "1CI1p_r8tGFztywALNbIT9yjjaMcwK_sphOhZ-Gnr8nA",
    [string]$SheetGid = "0",
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
Write-Host "Administrator: $(if ($admin) { 'YES' } else { 'NO (boot task will wait for logon)' })"

if (-not $DatabaseUrl) { $DatabaseUrl = Read-Host "DATABASE_URL (postgresql+psycopg://...neon.tech/neondb?sslmode=require)" }
if (-not $SecretKey)   { $SecretKey   = Read-Host "SECRET_KEY (44 chars, must match the key used for existing cookies)" }
if (-not $Password)    { $Password    = Read-Host "DASHBOARD_PASSWORD" }
if (-not $DatabaseUrl) { throw "DATABASE_URL is required" }
if (-not $SecretKey)   { throw "SECRET_KEY is required" }
if (-not $Password)    { throw "DASHBOARD_PASSWORD is required" }

$os = Get-CimInstance Win32_OperatingSystem
$freeMb = [math]::Round($os.FreePhysicalMemory / 1024)
Write-Host "Free RAM: $freeMb MB"
if ($freeMb -lt 150) { Warn "less than 150 MB free - the app may be killed by the OS" }

# ---------------------------------------------------------------- 1. code
Step "Downloading code"
$zip = Join-Path $env:TEMP "earnapp-monitor-main.zip"
$tmp = Join-Path $env:TEMP "eam-src"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
$zipUrl = "https://github.com/lukmandroidxxx/earnapp-monitor/archive/refs/heads/main.zip?t=" + [DateTime]::UtcNow.Ticks
Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
Expand-Archive -Path $zip -DestinationPath $tmp -Force
$src = Get-ChildItem $tmp -Directory | Select-Object -First 1
if (-not $src) { throw "Archive did not contain a project folder" }

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
foreach ($item in @('app', 'config', 'deploy', 'requirements.txt', 'run.py', 'README.md')) {
    $from = Join-Path $src.FullName $item
    if (Test-Path $from) { Copy-Item $from -Destination $InstallDir -Recurse -Force }
}
New-Item -ItemType Directory -Path (Join-Path $InstallDir 'data') -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $InstallDir 'secrets') -Force | Out-Null
Ok $InstallDir

# ---------------------------------------------------------------- 2. python
Step "Preparing Python"
$pythonExe = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    try { $pythonExe = (& py -3 -c "import sys;print(sys.executable)" 2>$null) } catch { }
}
if (-not $pythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $pythonExe = (Get-Command python).Source
}

if (-not $pythonExe) {
    Warn "Python not found - installing Python 3.12 for the current user (~30 MB)"
    $inst = Join-Path $env:TEMP "python-3.12.8-amd64.exe"
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" -OutFile $inst -UseBasicParsing
    $target = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"
    Start-Process -FilePath $inst -ArgumentList "/quiet", "InstallAllUsers=0", "Include_pip=1", "Include_launcher=0", "PrependPath=0", "TargetDir=$target" -Wait
    $pythonExe = Join-Path $target "python.exe"
}
if (-not (Test-Path $pythonExe)) { throw "Python executable not found" }
$ver = (& $pythonExe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])").Trim()
Ok "$pythonExe (Python $ver)"

$venv = Join-Path $InstallDir ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    & $pythonExe -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
$vpy = Join-Path $venv "Scripts\python.exe"

# ---------------------------------------------------------------- 3. deps
Step "Installing dependencies"
& $vpy -m pip install --disable-pip-version-check --no-input --upgrade pip | Out-Null
& $vpy -m pip install --disable-pip-version-check --no-input -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Ok "requirements installed"

# ---------------------------------------------------------------- 4. .env
Step "Writing .env"
$envPath = Join-Path $InstallDir ".env"
$lines = @(
    "DATABASE_URL=$DatabaseUrl",
    "SECRET_KEY=$SecretKey",
    "GOOGLE_SHEET_ID=$SheetId",
    "GOOGLE_SHEET_GID=$SheetGid",
    "DASHBOARD_USER=$User",
    "DASHBOARD_PASSWORD=$Password",
    "HOST=0.0.0.0",
    "PORT=$Port",
    "LOGIN_HELPER=0",
    "AUTO_SYNC=1",
    "AUTO_POLL=0",
    "DEVICE_SYNC=1",
    "SCRAPER_MODE=off",
    "SHEET_WRITEBACK=0",
    "EARN_APP_API_BASE=https://earnapp.com/dashboard/api",
    "EARN_APP_APP_VERSION=1.685.135",
    "REQUEST_TIMEOUT=30"
)
if (Test-Path $envPath) { Copy-Item $envPath "$envPath.bak" -Force; Warn "old .env backed up to .env.bak" }
[System.IO.File]::WriteAllText($envPath, ($lines -join "`r`n") + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
Ok $envPath

# ---------------------------------------------------------------- 5. launcher
$bat = Join-Path $InstallDir "start.bat"
$batText = @"
@echo off
cd /d "%~dp0"
"$vpy" -X utf8 run.py >> "data\server.out.log" 2>> "data\server.err.log"
"@
$batText = ($batText -replace "`r`n", "`n") -replace "`n", "`r`n"
[System.IO.File]::WriteAllText($bat, $batText, (New-Object System.Text.UTF8Encoding($false)))

# ---------------------------------------------------------------- 6. start now
Step "Starting the dashboard on port $Port"
$existing = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($existing.Count -gt 0) {
    Warn "Port $Port already in use by PID $($existing[0].OwningProcess) - checking whether it is the dashboard"
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/ping" -UseBasicParsing -TimeoutSec 5
        if ($r.Content -match 'pong') { Ok "already running - skipping start" }
        else { throw "Port $Port is in use by another service" }
    } catch { throw "Port $Port is in use by PID $($existing[0].OwningProcess) and does not answer /ping" }
} else {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$bat`"" -WorkingDirectory $InstallDir -WindowStyle Hidden
    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Seconds 2
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/ping" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200 -and $r.Content -match 'pong') { $up = $true; break }
        } catch { }
    }
    if ($up) { Ok "responds on http://127.0.0.1:$Port/ping" }
    else {
        Warn "no response yet - check $(Join-Path $InstallDir 'data\server.err.log')"
        Get-Content (Join-Path $InstallDir "data\server.err.log") -Tail 20
    }
}

# ---------------------------------------------------------------- 7. autostart
Step "Registering auto-start task"
$taskName = "EarnAppMonitor"
cmd /c "schtasks /Delete /TN $taskName /F >nul 2>&1"
if ($admin) {
  $create = 'schtasks /Create /TN ' + $taskName + ' /TR "\"' + $bat + '\"" /SC ONSTART /RU SYSTEM /RL HIGHEST /DELAY 0000:30 /F >nul 2>&1'
  cmd /c $create
  if ($LASTEXITCODE -eq 0) { Ok "starts at boot as SYSTEM (survives reboot without logon)" }
  else { Warn "could not create the SYSTEM boot task (exit $LASTEXITCODE)" }
} else {
  $create = 'schtasks /Create /TN ' + $taskName + ' /TR "\"' + $bat + '\"" /SC ONLOGON /RL LIMITED /DELAY 0000:30 /F >nul 2>&1'
  cmd /c $create
  Ok "starts at logon of $env:USERNAME"
  Warn "without admin it only starts after someone logs in; ask the provider for admin or use a boot task"
}

# ---------------------------------------------------------------- 8. firewall
if ($admin) {
    Step "Opening firewall port $Port"
    Get-NetFirewallRule -DisplayName "EarnApp Monitor $Port" -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName "EarnApp Monitor $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Any | Out-Null
    Ok "inbound TCP $Port allowed"
} else {
    Warn "no admin: firewall rule not created"
}

# ---------------------------------------------------------------- summary
Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Install dir : $InstallDir"
Write-Host " Local test  : http://127.0.0.1:$Port/ (user: $User)"
Write-Host " Logs        : $InstallDir\data\server.err.log"
Write-Host " Restart     : schtasks /Run /TN $taskName"
Write-Host " Stop        : schtasks /End /TN $taskName"
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "Next: open TCP $Port from outside (public IP), or run setup-caddy.ps1 for HTTPS." -ForegroundColor Cyan
