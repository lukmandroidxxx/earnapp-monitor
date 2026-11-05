# Deploy di RDP Windows (EarnApp sudah jalan di sana)

Dashboard ini sangat ringan: **~44 MB RAM** dan **~0% CPU** saat idle (diukur dari proses nyata).
Aman dijalankan berdampingan dengan VM EarnApp selama masih ada **±150 MB RAM kosong**.

## 1. Cek dulu (tanpa admin, read-only)

Buka **PowerShell** di RDP, lalu:

```powershell
irm https://raw.githubusercontent.com/lukmandroidxxx/earnapp-monitor/main/deploy/windows/check.ps1 -OutFile "$env:USERPROFILE\eam-check.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\eam-check.ps1"
```

Kirim output-nya. Yang penting:
- `RAM free` → minimal 150 MB, ideal 400 MB+
- `NAT` → kalau `BEHIND NAT`, opsi IP publik + port tidak mungkin, langsung pakai Tailscale
- `Administrator` → menentukan auto-start saat boot dan pembukaan firewall
- `python` → kalau `NOT FOUND`, installer akan memasang Python 3.12 per-user

## 2. Pasang aplikasi

```powershell
irm https://raw.githubusercontent.com/lukmandroidxxx/earnapp-monitor/main/deploy/windows/setup-app.ps1 -OutFile "$env:USERPROFILE\eam-setup.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\eam-setup.ps1"
```

Skrip akan menanyakan:

| Nilai | Keterangan |
|---|---|
| `DATABASE_URL` | `postgresql+psycopg://...neon.tech/neondb?sslmode=require` |
| `SECRET_KEY` | **harus sama** dengan key lama, supaya cookie tersimpan bisa didekripsi |
| `DASHBOARD_PASSWORD` | password Basic Auth dashboard |

Yang dilakukan skrip:

1. unduh kode dari GitHub (tanpa perlu git)
2. pasang Python 3.12 per-user bila belum ada
3. buat `.venv` + `pip install -r requirements.txt`
4. tulis `.env` (Neon, Basic Auth, `LOGIN_HELPER=0`, `AUTO_SYNC=1`, `AUTO_POLL=0`, `DEVICE_SYNC=1`)
5. jalankan dashboard di port `8080`, cek `/ping`
6. daftarkan auto-start: **ONSTART sebagai SYSTEM** bila admin, **ONLOGON** bila tidak
7. buka firewall TCP 8080 bila admin

Hasil: `http://127.0.0.1:8080/` (user `admin`).

## 3. Akses dari luar

Kalau server ada di belakang NAT provider (hanya port RDP yang diteruskan, cek dengan
`Test-NetConnection -ComputerName <IP-PUBLIK> -Port 8080`), pakai **Opsi C**.

### Opsi C — Cloudflare quick tunnel (tanpa akun, tanpa buka port) ← dipakai saat ini

```powershell
irm https://raw.githubusercontent.com/lukmandroidxxx/earnapp-monitor/main/deploy/windows/setup-tunnel.ps1 -OutFile "$env:USERPROFILE\eam-tunnel.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\eam-tunnel.ps1"
```

Menghasilkan URL publik `https://<acak>.trycloudflare.com` dan task `EarnAppMonitorTunnel`
yang jalan saat boot. URL terbaru disimpan di
`%USERPROFILE%\earnapp-monitor\data\public_url.txt`.

Kekurangan: URL berganti setiap kali tunnel/VM restart. Kalau butuh URL tetap, pakai
Opsi A (kalau provider mau membuka port 80/443) atau Opsi B.

### Opsi A — IP publik + HTTPS (butuh admin + port 80/443 terbuka)

```powershell
irm https://raw.githubusercontent.com/lukmandroidxxx/earnapp-monitor/main/deploy/windows/setup-caddy.ps1 -OutFile "$env:USERPROFILE\eam-caddy.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\eam-caddy.ps1"
```

Memakai `IP-PUBLIK.sslip.io` (tanpa domain sendiri) + sertifikat Let's Encrypt otomatis.
Kalau log Caddy menunjukkan timeout ACME, berarti provider memblokir 80/443 → pakai Opsi B.

### Opsi B — Tailscale Funnel (tetap, tanpa buka port)

```powershell
irm https://raw.githubusercontent.com/lukmandroidxxx/earnapp-monitor/main/deploy/windows/setup-funnel.ps1 -OutFile "$env:USERPROFILE\eam-funnel.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\eam-funnel.ps1"
```

Skrip memasang Tailscale, menghubungkan mesin sebagai `earnapp-monitor`, lalu
menjalankan `tailscale funnel --bg 8080`. Pada run pertama ia mencetak URL login
(`https://login.tailscale.com/a/...`) — buka, login dengan Google/GitHub, lalu
jalankan skrip sekali lagi.

Kalau Funnel belum aktif, aktifkan dulu di admin console:
1. <https://login.tailscale.com/admin/dns> → **HTTPS Certificates** → Enable
2. <https://login.tailscale.com/admin/acls> → tambahkan attribute `funnel` untuk node ini
3. `tailscale funnel --bg 8080`

URL tetap bentuknya `https://earnapp-monitor.<tailnet>.ts.net` dan tetap hidup setelah reboot.

Kalau `tailscale funnel` menggantung atau menampilkan *"Funnel is not enabled on your tailnet"*,
buka link `https://login.tailscale.com/f/funnel?node=...` yang dicetak, klik **Enable Funnel**,
lalu jalankan skrip/perintah sekali lagi. Konfigurasi Funnel tersimpan di state `tailscaled`,
jadi tetap aktif setelah service restart maupun reboot (tidak perlu scheduled task tambahan).

## Operasional

| Aksi | Perintah |
|---|---|
| Lihat status | `schtasks /Query /TN EarnAppMonitor /V /FO LIST` |
| Mulai ulang | `schtasks /End /TN EarnAppMonitor; schtasks /Run /TN EarnAppMonitor` |
| Log error | `Get-Content "$env:USERPROFILE\earnapp-monitor\data\server.err.log" -Tail 50` |
| Log Caddy | `Get-Content "$env:USERPROFILE\earnapp-monitor\tools\caddy\caddy.log" -Tail 50` |
| URL publik sekarang | `Get-Content "$env:USERPROFILE\earnapp-monitor\data\public_url.txt"` |
| Restart tunnel | `schtasks /End /TN EarnAppMonitorTunnel; schtasks /Run /TN EarnAppMonitorTunnel` |
| Update kode | jalankan ulang `setup-app.ps1` (`.env` lama otomatis di-backup) |

## Catatan

- `LOGIN_HELPER=0` karena RDP headless; login Google manual tetap bisa dilakukan lewat **import cookie** di menu akun.
- `AUTO_POLL=1` dengan `POLL_INTERVAL_SECONDS=1800` dan `POLL_CONCURRENCY=4`: cek slot + sync device tiap 30 menit, ringan untuk RAM 16 GB yang hampir penuh.
- Kalau RDP di-reboot dan tidak ada yang login, auto-start hanya jalan bila task dibuat sebagai SYSTEM (admin).
- Jangan taruh `.env`, cookie, atau `secrets/` di repo publik.
