# EarnApp Multi-Account Monitor

Dashboard monitoring pool proxy EarnApp: status tiap device slot, usage per akun,
guard IP unik, dan status login (cookie) per akun.

Selain data sheet, dashboard juga menarik **data EarnApp per akun** — device,
status online/offline, usage harian, saldo, dan device/IP yang kena banned — dari
halaman <https://earnapp.com/dashboard/me/passive-income/bandwidth>. **Mode grafik
memakai data EarnApp ini, bukan IP/Count di sheet.**

Sumber data pool tetap **Google Sheet** — dashboard tidak pernah menulis balik kecuali
`SHEET_WRITEBACK=1`.

## Yang dimonitor

| Data | Sumber |
|---|---|
| Daftar akun | kolom `Account` |
| Device slot (1 baris = 1 proxy) | kolom `Proxy IP` + `Proxy Command` |
| Proxy hidup / mati, latency | cek langsung lewat proxy |
| IP keluar sesuai sheet | perbandingan IP hasil cek vs `Proxy IP` |
| IP bentrok antar akun | unique constraint DB + deteksi runtime |
| Usage per slot | kolom `Count` |
| Device EarnApp (online/offline, usage harian) | API EarnApp `/devices`, `/usage`, `/device_statuses` |
| Device/IP banned | field `banned` di API EarnApp `/devices` |
| Saldo / lifetime / node online | API EarnApp `/money` (butuh cookie) |
| Uptime 24 jam | riwayat cek tersimpan |

## Jalankan

Windows (PowerShell):

```powershell
cd earnapp-monitor
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# edit .env, minimal isi GOOGLE_SHEET_ID
python run.py
```

Buka <http://127.0.0.1:8080>.

Run pertama disarankan:

```
AUTO_SYNC=0
AUTO_POLL=0
```

lalu klik **Sync sheet** dan **Cek semua slot** manual, supaya 92 proxy tidak
langsung dicek bersamaan saat server start.

## Akses dari luar

### 24/7 tanpa PC/laptop ini menyala

Quick Tunnel di bawah **hanya hidup selama PC ini menyala**. Kalau ingin dashboard
online terus tanpa menyalakan PC/laptop, deploy ke server yang selalu aktif:
**Oracle Cloud Always Free** (rekomendasi), VPS murah, atau Raspberry Pi/mini PC
di rumah. Project ini sudah dilengkapi `Dockerfile`, `docker-compose.yml`, Caddy
untuk HTTPS, dan panduan langkah demi langkah di
**[deploy/README.md](deploy/README.md)**. Di server Ubuntu, setelah zip diekstrak,
cukup jalankan `sudo -E bash deploy/setup-server.sh`.

### Sementara dari PC ini (Cloudflare Quick Tunnel)

Dashboard tetap berjalan di `127.0.0.1:8080`. Untuk membukanya dari HP/laptop lain
tanpa biaya, pakai **Cloudflare Quick Tunnel** — gratis, tanpa akun Cloudflare.
Cloudflare yang menghubungkan PC ini ke internet dan menyediakan HTTPS.

### 1. Wajib: pasang password dashboard

Isi di `.env` sebelum mengekspos dashboard:

```
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=password-panjang-anda
```

Selama `DASHBOARD_PASSWORD` terisi, semua halaman dilindungi **HTTP Basic Auth**
(browser akan menampilkan kotak login). Kalau kosong, dashboard terbuka tanpa
login — hanya aman untuk lokal.

Restart server setelah mengubah `.env`.

### 2. Nyalakan tunnel

```powershell
cd earnapp-monitor
powershell -ExecutionPolicy Bypass -File .\tools\remote.ps1
```

Pertama kali script mengunduh `tools/cloudflared.exe` (± 40 MB). Setelah kurang
lebih 10 detik muncul:

```
URL      : https://xxxx-xxxx-xxxx.trycloudflare.com
Username : admin
Password : ...
```

Buka URL tersebut dari perangkat mana pun. URL juga disimpan di
`data/remote_url.txt`.

