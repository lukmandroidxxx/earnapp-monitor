# Deploy 24/7 (tanpa PC/laptop ini menyala)

Panduan ini membuat dashboard tetap online di server yang selalu aktif. Setelah
deploy, PC/laptop ini boleh dimatikan; server yang menjalankan dashboard, sync
sheet, dan pengecekan proxy.

Ada beberapa jalur:

| Jalur | Cocok untuk | Biaya |
|---|---|---|
| **Oracle Cloud Always Free** + Docker + Caddy | Gratis, online 24/7, URL tetap | Gratis (perlu kartu untuk verifikasi) |
| **VPS murah** + Docker + Caddy | Paling mudah, tidak perlu kartu Oracle | ± $3–6/bulan |
| **Cloudflare Quick Tunnel** di server | Tidak bisa membuka port 80/443 / tidak punya domain | Gratis, URL acak |
| Raspberry Pi / mini PC di rumah | Sudah punya perangkat selalu menyala | Listrik + internet rumah |

> **Tidak disarankan:** PaaS gratis seperti Render/Koyeb untuk aplikasi ini.
> Free tier biasanya tidur setelah tidak dipakai dan filesystem-nya sementara,
> sedangkan aplikasi ini butuh SQLite + folder `secrets/` yang persisten.

---

## Jalur cepat (satu perintah di server)

Kalau server Ubuntu sudah bisa di-SSH, langkah 1–6 di bawah bisa diringkas.
Upload `dist/earnapp-monitor-deploy.zip`, ekstrak, lalu jalankan:

```bash
unzip -o earnapp-monitor-deploy.zip -d ~/earnapp-monitor
cd ~/earnapp-monitor
sudo -E bash deploy/setup-server.sh
```

Dari Windows (jalankan di folder project ini, bukan di server), langkah upload +
ekstrak bisa otomatis (termasuk memindahkan database dan `secret.key` kalau pakai
`-MigrateData`):

```powershell
cd "C:\Users\Admin\Documents\Default Project\earnapp-monitor"
powershell -ExecutionPolicy Bypass -File .\tools\deploy-to-server.ps1 `
  -Server IP-PUBLIK-ANDA -Key C:\path\ke\private.key -MigrateData
```

Script akan menampilkan perintah SSH terakhir untuk menjalankan
`deploy/setup-server.sh`. Script setup menanyakan domain (default
`<IP-publik>.sslip.io`) dan password, memasang Docker, membuka port 80/443 di
firewall lokal, membuat `.env`, lalu menjalankan app + Caddy. Kalau password
dikosongkan, script membuat password acak dan menampilkannya di akhir.

Yang tetap harus manual: membuka port 80/443 di **security list/VCN** cloud
(Oracle), dan memindahkan database lama kalau diinginkan (langkah 4).

---

## 1. Buat server Oracle Cloud Always Free

1. Daftar di <https://cloud.oracle.com/free>. Kartu dipakai untuk verifikasi;
   pakai shape **Always Free** supaya tidak ditagih.
2. Buat instance:
   - **Image:** Ubuntu 24.04 (atau 22.04).
   - **Shape:** `VM.Standard.A1.Flex` (ARM, Always Free hingga 4 OCPU / 24 GB)
     atau `VM.Standard.E2.1.Micro` (AMD, 1 GB RAM) kalau ARM tidak tersedia.
   - **Networking:** buat VCN + public subnet, centang **Assign a public IPv4 address**.
   - Simpan private key SSH (`.key`).
3. Catat **Public IP** instance. Sebaiknya reserve IP tersebut:
   **Networking → Public IPs → Reserve**.

> Kalau ARM selalu "out of capacity", coba availability domain lain atau pakai
> `E2.1.Micro`. Aplikasi ini ringan karena login helper (Playwright) dimatikan di server.

### Buka port 80 dan 443

**Di Oracle Console:** Networking → Virtual Cloud Networks → VCN Anda →
Security Lists → Default Security List → **Add Ingress Rules**:

| Source CIDR | IP Protocol | Destination Port Range |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

Port `22` (SSH) biasanya sudah ada. Jangan buka `8080` ke internet.

**Di dalam VM Ubuntu** (Oracle images punya firewall sendiri):

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Cek dari komputer lain:

```bash
curl -I http://IP-PUBLIK-ANDA
```

Kalau masih timeout, berarti aturan VCN atau firewall VM belum benar.

---

## 2. Install Docker di server

SSH ke server:

```bash
ssh -i path\ke\private.key ubuntu@IP-PUBLIK-ANDA
```

Install Docker Engine + Compose plugin:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
docker compose version
```

---

## 3. Upload project

