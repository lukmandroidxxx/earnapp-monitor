#requires -Version 5.1
# EarnApp Monitor - stable public HTTPS with Tailscale Funnel.
# No router port, no domain, no card: only a Tailscale login (Google/GitHub/Microsoft).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup-funnel.ps1
#   powershell -ExecutionPolicy Bypass -File setup-funnel.ps1 -Port 8080 -Hostname earnapp-monitor
#
# After the first run it prints a login URL - open it, sign in, then run the script again
# (or just `tailscale funnel --bg 8080`) once the machine shows as connected.

param(
    [int]$Port = 8080,
    [string]$Hostname = 'earnapp-monitor',
    [string]$InstallDir = "$env:USERPROFILE\earnapp-monitor"
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Step($m) { Write-Host "`n>>> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    !!  $m" -ForegroundColor Yellow }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Tailscale needs Administrator to install. Right-click PowerShell -> Run as administrator." }

Step "Checking the dashboard"
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/ping" -UseBasicParsing -TimeoutSec 5
    if ($r.Content -notmatch 'pong') { throw "unexpected body" }
    Ok "app is running on port $Port"
} catch {
    throw "The dashboard is not answering on 127.0.0.1:$Port. Run setup-app.ps1 first."
}

Step "Installing Tailscale"
$ts = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not (Test-Path $ts)) {
    $msi = Join-Path $env:TEMP 'tailscale-setup.msi'
    $url = 'https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi?t=' + [DateTime]::UtcNow.Ticks
    Invoke-WebRequest -Uri $url -OutFile $msi -UseBasicParsing
    $p = Start-Process msiexec.exe -ArgumentList @('/i', "`"$msi`"", '/quiet', '/norestart') -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "msiexec failed with $($p.ExitCode)" }
    Start-Sleep -Seconds 5
}
if (-not (Test-Path $ts)) { throw "tailscale.exe not found after install" }
Ok "$(& $ts version 2>&1)"

$svc = Get-Service Tailscale -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne 'Running') { Start-Service Tailscale; Start-Sleep -Seconds 5 }

Step "Connecting to the tailnet"
$statusRaw = & $ts status --json 2>&1 | Out-String
$state = ''
try { $state = ($statusRaw | ConvertFrom-Json).BackendState } catch { }
if ($state -ne 'Running') {
    $outLog = Join-Path $env:TEMP 'tsup.out.txt'
    $errLog = Join-Path $env:TEMP 'tsup.err.txt'
    Remove-Item $outLog, $errLog -ErrorAction SilentlyContinue
    Start-Process -FilePath $ts -ArgumentList @('up', "--hostname=$Hostname", '--accept-dns=false') `
        -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    $authUrl = $null
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $txt = ''
        foreach ($f in @($outLog, $errLog)) { if (Test-Path $f) { $txt += (Get-Content $f -Raw) } }
        $m = [regex]::Match($txt, 'https://login\.tailscale\.com/\S+')
        if ($m.Success) { $authUrl = $m.Value.Trim(); break }
    }
    if ($authUrl) {
        Write-Host ""
        Write-Host "==================================================" -ForegroundColor Yellow
        Write-Host " Buka URL ini di browser dan login (Google/GitHub):" -ForegroundColor Yellow
        Write-Host " $authUrl" -ForegroundColor White
        Write-Host "==================================================" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "Setelah login, jalankan skrip ini sekali lagi." -ForegroundColor Cyan
    return
}
Ok "tailnet connected"

Step "Enabling Funnel"
# `tailscale funnel` blocks forever when the tailnet has not enabled Funnel yet,
# so run it with a timeout and surface the activation URL instead of hanging.
$outFile = Join-Path $env:TEMP 'funnel.out.txt'
$errFile = Join-Path $env:TEMP 'funnel.err.txt'
Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $ts -ArgumentList @('funnel', '--bg', "$Port") -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $outFile -RedirectStandardError $errFile
$exited = $proc.WaitForExit(45000)
if (-not $exited) { try { $proc.Kill() } catch { } ; Start-Sleep -Seconds 2 }
$funnelOut = ''
foreach ($f in @($outFile, $errFile)) { if (Test-Path $f) { $funnelOut += (Get-Content $f -Raw) } }
$funnelOut = $funnelOut.Trim()
if ($funnelOut) { Write-Host $funnelOut }

$activate = [regex]::Match($funnelOut, 'https://login\.tailscale\.com/f/funnel\S*')
if ($activate.Success) {
    Write-Host ""
    Write-Host "==================================================" -ForegroundColor Yellow
    Write-Host " Funnel belum aktif di tailnet ini." -ForegroundColor Yellow
    Write-Host " Buka URL di bawah, klik Enable Funnel, lalu jalankan" -ForegroundColor Yellow
    Write-Host " skrip ini sekali lagi:" -ForegroundColor Yellow
    Write-Host " $($activate.Value)" -ForegroundColor White
    Write-Host "==================================================" -ForegroundColor Yellow
    return
}

$funnelStatus = & $ts funnel status 2>&1 | Out-String
Write-Host $funnelStatus

$m = [regex]::Match($funnelStatus, 'https://[A-Za-z0-9.-]+\.ts\.net')
if (-not $m.Success) { $m = [regex]::Match($funnelOut, 'https://[A-Za-z0-9.-]+\.ts\.net') }
if ($m.Success) {
    $publicUrl = $m.Value
    New-Item -ItemType Directory -Path (Join-Path $InstallDir 'data') -Force | Out-Null
    $publicUrl | Out-File -FilePath (Join-Path $InstallDir 'data\public_url.txt') -Encoding ascii
    Ok $publicUrl
    Write-Host ""
    Write-Host " Public URL : $publicUrl  (tetap setelah reboot)" -ForegroundColor Green
} else {
    Warn "Funnel belum aktif. Biasanya karena belum mengaktifkan HTTPS/Funnel di admin console:"
    Write-Host "   1. https://login.tailscale.com/admin/dns  -> aktifkan HTTPS Certificates"
    Write-Host "   2. https://login.tailscale.com/admin/acls  -> tambahkan node attribute funnel"
    Write-Host "   3. jalankan lagi: tailscale funnel --bg $Port"
}