### 3. Cek status / hentikan

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\remote.ps1 -Status
powershell -ExecutionPolicy Bypass -File .\tools\remote.ps1 -Stop
```

Log tunnel ada di `data/cloudflared.out.log` dan `data/cloudflared.err.log`,
PID di `data/cloudflared.pid`.

### Peringatan

- URL Quick Tunnel **acak dan berubah setiap tunnel di-restart**. Untuk URL tetap
  perlu Cloudflare named tunnel (butuh domain) atau alternatif seperti Tailscale.
- Siapa pun yang tahu URL masih bisa mencoba menebak password. Pakai password
  panjang, dan hentikan tunnel (`-Stop`) kalau tidak dipakai.
- Jangan pernah menyalakan tunnel dengan `DASHBOARD_PASSWORD` kosong.
- Sheet tetap publik dan berisi kredensial proxy; batasi akses sheet dan rotasi
  kredensial bila perlu.
- Alternatif gratis lain: **Tailscale** (privat, perlu install & login di tiap
  perangkat), **ngrok** (perlu akun), **localtunnel** (perlu Node, kurang stabil).

### Cloudflare error 1033

Error 1033 berarti Cloudflare tidak menemukan koneksi tunnel yang aktif — biasanya
karena Internet/Wi-Fi sempat putus, PC sleep, atau cloudflared mati, bukan karena
dashboard-nya rusak. cloudflared umumnya menyambung ulang sendiri dalam beberapa
detik.

```powershell
# cek apakah proses & URL masih ada
powershell -ExecutionPolicy Bypass -File .\tools\remote.ps1 -Status

