"""``python -m app.seed`` — idempotent app-db seed (tenant, users, data source allow-list, scopes)."""

from __future__ import annotations

import asyncio

from app.db.seed.app_seed import seed

if __name__ == "__main__":
    asyncio.run(seed())
