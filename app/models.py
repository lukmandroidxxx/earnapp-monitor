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
