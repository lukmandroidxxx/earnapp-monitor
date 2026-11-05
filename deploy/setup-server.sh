#!/usr/bin/env bash
# Setup dashboard EarnApp di server Ubuntu/Debian yang selalu menyala.
#
# Pemakaian (dari folder project, setelah zip diekstrak):
#   sudo -E bash deploy/setup-server.sh
#
# Bisa juga tanpa interaksi:
#   DOMAIN=203.0.113.10.sslip.io DASHBOARD_PASSWORD=rahasia-panjang \
#     sudo -E bash deploy/setup-server.sh
#
# Script ini:
#   1. memasang Docker + Compose plugin kalau belum ada,
#   2. membuka port 80/443 di firewall lokal,
#   3. membuat/memperbarui .env (LOGIN_HELPER=0, AUTO_SYNC=1, AUTO_POLL=0),
#   4. build image dan menjalankan app + Caddy (HTTPS otomatis).
#
# Setelah selesai, database/cookie lama (kalau ada) tetap dipakai dari folder
# data/ dan secrets/. Kalau belum ada, dashboard mulai kosong dan bisa sync sheet.

set -euo pipefail

SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

say() { printf '\n== %s ==\n' "$*"; }
warn() { printf 'Peringatan: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ -f docker-compose.yml ] || die "docker-compose.yml tidak ditemukan. Jalankan script dari folder project hasil ekstrak zip."
[ -f .env.example ] || die ".env.example tidak ditemukan."

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "Butuh sudo untuk memasang Docker dan membuka port."
    SUDO="sudo"
fi

if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|debian) ;;
        *) warn "Script diuji di Ubuntu/Debian; distro Anda: ${ID:-tidak diketahui}." ;;
    esac
fi

# ---------------------------------------------------------------------------
# 1. Docker
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    say "Memasang Docker"
    $SUDO apt-get update -y
    $SUDO apt-get install -y ca-certificates curl
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    $SUDO sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
    fi
fi

if docker info >/dev/null 2>&1; then
    DOCKER="docker"
else
    DOCKER="$SUDO docker"
fi

if ! $DOCKER compose version >/dev/null 2>&1; then
    say "Memasang Docker Compose plugin"
    $SUDO apt-get update -y
    $SUDO apt-get install -y docker-compose-plugin
fi
$DOCKER compose version

if ! command -v curl >/dev/null 2>&1; then
    $SUDO apt-get install -y curl
fi
if ! command -v python3 >/dev/null 2>&1; then
    $SUDO apt-get install -y python3
fi

# ---------------------------------------------------------------------------
# 2. Domain dan password
# ---------------------------------------------------------------------------
detect_ip() {
    curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null \
        || curl -4fsS --max-time 8 https://ifconfig.me 2>/dev/null \
        || true
}

PUBLIC_IP="$(detect_ip)"
DOMAIN="${DOMAIN:-}"
if [ -z "$DOMAIN" ]; then
    if [ -n "$PUBLIC_IP" ]; then
        default_domain="${PUBLIC_IP}.sslip.io"
    else
        default_domain=""
    fi
    printf '\nDomain HTTPS untuk dashboard'
    if [ -n "$default_domain" ]; then
        printf ' [%s]' "$default_domain"
    fi
    printf ' (tanpa https://): '
    read -r DOMAIN
    DOMAIN="${DOMAIN:-$default_domain}"
fi

DOMAIN="${DOMAIN#http://}"
DOMAIN="${DOMAIN#https://}"
DOMAIN="${DOMAIN%%/*}"
[ -n "$DOMAIN" ] || die "DOMAIN kosong. Contoh: 203.0.113.10.sslip.io"

DASHBOARD_USER="${DASHBOARD_USER:-admin}"
DASHBOARD_PASSWORD="${DASHBOARD_PASSWORD:-}"
if [ -z "$DASHBOARD_PASSWORD" ]; then
    printf 'Password dashboard untuk user "%s" (min 12 karakter, kosong = dibuat otomatis): ' "$DASHBOARD_USER"
    read -r -s DASHBOARD_PASSWORD
    printf '\n'
    if [ -z "$DASHBOARD_PASSWORD" ]; then
        DASHBOARD_PASSWORD="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64)"
        printf 'Password dibuat otomatis: %s\n' "$DASHBOARD_PASSWORD"
    fi
