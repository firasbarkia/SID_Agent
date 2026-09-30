"""Explicit, idempotent setup: uv run python -m app.db.init."""

import asyncio

from app.config import Settings
from app.db.connection import ensure_indexes, mongo_connection


async def main():
    async with mongo_connection(Settings()) as db:
        if db is None:
            raise RuntimeError("Configure MONGODB_URI before initializing collections")
        await ensure_indexes(db)
        print("SID collections and indexes are ready.")


if __name__ == "__main__":
    asyncio.run(main())