# kalau log menunjukkan QUIC sering timeout, coba paksa HTTP/2
powershell -ExecutionPolicy Bypass -File .\tools\remote.ps1 -Stop
powershell -ExecutionPolicy Bypass -File .\tools\remote.ps1 -Protocol http2
```

Pastikan PC tidak sleep dan opsi Windows **Allow the computer to turn off this
device to save power** untuk adapter Wi-Fi dimatikan. Log ada di
`data/cloudflared.err.log`.

## Cara kerja

1. **Sync sheet** (tiap 5 menit / tombol) → baca semua baris, buat akun + slot.
   - Satu baris = satu device slot = satu sesi proxy.
   - `Proxy Command` dibaca sebagai `host:port:user:pass` (atau URL proxy).
   - IP dan sesi proxy wajib unik; baris duplikat ditolak dan dilaporkan di dashboard.
2. **Cek slot** (tiap 15 menit / tombol) → tiap slot dites lewat proxynya sendiri.
   - Gagal → status `PERLU DIGANTI`.
   - IP keluar ≠ `Proxy IP` → `PERLU DIGANTI` + alasannya.
   - Dua slot keluar dari IP yang sama → `BENTROK IP`.
3. **Status akun** dihitung dari slotnya: `SEHAT`, `SEBAGIAN`, `BERMASALAH`,
   `BENTROK IP`, `NONAKTIF`, `BELUM DICEK`.
4. **Sync device EarnApp** (otomatis saat cek slot jika `DEVICE_SYNC=1`, atau tombol
   **Refresh data EarnApp** / **Refresh device**) → memakai cookie + proxy akun:
   - `/devices` → daftar device, `earned`, `rate`, uptime, dan `banned {reason, ip}`.
   - `/usage?step=daily` → usage harian per device (disimpan `DEVICE_USAGE_DAYS` hari).
   - `/device_statuses` (POST) → device mana yang online + waktu terakhir terlihat.
   - `/money` → saldo, lifetime, dan node online.
   - Device atau IP yang baru masuk banned memicu alert Telegram.

Tidak ada fallback ke IP server: kalau proxy mati, pengecekan akun berhenti,
bukan diam-diam memakai IP lain.

## Grafik dashboard

Mode **Grafik** (`/?view=grafik`) memakai data EarnApp semua akun, bukan IP/Count
di sheet:

- donut **online / offline / banned** dan gauge persentase online;
- bar **usage per akun** (1 hari dan 7 hari);
- **usage harian 7 hari** — default **bar**, bisa diganti ke **line** atau **area**;
- peta device per akun (hijau online, abu offline, merah banned);
- tabel device banned beserta alasan dan IP-nya.

Grafik usage per akun dan usage harian punya tombol **Bar / Line / Area** di kanan
judul kartu. Pilihan berlaku untuk kedua grafik, tersimpan di browser
(`localStorage`), jadi tetap sama setelah halaman dimuat ulang.

Nilai usage ditampilkan sebagai durasi ringkas `XdYH` tanpa komponen nol, misalnya
`7D`, `1D6H`, `5H`, atau `-` kalau kosong. Tooltip dan sumbu Y grafik memakai format
yang sama.

Halaman **Device EarnApp** (`/devices`) menampilkan semua device dengan filter akun
dan status. Di halaman akun, device yang IP banned-nya masih dipakai proxy akun
tersebut diberi peringatan.

## Menambah akun

Tambahkan baris baru di sheet dengan minimal:

```
Account | Proxy IP | Proxy Command
```

lalu klik **Sync sheet**. Akun baru otomatis dibuat. Cookie tidak wajib untuk
monitoring proxy, tapi **diperlukan** untuk grafik/device EarnApp (saldo, usage,
online, banned).

Cookie hanya perlu kalau ingin melihat saldo/node: satu cookie per akun. Cara
termudah adalah **Login otomatis** di halaman akun:

1. Klik **Buka browser login** — Chrome/Edge terbuka di PC yang menjalankan
   dashboard, otomatis lewat proxy milik akun itu, dengan profil browser terpisah.
2. Login dengan Google seperti biasa.
3. Setelah masuk dashboard EarnApp, **tutup jendela browser** atau klik
   **Simpan sekarang**. Semua cookie `earnapp.com` (termasuk `httpOnly`)
   ditangkap dan disimpan terenkripsi.

Kredensial Google tidak pernah dibaca atau disimpan aplikasi; yang disimpan hanya
cookie sesi EarnApp. Login manual dengan copy header `Cookie` tetap tersedia
sebagai cadangan.

Status cookie di halaman akun:

| Badge | Arti |
| --- | --- |
| `BELUM ADA COOKIE` | belum ada cookie tersimpan (atau sudah dihapus) |
| `COOKIE TERSIMPAN · BELUM DITES` | cookie ada, klik **Test login** untuk verifikasi |
| `LOGIN OK` | cookie valid dan API EarnApp bisa dibaca |
| `LOGIN ERROR` | cookie ada tapi gagal dipakai (sering karena proxy mati) |
| `COOKIE EXPIRED` | cookie sudah tidak berlaku, login ulang lewat helper |

Helper hanya menyimpan cookie kalau cookie login EarnApp sudah terbentuk
(`auth`, `oauth-token`, `oauth-refresh-token`, atau `earnapp_user`). Kalau
**Simpan sekarang** diklik terlalu cepat, helper menunggu sampai 20 detik; kalau
cookie login tetap belum ada, cookie **tidak** disimpan dan statusnya gagal.
Cookie visitor/analytics (`brd_sess_id`, `xsrf-token`, `_ga`) tidak dianggap
sebagai login.

Kotak cookie manual yang dikirim **kosong tidak menghapus** cookie lama. Untuk
menghapus, pakai tombol **Hapus cookie** (ada konfirmasi). Kalau setelah helper
statusnya masih `LOGIN ERROR`, cek dulu apakah proxy akun hidup — bukan berarti
cookie-nya hilang.

**Import JSON Cookie-Editor:** kotak cookie manual juga menerima export JSON dari
extension Cookie-Editor/EditThisCookie. Login di Chrome biasa sampai masuk
dashboard EarnApp, export cookie untuk situs `earnapp.com` (format JSON), lalu
tempel apa adanya. Dashboard hanya mengambil cookie domain `earnapp.com`,
mengabaikan cookie yang `expirationDate`-nya sudah lewat, dan menolak input yang
tidak berisi satu pun cookie EarnApp (cookie lama tetap aman).

Catatan: extension tidak memperpanjang umur cookie — masa berlaku cookie `auth`
ditentukan server EarnApp. Yang biasanya membuat hasil export "tahan lama" adalah
login yang benar-benar selesai di Chrome biasa (termasuk verifikasi Google),
sehingga set cookie `auth`/`oauth-token`/`oauth-refresh-token` lengkap. Setelah
menempel, selalu klik **Test login** untuk memastikan cookie benar-benar valid.

Browser yang dibuka login helper **tidak perlu extension** — cookie (termasuk
`httpOnly`) dibaca langsung dari browser oleh helper. Kalau tetap butuh format
JSON (misalnya untuk dipakai di browser/tool lain), buka halaman akun dan klik
**Download cookie JSON**. File itu kompatibel dengan Cookie-Editor dan bisa
di-import kembali kapan saja.

Sebelum membuka browser, helper menguji slot proxy akun (maksimal 5 slot) dan
memilih yang benar-benar hidup. Skema yang berhasil (`socks5`/`http`) disimpan,
lalu dipakai lewat jembatan HTTP lokal (`app/proxy_bridge.py`) karena Chromium
tidak mendukung SOCKS5 ber-autentikasi. IP keluar ditampilkan di status; kalau
berbeda dari `Proxy IP` di sheet, helper memberi peringatan.

Butuh Google Chrome atau Microsoft Edge terpasang di PC tersebut. Kalau tidak ada,
jalankan `.\.venv\Scripts\playwright install chromium`. Jendela browser muncul di
mesin yang menjalankan server, jadi jalankan dashboard di PC yang kamu pakai.

## Mapping kolom

Kalau nama kolom di sheet berubah, edit `config/sheet.json`. Setiap field punya
daftar alias; pencocokan tidak peka huruf besar/spasi.

```json
{
  "read": {
    "account": ["account", "nama akun", "akun"],
    "declared_ip": ["proxy ip", "ip"],
    "endpoint": ["proxy command", "proxy comand"],
    "region": ["country/region", "region"],
    "group": ["group"],
    "local_ip": ["local ip"],
    "usage_count": ["count"],
    "blacklist": ["blacklist"]
  }
}
```

Baris dengan `Blacklist` terisi otomatis dinonaktifkan.

## Konfigurasi penting

| Env | Arti |
|---|---|
| `GOOGLE_SHEET_ID` | ID spreadsheet; kalau sheet publik cukup ini saja |
| `GOOGLE_SHEET_GID` | gid tab (default 0) |
| `SHEET_WRITEBACK=1` | tulis `_status`, `_detected_ip`, `_healthy`, `_last_check` ke sheet (butuh service account) |
| `SCRAPER_MODE` | `off` (default), `mock`, `real` — hanya untuk endpoint saldo lama |
| `EARN_APP_BALANCE_URL` | endpoint saldo EarnApp (tidak resmi, tangkap dari DevTools) |
| `EARN_APP_API_BASE` | base API dashboard EarnApp (default `https://earnapp.com/dashboard/api`) |
| `EARN_APP_APP_VERSION` | versi app di query API (default `1.685.135`) |
| `DEVICE_SYNC=1` | tarik device/usage/banned dari API EarnApp (tidak butuh `SCRAPER_MODE`) |
| `DEVICE_USAGE_DAYS` | jumlah hari usage yang disimpan (default 14) |
| `LOGIN_HELPER=1` | tampilkan tombol login otomatis (cookie via browser + proxy) |
| `LOGIN_HELPER_BROWSER` | `auto`, `chrome`, `msedge`, atau `chromium` |
| `LOGIN_HELPER_TIMEOUT` | batas waktu sesi login (detik, default 900) |
| `LOGIN_PROFILE_DIR` | folder profil browser per akun (default `secrets/browser_profiles`) |
| `DOMAIN` | domain HTTPS untuk Caddy saat deploy Docker (contoh `203.0.113.10.sslip.io`) |
| `SECRET_KEY` | kunci Fernet pengganti `secrets/secret.key` (opsional; harus sama dengan DB lama) |
| `POLL_CONCURRENCY` | jumlah cek paralel (default 8) |
| `PROXY_SCHEME` | `auto`, `socks5`, atau `http` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | alert saat akun bermasalah / cookie expired |