Di Windows, buat arsip deploy (tanpa `.venv`, `data`, `secrets`, dan file besar):

```powershell
cd "C:\Users\Admin\Documents\Default Project\earnapp-monitor"
powershell -ExecutionPolicy Bypass -File .\tools\package-deploy.ps1
```

Hasilnya `dist\earnapp-monitor-deploy.zip`. Upload ke server:

```powershell
scp .\dist\earnapp-monitor-deploy.zip ubuntu@IP-PUBLIK-ANDA:~/
```

Di server:

```bash
sudo apt-get install -y unzip
unzip -o earnapp-monitor-deploy.zip -d earnapp-monitor
cd earnapp-monitor
```

Kalau tidak memakai zip, upload folder project dengan `scp`/WinSCP, tetapi
**jangan** ikutkan `.venv`, `data`, `secrets`, dan `tools\cloudflared.exe`.

---

## 4. Pindahkan data lama (opsional, disarankan)

Kalau ingin cookie, akun, dan riwayat yang sudah ada ikut pindah, salin database
dan kunci enkripsi. Database berisi cookie terenkripsi; tanpa `secret.key` yang
sama, cookie tidak bisa dibaca.

**Di Windows**, buat backup SQLite yang aman walaupun dashboard lokal masih jalan:

```powershell
cd "C:\Users\Admin\Documents\Default Project\earnapp-monitor"
.\.venv\Scripts\python.exe -c "import sqlite3; s=sqlite3.connect(r'data\monitor.db'); d=sqlite3.connect(r'data\monitor-backup.db'); s.backup(d); d.close(); s.close()"
scp .\data\monitor-backup.db ubuntu@IP-PUBLIK-ANDA:~/earnapp-monitor/data/monitor.db
scp .\secrets\secret.key ubuntu@IP-PUBLIK-ANDA:~/earnapp-monitor/secrets/secret.key
```

Di server:

```bash
cd ~/earnapp-monitor
chmod 700 data secrets
chmod 600 secrets/secret.key
```

Kalau **tidak** memindahkan database, server akan mulai dari kosong: sync sheet
dulu, lalu tempel cookie JSON per akun (lihat bagian 7).

Alternatif: isi `SECRET_KEY` di `.env` dengan isi `secrets/secret.key` dari
Windows, lalu cukup salin database. Jangan mengganti nilai `SECRET_KEY` setelah
cookie tersimpan.

---

## 5. Konfigurasi `.env`

```bash
cd ~/earnapp-monitor
cp .env.example .env
nano .env
```

Minimal yang perlu diubah:

```dotenv
GOOGLE_SHEET_ID=1CI1p_r8tGFztywALNbIT9yjjaMcwK_sphOhZ-Gnr8nA
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=ganti-dengan-password-panjang-dan-unik

# Domain HTTPS untuk Caddy. Tanpa beli domain, pakai IP + .sslip.io:
#   203.0.113.10.sslip.io
DOMAIN=IP-PUBLIK-ANDA.sslip.io

# Server tidak punya desktop, jadi helper browser dimatikan.
LOGIN_HELPER=0

# Sheet sync otomatis.
AUTO_SYNC=1

# 0 = cek proxy/device hanya lewat tombol.
# 1 = cek semua slot + sync device otomatis tiap POLL_INTERVAL_SECONDS.
AUTO_POLL=0
POLL_INTERVAL_SECONDS=900

DEVICE_SYNC=1
DEVICE_USAGE_DAYS=14
SCRAPER_MODE=off
EARN_APP_API_BASE=https://earnapp.com/dashboard/api
EARN_APP_APP_VERSION=1.685.135
```

`DOMAIN` dibaca oleh Docker Compose, bukan oleh aplikasi. Contoh kalau IP publik
`203.0.113.10`, isinya `203.0.113.10.sslip.io`. DNS `sslip.io` otomatis
mengarahkan hostname itu ke IP tersebut, dan Caddy akan membuat sertifikat HTTPS
Let's Encrypt secara otomatis.

Amankan file:

```bash
chmod 600 .env
```

Kalau `.env` dari Windows ikut terbawa, pastikan tidak ada `HOST=127.0.0.1`;
Compose sudah memaksa container mendengar di `0.0.0.0`.

---

## 6. Jalankan

```bash
cd ~/earnapp-monitor
docker compose up -d --build
docker compose ps
docker compose logs -f app
```

Tunggu Caddy membuat sertifikat (10–60 detik), lalu buka:

```
https://IP-PUBLIK-ANDA.sslip.io
```

Browser akan meminta HTTP Basic Auth. Gunakan `DASHBOARD_USER` dan
`DASHBOARD_PASSWORD` dari `.env`.

