"""Entrypoint for the ``migrate`` compose service: migrations, app seed, analytics seed, then exit 0."""

from __future__ import annotations

import asyncio
import subprocess
import sys

from app.db.seed.analytics_seed import seed as seed_analytics
from app.db.seed.app_seed import seed as seed_app


def main() -> int:
    print("migrate: alembic upgrade head", flush=True)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    asyncio.run(seed_app())
    asyncio.run(seed_analytics(force="--force" in sys.argv))
    print("migrate: done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