fi
[ "${#DASHBOARD_PASSWORD}" -ge 12 ] || die "Password minimal 12 karakter."
case "$DASHBOARD_PASSWORD" in
    *[[:space:]]*) die "Password jangan mengandung spasi." ;;
esac

# ---------------------------------------------------------------------------
# 3. .env
# ---------------------------------------------------------------------------
[ -f .env ] || cp .env.example .env
mkdir -p data secrets
chmod 700 data secrets 2>/dev/null || true

TARGET_DOMAIN="$DOMAIN" \
TARGET_USER="$DASHBOARD_USER" \
TARGET_PASSWORD="$DASHBOARD_PASSWORD" \
TARGET_SECRET="${SECRET_KEY:-}" \
python3 - <<'PY'
import os
import pathlib
import re

path = pathlib.Path(".env")
text = path.read_text(encoding="utf-8")

updates = {
    "DOMAIN": os.environ["TARGET_DOMAIN"],
    "DASHBOARD_USER": os.environ["TARGET_USER"],
    "DASHBOARD_PASSWORD": os.environ["TARGET_PASSWORD"],
    "LOGIN_HELPER": "0",
    "AUTO_SYNC": "1",
    "AUTO_POLL": "0",
    "DEVICE_SYNC": "1",
}
secret = os.environ.get("TARGET_SECRET", "").strip()
if secret:
    updates["SECRET_KEY"] = secret

lines = text.splitlines()
seen = set()
out = []
for line in lines:
    match = re.match(r"^([A-Za-z0-9_]+)=", line)
    if match and match.group(1) in updates:
        key = match.group(1)
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)

for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")

path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
PY

chmod 600 .env 2>/dev/null || true

# ---------------------------------------------------------------------------
# 4. Firewall lokal (VCN/security group cloud tetap harus dibuka manual)
# ---------------------------------------------------------------------------
say "Membuka port 80/443 di firewall server"

if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -qi "active"; then
    $SUDO ufw allow 22/tcp >/dev/null 2>&1 || true
    $SUDO ufw allow 80/tcp >/dev/null 2>&1 || true
    $SUDO ufw allow 443/tcp >/dev/null 2>&1 || true
    printf 'ufw: port 22/80/443 diizinkan.\n'
fi

if command -v iptables >/dev/null 2>&1; then
    for port in 80 443; do
        if ! $SUDO iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
            $SUDO iptables -I INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
        fi
    done
    if command -v netfilter-persistent >/dev/null 2>&1; then
        $SUDO netfilter-persistent save >/dev/null 2>&1 || true
    else
        printf 'iptables: aturan sementara aktif. Pasang iptables-persistent agar tetap ada setelah reboot.\n'
    fi
fi

warn "Jangan lupa buka port 80/443 di security list/VCN cloud (Oracle: Default Security List)."

# ---------------------------------------------------------------------------
# 5. Jalankan
# ---------------------------------------------------------------------------
say "Build dan jalankan container (bisa 2-5 menit di server kecil)"
$DOCKER compose up -d --build
$DOCKER compose ps

say "Menunggu HTTPS siap (maks 120 detik)"
ready=0
for _ in $(seq 1 40); do
    code="$(curl -ks -o /dev/null -w '%{http_code}' \
        --resolve "$DOMAIN:443:127.0.0.1" \
        "https://$DOMAIN/healthz" 2>/dev/null || true)"
    if [ "$code" = "200" ] || [ "$code" = "401" ]; then
        ready=1
        break
    fi
    sleep 3
done

printf '\n============================================================\n'
if [ "$ready" = "1" ]; then
    printf 'Dashboard siap: https://%s\n' "$DOMAIN"
else
    printf 'Dashboard belum menjawab lewat HTTPS.\n'
    printf 'Cek log: %s compose logs caddy\n' "$DOCKER"
    printf 'Cek app : %s compose logs app\n' "$DOCKER"
fi
printf 'User    : %s\n' "$DASHBOARD_USER"
printf 'Password: %s\n' "$DASHBOARD_PASSWORD"
printf '\nCookie: login helper dimatikan di server. Perbarui cookie dari PC lokal\n'
printf 'lalu tempel JSON-nya di halaman akun dashboard server.\n'
printf '============================================================\n'

if [ -n "${SUDO_USER:-}" ]; then
    printf '\nAgar tidak perlu sudo untuk Docker, jalankan sekali lalu login ulang:\n'
    printf '  sudo usermod -aG docker %s\n' "$SUDO_USER"
fi
