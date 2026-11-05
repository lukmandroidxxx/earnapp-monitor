from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    group_name: Mapped[str | None] = mapped_column(String(32), default=None)
    local_ip: Mapped[str | None] = mapped_column(String(64), default=None)

    # OK | DEGRADED | ERROR | BLOCKED | PAUSED | UNCHECKED
    status: Mapped[str] = mapped_column(String(16), default="UNCHECKED", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)

    # Opsional: hanya dipakai kalau ada kolom cookie/session di sheet.
    cookie_enc: Mapped[str | None] = mapped_column(Text, default=None)
    balance: Mapped[float | None] = mapped_column(Float, default=None)
    lifetime: Mapped[float | None] = mapped_column(Float, default=None)
    nodes_online: Mapped[int | None] = mapped_column(Integer, default=None)

    # NOT_SET | OK | EXPIRED | ERROR
    auth_status: Mapped[str] = mapped_column(String(16), default="NOT_SET")
    auth_reason: Mapped[str | None] = mapped_column(Text, default=None)
    auth_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    auth_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    last_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    slots: Mapped[list["ProxySlot"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    devices: Mapped[list["EarnDevice"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    device_usage: Mapped[list["DeviceUsage"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class CookieMeta(Base):
    """Metadata cookie (expiry/path/httpOnly) untuk export JSON yang setia."""

    __tablename__ = "cookie_meta"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    payload_enc: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProxySlot(Base):
    """Satu baris pool = satu sesi proxy = satu IP keluar untuk satu akun."""

    __tablename__ = "proxy_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    sheet_row: Mapped[int | None] = mapped_column(Integer, default=None)

    # Kredensial proxy (host:port:user:pass) disimpan terenkripsi.
    endpoint_enc: Mapped[str] = mapped_column(Text, default="")
    endpoint_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    endpoint_hint: Mapped[str] = mapped_column(String(200), default="")

    # declared_ip = guard unik (NULL kalau bentrok), raw = nilai asli dari sheet.
    declared_ip: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)
    declared_ip_raw: Mapped[str | None] = mapped_column(String(64), default=None)

    region: Mapped[str | None] = mapped_column(String(160), default=None)
    group_name: Mapped[str | None] = mapped_column(String(32), default=None)
    local_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    blacklisted: Mapped[bool] = mapped_column(Boolean, default=False)
    scheme: Mapped[str] = mapped_column(String(16), default="auto")

    detected_ip: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    # OK | UNHEALTHY | BLOCKED | BLACKLIST | UNCHECKED | PAUSED
    status: Mapped[str] = mapped_column(String(16), default="UNCHECKED", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    last_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_ok: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    account: Mapped[Account] = relationship(back_populates="slots")
    checks: Mapped[list["SlotCheck"]] = relationship(
        back_populates="slot", cascade="all, delete-orphan"
    )


class SlotCheck(Base):
    __tablename__ = "slot_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot_id: Mapped[int] = mapped_column(
        ForeignKey("proxy_slots.id", ondelete="CASCADE"), index=True
    )
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    slot: Mapped[ProxySlot] = relationship(back_populates="checks")


class Snapshot(Base):
    """Snapshot saldo EarnApp (opsional, hanya jika kolom cookie diisi)."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    balance: Mapped[float | None] = mapped_column(Float, default=None)
    lifetime: Mapped[float | None] = mapped_column(Float, default=None)
    nodes_online: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    account: Mapped[Account] = relationship(back_populates="snapshots")


class EarnDevice(Base):
    """Device yang terdaftar di akun EarnApp (dari /dashboard/api/devices)."""

    __tablename__ = "earn_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    uuid: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160), default="")
    country: Mapped[str | None] = mapped_column(String(8), default=None)
    rate: Mapped[float | None] = mapped_column(Float, default=None)
    earned: Mapped[float | None] = mapped_column(Float, default=None)
    earned_total: Mapped[float | None] = mapped_column(Float, default=None)
    uptime_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    total_uptime_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    ips_json: Mapped[str] = mapped_column(Text, default="[]")

    # Diisi kalau EarnApp menandai device/IP ini kena blacklist.
    banned_reason: Mapped[str | None] = mapped_column(String(80), default=None)
    banned_ip: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    online: Mapped[bool] = mapped_column(Boolean, default=False)
    online_known: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship(back_populates="devices")

    @property
    def ips(self) -> list[str]:
        try:
            value = json.loads(self.ips_json or "[]")
        except (TypeError, ValueError):
            return []
        return [str(item) for item in value] if isinstance(value, list) else []


class PoolEntry(Base):
    """Satu sesi proxy dari file export Loki (bisa juga dari sheet Pool).

    Berbeda dari ProxySlot yang terikat satu akun EarnApp, PoolEntry adalah
    inventaris IP mentah. Assignment ke port/mesin ada di PoolPort.
    """

    __tablename__ = "pool_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    endpoint_enc: Mapped[str] = mapped_column(Text)
    endpoint_hint: Mapped[str] = mapped_column(String(200), default="")

    host: Mapped[str] = mapped_column(String(120), default="")
    port: Mapped[int] = mapped_column(Integer, default=0)
    username: Mapped[str] = mapped_column(String(120), default="")

    ip_address: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    country_region: Mapped[str | None] = mapped_column(String(160), index=True, default=None)
    country_code: Mapped[str | None] = mapped_column(String(8), default=None)
    region_state: Mapped[str | None] = mapped_column(String(80), default=None)
    city: Mapped[str | None] = mapped_column(String(80), default=None)
    asn: Mapped[str | None] = mapped_column(String(32), default=None)

    # ACTIVE | OFFLINE | EXPIRED | UNKNOWN (dari file) — expired dihitung ulang saat dibaca.
    status: Mapped[str] = mapped_column(String(16), default="UNKNOWN", index=True)
    usage_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, default=None)

    account_name: Mapped[str | None] = mapped_column(String(160), default=None)
    group_name: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    local_ip: Mapped[str | None] = mapped_column(String(64), default=None)

    blacklisted: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Kesehatan sesi dari probe agent. Sesudah beberapa kali gagal, sesi
    # dikarantina sementara supaya port dipindah ke sesi lain.
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_fail_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    quarantine_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, default=None
    )

    source_file: Mapped[str | None] = mapped_column(String(240), default=None)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PoolPort(Base):
    """Assignment satu port listener ke satu PoolEntry (1 port = 1 sesi)."""

    __tablename__ = "pool_ports"
    __table_args__ = (UniqueConstraint("machine", "port", name="uq_pool_port_machine"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine: Mapped[str] = mapped_column(String(64), index=True)
    port: Mapped[int] = mapped_column(Integer)
    group_name: Mapped[str | None] = mapped_column(String(32), default=None)

    entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("pool_entries.id", ondelete="SET NULL"), index=True, default=None
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Laporan dari agent yang jalan di RDP.
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    active_conns: Mapped[int | None] = mapped_column(Integer, default=None)
    rx_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    tx_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    last_error: Mapped[str | None] = mapped_column(String(240), default=None)
    note: Mapped[str | None] = mapped_column(String(240), default=None)

    entry: Mapped[PoolEntry | None] = relationship()


class PoolImport(Base):
    """Riwayat upload file xlsx."""

    __tablename__ = "pool_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(240), default="")
    source_format: Mapped[str] = mapped_column(String(32), default="")
    entries_seen: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    blacklisted: Mapped[int] = mapped_column(Integer, default=0)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class DeviceUsage(Base):
    """Pemakaian harian per device (nilai API EarnApp dalam milidetik uptime)."""

    __tablename__ = "device_usage"
    __table_args__ = (UniqueConstraint("device_uuid", "day", name="uq_device_usage_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    device_uuid: Mapped[str] = mapped_column(String(80), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    ms: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship(back_populates="device_usage")
