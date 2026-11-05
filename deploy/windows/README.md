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
4. tulis `.env` (Neon, Basic Auth, `LOGIN_HELPER=0`, `AUTO_SYNC=1`, `AUTO_POLL=0`)
5. jalankan dashboard di port `8080`, cek `/ping`
6. daftarkan auto-start: **ONSTART sebagai SYSTEM** bila admin, **ONLOGON** bila tidak
7. buka firewall TCP 8080 bila admin

Hasil: `http://127.0.0.1:8080/` (user `admin`).

## 3. Akses dari luar

### Opsi A — IP publik + HTTPS (butuh admin + port 80/443 terbuka)

```powershell
irm https://raw.githubusercontent.com/lukmandroidxxx/earnapp-monitor/main/deploy/windows/setup-caddy.ps1 -OutFile "$env:USERPROFILE\eam-caddy.ps1"
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\eam-caddy.ps1"
```

Memakai `IP-PUBLIK.sslip.io` (tanpa domain sendiri) + sertifikat Let's Encrypt otomatis.
Kalau log Caddy menunjukkan timeout ACME, berarti provider memblokir 80/443 → pakai Opsi B.

### Opsi B — Tailscale Funnel (tetap, tanpa buka port)

1. Install Tailscale di RDP: <https://tailscale.com/download/windows> (butuh admin)
2. Login pakai akun Google
3. Di admin console Tailscale → **DNS** → aktifkan **HTTPS Certificates**
4. Di PowerShell RDP:

```powershell
tailscale funnel --bg 8080
tailscale funnel status
```

URL tetap bentuknya `https://<nama-mesin>.<tailnet>.ts.net` dan tetap hidup setelah reboot.

## Operasional

| Aksi | Perintah |
|---|---|
| Lihat status | `schtasks /Query /TN EarnAppMonitor /V /FO LIST` |
| Mulai ulang | `schtasks /End /TN EarnAppMonitor; schtasks /Run /TN EarnAppMonitor` |
| Log error | `Get-Content "$env:USERPROFILE\earnapp-monitor\data\server.err.log" -Tail 50` |
| Log Caddy | `Get-Content "$env:USERPROFILE\earnapp-monitor\tools\caddy\caddy.log" -Tail 50` |
| Update kode | jalankan ulang `setup-app.ps1` (`.env` lama otomatis di-backup) |

## Catatan

- `LOGIN_HELPER=0` karena RDP headless; login Google manual tetap bisa dilakukan lewat **import cookie** di menu akun.
- `AUTO_POLL=0` → cek slot & sync device manual dari dashboard, hemat CPU.
- Kalau RDP di-reboot dan tidak ada yang login, auto-start hanya jalan bila task dibuat sebagai SYSTEM (admin).
- Jangan taruh `.env`, cookie, atau `secrets/` di repo publik.
