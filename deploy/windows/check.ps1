#requires -Version 5.1
# EarnApp Monitor - RDP readiness check (read-only, no admin required)
# Usage:
#   powershell -ExecutionPolicy Bypass -File check.ps1

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

function Row([string]$k, $v) { "{0,-28} {1}" -f $k, $v }

function Test-Tcp([string]$h, [int]$p, [int]$ms = 3000) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $t = $c.ConnectAsync($h, $p)
        if ($t.Wait($ms) -and $c.Connected) { return $true }
        return $false
    } catch { return $false } finally { $c.Dispose() }
}

Write-Host ""
Write-Host "=== SYSTEM ===" -ForegroundColor Cyan
$os  = Get-CimInstance Win32_OperatingSystem
$cs  = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$load = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average

Row "OS"            ("{0} {1} ({2})" -f $os.Caption, $os.Version, $os.OSArchitecture)
Row "Computer"      $env:COMPUTERNAME
Row "User"          ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME)
Row "CPU"           ("{0} | {1}C/{2}T | load {3}%" -f $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors, $load)
Row "RAM total"     ("{0:N0} MB" -f ($os.TotalVisibleMemorySize / 1024))
Row "RAM free"      ("{0:N0} MB" -f ($os.FreePhysicalMemory / 1024))
Row "RAM used"      ("{0:N0} MB" -f (($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1024))
Row "Uptime"        ((Get-Date) - $os.LastBootUpTime).ToString("d\d\ hh\:mm")
$disk = Get-PSDrive C
Row "Disk C: free"  ("{0:N1} GB" -f ($disk.Free / 1GB))

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Row "Administrator" $(if ($admin) { "YES" } else { "NO" })

Write-Host ""
Write-Host "=== RUNTIME (what is already installed) ===" -ForegroundColor Cyan
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    Row "python" $py.Source
    Row "python version" ((& python --version 2>&1) -join ' ')
} else { Row "python" "NOT FOUND" }

$pyL = Get-Command py -ErrorAction SilentlyContinue
if ($pyL) {
    Row "py launcher" $pyL.Source
    Row "py versions" ((& py -0p 2>&1) -join '; ')
} else { Row "py launcher" "NOT FOUND" }

$git = Get-Command git -ErrorAction SilentlyContinue
Row "git" $(if ($git) { $git.Source } else { "NOT FOUND (zip download will be used)" })

$wg = Get-Command winget -ErrorAction SilentlyContinue
Row "winget" $(if ($wg) { "available" } else { "NOT FOUND" })

$ts = Get-Command tailscale -ErrorAction SilentlyContinue
Row "tailscale" $(if ($ts) { $ts.Source } else { "NOT FOUND" })

Write-Host ""
Write-Host "=== NETWORK / NAT (decides if public IP + port is possible) ===" -ForegroundColor Cyan
$localIps = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*'
} | Select-Object -ExpandProperty IPAddress)
Row "Local IPv4" ($localIps -join ', ')

$pub = $null
foreach ($svc in @('https://api.ipify.org?format=json', 'https://ifconfig.me/ip')) {
    try {
        if ($svc -like '*ipify*') { $pub = (Invoke-RestMethod -Uri $svc -TimeoutSec 10).ip }
        else { $pub = (Invoke-WebRequest -Uri $svc -UseBasicParsing -TimeoutSec 10).Content.Trim() }
        if ($pub) { break }
    } catch { }
}
Row "Public IP" $(if ($pub) { $pub } else { "unknown (outbound HTTP blocked?)" })

$natMode = "UNKNOWN"
if ($pub -and $localIps.Count -gt 0) {
    if ($localIps -contains $pub) { $natMode = "PUBLIC (no NAT) - port forwarding possible" }
    else { $natMode = "BEHIND NAT - cannot open port from outside" }
}
Row "NAT" $natMode

Write-Host ""
Write-Host "=== PORTS IN USE ===" -ForegroundColor Cyan
foreach ($p in @(8080, 80, 443, 3001)) {
    $l = @(Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
    if ($l.Count -gt 0) {
        $procName = (Get-Process -Id $l[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Row "port $p" ("IN USE by {0} (PID {1})" -f $procName, $l[0].OwningProcess)
    } else { Row "port $p" "free" }
}

Write-Host ""
Write-Host "=== OUTBOUND CONNECTIVITY (TCP 443) ===" -ForegroundColor Cyan
$targets = @(
    'github.com',
    'raw.githubusercontent.com',
    'pypi.org',
    'earnapp.com',
    'ep-small-shape-a1qyrtfk-pooler.ap-southeast-1.aws.neon.tech'
)
foreach ($h in $targets) { Row $h (Test-Tcp $h 443) }

Write-Host ""
Write-Host "=== WHAT IS CONSUMING THE RAM ===" -ForegroundColor Cyan
Get-Process | Sort-Object WorkingSet64 -Descending | Select-Object -First 8 |
    ForEach-Object { Row $_.ProcessName ("{0:N0} MB" -f ($_.WorkingSet64 / 1MB)) }

$vm = @(Get-Process | Where-Object { $_.ProcessName -match 'vmware|vbox|qemu|vmcompute|vmwp|prl_' })
Row "VM workload" $(if ($vm.Count -gt 0) { ($vm | Group-Object ProcessName | ForEach-Object { "$($_.Name) x$($_.Count)" }) -join ', ' } else { "none detected" })

Write-Host ""
Write-Host "=== VERDICT ===" -ForegroundColor Cyan
$freeMb = [math]::Round($os.FreePhysicalMemory / 1024)
if ($freeMb -ge 400) { Write-Host "RAM: OK for dashboard (~150 MB needed, $freeMb MB free)" -ForegroundColor Green }
elseif ($freeMb -ge 200) { Write-Host "RAM: TIGHT but possible (~150 MB needed, $freeMb MB free)" -ForegroundColor Yellow }
else { Write-Host "RAM: TOO LOW ($freeMb MB free). Free up memory first or use another host." -ForegroundColor Red }

if ($natMode -like 'PUBLIC*') { Write-Host "Access: public IP + port is possible (needs firewall rule + admin)" -ForegroundColor Green }
else { Write-Host "Access: NAT detected. Use Tailscale Funnel (option 2)." -ForegroundColor Yellow }

Write-Host ""
Write-Host "Copy everything above and send it back." -ForegroundColor Cyan
Write-Host ""
