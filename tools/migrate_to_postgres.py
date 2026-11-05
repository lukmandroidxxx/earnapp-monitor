"""Salin seluruh isi SQLite lokal ke Postgres (Neon).

Pemakaian:
    python tools/migrate_to_postgres.py --target "postgresql+psycopg://user:pass@host/db?sslmode=require"
    python tools/migrate_to_postgres.py --target "..." --dry-run

Skrip ini:
- membuat tabel di target kalau belum ada (create_all),
- mengosongkan tabel target (TRUNCATE ... RESTART IDENTITY CASCADE),
- menyalin semua baris apa adanya termasuk id,
- menyetel ulang sequence id supaya INSERT berikutnya tidak bentrok.

Sumbernya selalu data/monitor.db (SQLite lokal).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrasi SQLite lokal ke Postgres")
    parser.add_argument("--source", default=str(ROOT / "data" / "monitor.db"))
    parser.add_argument("--target", required=True, help="URL Postgres tujuan")
    parser.add_argument("--dry-run", action="store_true", help="Hanya hitung baris")
    args = parser.parse_args()

    source_url = f"sqlite:///{Path(args.source).as_posix()}"
    src = create_engine(source_url, future=True)

    tables = list(models.Base.metadata.sorted_tables)
    src_tables = set(inspect(src).get_table_names())

    if args.dry_run:
        with src.connect() as sconn:
            total = 0
            for table in tables:
                if table.name not in src_tables:
                    print(f"- {table.name}: tidak ada di sumber, dilewati")
                    continue
                count = sconn.execute(select(func.count()).select_from(table)).scalar_one()
                print(f"- {table.name}: {count} baris (dry-run)")
                total += count
        print(f"Dry-run: {total} baris akan disalin. Target tidak disentuh.")
        return 0

    dst = create_engine(args.target, future=True, pool_pre_ping=True)

    with dst.begin() as conn:
        if conn.dialect.name != "postgresql":
            print(f"Target bukan Postgres: {conn.dialect.name}", file=sys.stderr)
            return 2

    models.Base.metadata.create_all(dst)

    with src.connect() as sconn, dst.begin() as dconn:
        for table in reversed(tables):
            dconn.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))

        total = 0
        for table in tables:
            if table.name not in src_tables:
                print(f"- {table.name}: tidak ada di sumber, dilewati")
                continue
            rows = [dict(row._mapping) for row in sconn.execute(table.select())]
            if args.dry_run:
                print(f"- {table.name}: {len(rows)} baris (dry-run)")
                total += len(rows)
                continue
            if rows:
                dconn.execute(table.insert(), rows)
            print(f"- {table.name}: {len(rows)} baris disalin")
            total += len(rows)

            id_col = table.c.get("id")
            if id_col is not None and rows:
                dconn.execute(
                    text(
                        f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', 'id'), "
                        f"COALESCE((SELECT MAX(id) FROM \"{table.name}\"), 1), true)"
                    )
                )

    verify = Session(dst)
    try:
        for table in tables:
            count = verify.execute(text(f'SELECT COUNT(*) FROM "{table.name}"')).scalar_one()
            print(f"  target {table.name}: {count}")
    finally:
        verify.close()

    print(f"Selesai: {total} baris diproses.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