## Struktur

```
app/
  config.py    konfigurasi + mapping kolom
  models.py    Account, ProxySlot, SlotCheck, Snapshot, EarnDevice, DeviceUsage
  sheets.py    baca sheet (publik / service account / CSV)
  proxy.py     parse endpoint kredensial + health check
  earnapp.py   API dashboard EarnApp: saldo, device, usage, banned
  login_helper.py  buka browser lewat proxy + tangkap cookie otomatis
  jobs.py      sync sheet, cek slot, guard IP, sync device EarnApp, status akun
  main.py      route + tampilan
  templates/   dashboard, semua slot, device EarnApp, detail akun
config/sheet.json
data/          database + export CSV (gitignored)
secrets/       kunci enkripsi (gitignored)
```

## Catatan

- Belum ada migrasi skema: kalau model berubah, hapus `data/monitor.db` lalu sync ulang.
- Mengganti `Proxy Command` di sheet = slot baru; slot lama dinonaktifkan, riwayatnya tetap ada.
- Tambah `data/pool.csv` (export CSV) untuk jalan tanpa Google Sheet: kosongkan `GOOGLE_SHEET_ID`.
- Sheet berisi kredensial proxy. Jangan dibagikan publik; dashboard hanya butuh akses baca.
- Multi-akun di satu layanan seperti EarnApp umumnya melanggar ToS-nya, dan saldo bisa hangus.
  Sistem ini sengaja menolak berjalan kalau IP tidak benar-benar terpisah.