Cek dari terminal:

```bash
curl -I https://IP-PUBLIK-ANDA.sslip.io
# 401 = server hidup dan minta login (normal)
```

Kalau `502`, lihat `docker compose logs app`. Kalau sertifikat gagal, lihat
`docker compose logs caddy` dan pastikan port 80/443 benar-benar terbuka.

---

## 7. Login/cookie di server tanpa desktop

Login helper membuka jendela Chrome di mesin yang menjalankan server. Di VPS
headless itu tidak praktis, jadi `LOGIN_HELPER=0`. Cookie yang sudah ada tetap
bisa dipakai sampai kedaluwarsa.

Cara memperbarui cookie:

1. Di PC/laptop ini, jalankan dashboard lokal seperti biasa.
2. Login lewat **Buka browser login** sampai masuk dashboard EarnApp.
3. Buka halaman akun, klik **Download cookie JSON**.
4. Di dashboard server, buka akun yang sama, tempel JSON tersebut ke kotak
   cookie, lalu klik **Simpan & test login**.

Alternatif: login di Chrome biasa, export cookie `earnapp.com` dengan
**Cookie-Editor**, lalu tempel JSON-nya di dashboard server.

Cookie hanya perlu diperbarui kalau statusnya `COOKIE EXPIRED` atau
`LOGIN ERROR` karena sesi benar-benar habis.

---

## 8. Alternatif: Cloudflare Quick Tunnel (tanpa buka port/domain)

Kalau port 80/443 tidak bisa dibuka atau tidak ingin memakai domain, gunakan
tunnel. Jalankan **salah satu** jalur, jangan bersamaan dengan Compose utama:

```bash
cd ~/earnapp-monitor/deploy
docker compose -f docker-compose.tunnel.yml up -d --build
docker compose -f docker-compose.tunnel.yml logs -f cloudflared
```

Cari URL seperti `https://xxxx-xxxx-xxxx.trycloudflare.com` di log. URL ini
**acak dan berubah setiap container cloudflared di-restart**. Untuk URL tetap,
perlu Cloudflare named tunnel (akun Cloudflare + domain).

Hentikan Compose utama dulu supaya SQLite tidak dipakai dua instance:

```bash
cd ~/earnapp-monitor
docker compose down
```

---

## 9. Update dan backup

Update kode:

```bash
cd ~/earnapp-monitor
# upload kode baru / git pull
docker compose up -d --build
```

Backup database (aman saat aplikasi berjalan):

```bash
cd ~/earnapp-monitor
docker compose exec -T app python -c \
  "import sqlite3; s=sqlite3.connect('/app/data/monitor.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp app:/app/data/backup.db ./monitor-backup.db
docker compose exec app rm /app/data/backup.db

tar czf "earnapp-backup-$(date +%F).tar.gz" monitor-backup.db secrets/secret.key .env
```

Simpan hasil backup di tempat aman. File itu berisi cookie terenkripsi dan kunci
enkripsinya.

---

## 10. Keamanan

- Gunakan `DASHBOARD_PASSWORD` yang panjang dan unik; jangan sama dengan password
  lain.
- Jangan pernah membuka port `8080` ke internet. Hanya Caddy (80/443) yang publik.
- Jangan commit `.env`, `data/`, atau `secrets/`. Ketiganya sudah ada di
  `.gitignore`.
- Batasi SSH ke IP Anda kalau bisa, dan jalankan update OS:
  `sudo apt update && sudo apt upgrade -y`.
- HTTPS wajib; jangan pakai tunnel dengan `DASHBOARD_PASSWORD` kosong.
- Sheet pool berisi kredensial proxy. Dashboard hanya butuh akses baca; batasi
  siapa yang bisa membuka sheet.

---

## Troubleshooting singkat

| Gejala | Cek |
|---|---|
| `https://...sslip.io` timeout | VCN ingress 80/443 dan firewall `iptables` di VM |
| Sertifikat Caddy gagal | `docker compose logs caddy`; DNS `nslookup IP.sslip.io` harus ke IP server |
| `502 Bad Gateway` | `docker compose logs app`; container `app` harus `healthy` |
| `401 Login required` | Password/user Basic Auth salah |
| Data device tidak update | `AUTO_POLL=0` berarti manual; klik Refresh, atau set `AUTO_POLL=1` |
| Cookie hilang setelah pindah | `secret.key`/`SECRET_KEY` berbeda dengan database lama |
| `database is locked` | Ada dua container app memakai `data/monitor.db` yang sama |
| Build kehabisan RAM (1 GB) | Tambah swap 1–2 GB atau pakai shape ARM yang lebih besar |
