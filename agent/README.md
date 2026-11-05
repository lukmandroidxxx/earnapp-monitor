# Pool Agent (pengganti ProxyfierClient)

Agent kecil tanpa dependensi (hanya standard library) yang membuka listener
proxy di RDP host, mengambil assignment IP dari dashboard, dan meneruskan
koneksi VM ke upstream Loki.

```
VM (NAT 192.168.76.x)
   -> 192.168.18.200:10000-10027   (agent ini)
      -> CONNECT + Basic auth
         -> global.rpip.lokiproxy.com:35001
            -> IP residensial sesuai assignment
```

## Kenapa di host, bukan di VM?

VM memakai VMnet8 (NAT). Koneksi dari guest ke `192.168.18.200:10019`
di-SNAT oleh `vmnat` sehingga terlihat berasal dari `192.168.18.200`. Jadi
listener harus ada di host RDP, dan IP/port di dalam VM tidak perlu diubah.

## Pasang di RDP

1. Salin folder `agent/` ke `C:\LokiProxyAgent`.
2. Salin `config.example.json` menjadi `config.json`, isi `token` dengan
   `POOL_AGENT_TOKEN` dari `.env` server.
3. (Opsional) uji dulu di port cadangan tanpa mengganggu produksi:

   ```powershell
   cd C:\LokiProxyAgent
   python pool_agent.py --selftest --ports 10000,10001 --target api.ipify.org:443
   ```

   Atau jalankan listener di port lain:

   ```powershell
   python pool_agent.py --config config.json
   # ubah sementara start_port/num_ports ke 10100/28 di config.json
   ```

4. Setelah yakin, hentikan `ProxyfierClient.exe` dan jalankan agent di
   10000-10027. Contoh scheduled task (SYSTEM, restart otomatis):

   ```powershell
   $py = "C:\LokiProxyAgent\.venv\Scripts\pythonw.exe"   # atau python.exe
   $action = New-ScheduledTaskAction -Execute $py `
       -Argument "C:\LokiProxyAgent\pool_agent.py --config C:\LokiProxyAgent\config.json" `
       -WorkingDirectory "C:\LokiProxyAgent"
   $trigger = New-ScheduledTaskTrigger -AtStartup
   $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
   $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
   Register-ScheduledTask -TaskName "PoolProxyAgent" -Action $action -Trigger $trigger `
       -Principal $principal -Settings $settings -Force
   Start-ScheduledTask -TaskName "PoolProxyAgent"
   ```

## Konfigurasi

| Field | Default | Arti |
|---|---|---|
| `api_url` | - | endpoint `/api/pool` dashboard |
| `token` | - | `POOL_AGENT_TOKEN` (Bearer) |
| `machine` | `RDP1` | harus sama dengan `config/pool.json` |
| `bind_ip` | `0.0.0.0` | pakai `192.168.18.200` di RDP |
| `start_port` / `num_ports` | `10000` / `28` | sama dengan port VM |
| `refresh_sec` | `60` | ambil assignment dari web |
| `report_sec` | `30` | kirim statistik ke web |
| `probe_sec` | `300` | cek kesehatan sesi (0 = nonaktif) |
| `probe_target` | `api.ipify.org:443` | target CONNECT untuk probe |
| `connect_timeout` | `20` | timeout ke upstream |

Environment variable dengan prefix `POOL_` menimpa nilai config
(`POOL_API_URL`, `POOL_API_TOKEN`, `POOL_BIND_IP`, dst.).

## Perilaku

- Protokol masuk: HTTP proxy (`CONNECT` dan request absolut) dan SOCKS5
  (tanpa auth atau username/password apa pun).
- Satu port = satu sesi. Assignment diambil ulang tiap `refresh_sec`.
- Probe periodik membuka `CONNECT` ke `probe_target`. Sesi yang gagal
  dilaporkan ke dashboard; dashboard memindahkan port ke sesi lain dan
  mengarantina sesi rusak (5 menit, 1 jam, 6 jam, 24 jam).
- Kalau dashboard tidak bisa dihubungi saat start, agent memakai cache
  `cache_file` terakhir.

## Selftest

```powershell
python pool_agent.py --selftest
python pool_agent.py --selftest --ports 10000,10016 --target api.ipify.org:443
```

Exit code `0` kalau semua port bisa `CONNECT`, `2` kalau ada yang gagal.
