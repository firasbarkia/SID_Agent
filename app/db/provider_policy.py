"""Apply configured limits without resetting charged quota or outage cooldowns."""

import argparse
import asyncio

from app.ai.mongo_circuit import MongoCircuitBreaker
from app.config import Settings
from app.db.connection import mongo_connection


async def apply(provider):
    settings = Settings()
    async with mongo_connection(settings) as db:
        if db is None:
            raise RuntimeError("Configure MONGODB_URI first")
        await MongoCircuitBreaker(db, provider, settings).apply_policy()
        print(f"Applied {provider} limits; existing quota reservations and cooldowns retained.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=("groq", "gemini"))
    asyncio.run(apply(parser.parse_args().provider))
