"""Builds the analytics database: a SQLite file holding the vendored Northwind dataset.

* The only step that ever opens the file read-write.  The API opens it ``mode=ro``.
* Base data is the upstream ``northwind/northwind.db`` copied as-is (MIT — see
  ``northwind/README.md``); ``northwind/schema.sql`` documents the DDL it was built with.
* Idempotent: the file is copied once; Lens's ``v_*`` views are (re)created on every run
  so a catalogue change lands without touching the data.

Run with ``python -m app.db.seed.analytics_seed`` (``--force`` recopies the file).
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

from app.config import get_settings
from app.db.seed.analytics_catalog import VIEWS

DATASET_DIR = Path(__file__).parent / "northwind"
SOURCE_DB = DATASET_DIR / "northwind.db"
SCHEMA_SQL = DATASET_DIR / "schema.sql"
BASE_TABLES = (
    "Categories",
    "Suppliers",
    "Products",
    "Customers",
    "Employees",
    "Regions",
    "Territories",
    "EmployeeTerritories",
    "Shippers",
    "Orders",
    "Order Details",
)


# foreign-key indexes the upstream file lacks; they live only in the runtime copy
INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_orders_employee ON Orders(EmployeeID)",
    "CREATE INDEX IF NOT EXISTS ix_orders_customer ON Orders(CustomerID)",
    "CREATE INDEX IF NOT EXISTS ix_orders_date ON Orders(OrderDate)",
    "CREATE INDEX IF NOT EXISTS ix_orders_shipvia ON Orders(ShipVia)",
    'CREATE INDEX IF NOT EXISTS ix_order_details_product ON "Order Details"(ProductID)',
    "CREATE INDEX IF NOT EXISTS ix_products_category ON Products(CategoryID)",
    "CREATE INDEX IF NOT EXISTS ix_products_supplier ON Products(SupplierID)",
)


def _has_base_data(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'Orders'").fetchone()
    return bool(row and row[0]) and conn.execute("SELECT COUNT(*) FROM Orders").fetchone()[0] > 0


def create_views(conn: sqlite3.Connection) -> None:
    for v in VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {v.name}")
        conn.execute(f"CREATE VIEW {v.name} AS {v.sql}")


def verify_views(conn: sqlite3.Connection) -> None:
    """Every catalogued column must exist in the created view — the allow-list must not lie to the model."""
    for v in VIEWS:
        actual = {r[1] for r in conn.execute(f"PRAGMA table_info({v.name})")}
        declared = {c.name for c in v.columns}
        missing = declared - actual
        if missing:
            raise RuntimeError(f"{v.name}: catalogue declares columns the view does not have: {sorted(missing)}")
        if v.scope_column and v.scope_column not in actual:
            raise RuntimeError(f"{v.name}: scope column {v.scope_column} is not in the view")


def seed(force: bool = False) -> Path:
    settings = get_settings()
    path = settings.analytics_sqlite_file
    path.parent.mkdir(parents=True, exist_ok=True)
    if force and path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    if not path.exists():
        print(f"analytics seed: copying {SOURCE_DB.name} → {path}", flush=True)
        shutil.copyfile(SOURCE_DB, path)
    conn = sqlite3.connect(path)
    try:
        if not _has_base_data(conn):  # a stale or foreign file at the path: replace it
            conn.close()
            shutil.copyfile(SOURCE_DB, path)
            conn = sqlite3.connect(path)
        for table in BASE_TABLES:
            n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            print(f"  {table:<20} {n:>7} rows", flush=True)
        for ddl in INDEXES:
            conn.execute(ddl)
        create_views(conn)
        verify_views(conn)
        conn.commit()
        conn.execute("ANALYZE")
        conn.commit()
        print(f"analytics seed: {len(VIEWS)} views ready at {path}", flush=True)
    finally:
        conn.close()
    return path


if __name__ == "__main__":
    seed(force="--force" in sys.argv)
