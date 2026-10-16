"""Keep match indexes current: python -m app.matching.worker."""

import asyncio
import logging
import os
import socket
from datetime import timedelta
from uuid import uuid4

import httpx
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.config import Settings
from app.db.connection import ensure_indexes, mongo_connection, server_time
from app.knowledge.embedding import LocalEmbedder
from app.knowledge.qdrant import QdrantIndex, qdrant_client
from app.matching.catalog import PlatformCatalog
from app.matching.index import MatchIndex

logger = logging.getLogger(__name__)


class MatchSyncWorker:
    def __init__(self, db, qdrant, embedder, settings: Settings, *, owner=None):
        self.db, self.settings = db, settings
        self.index = MatchIndex(db, qdrant, embedder)
        self.catalog = PlatformCatalog(db, settings)
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"

    async def _lease(self) -> bool:
        # One writer at a time; a crashed worker's lease expires and another takes over.
        now = await server_time(self.db)
        until = now + timedelta(seconds=max(60.0, self.settings.match_sync_seconds * 4))
        try:
            lease = await self.db.sid_match_state.find_one_and_update(
                {
                    "_id": "sync_lease",
                    "$or": [{"owner": self.owner}, {"lease_until": {"$lt": now}}],
                },
                {"$set": {"owner": self.owner, "lease_until": until}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            return False
        return lease["owner"] == self.owner

    async def release(self):
        """Let a restarted worker take over immediately instead of waiting for expiry."""
        await self.db.sid_match_state.delete_one({"_id": "sync_lease", "owner": self.owner})

    async def run_once(self) -> dict:
        if not await self._lease():
            return {"status": "lease_held"}
        offers, skipped_offers = await self.catalog.eligible_offers()
        candidates, skipped_candidates = await self.catalog.eligible_candidates()
        return {
            "status": "synced",
            "offers": await self.index.sync("offers", offers, skipped=skipped_offers),
            "candidates": await self.index.sync(
                "candidates", candidates, skipped=skipped_candidates
            ),
        }


async def main():
    settings = Settings()
    async with mongo_connection(settings) as db:
        if db is None:
            raise SystemExit("Configure MONGODB_URI")
        await ensure_indexes(db)
        embedder = await asyncio.to_thread(
            LocalEmbedder, settings.embedding_model_directory, settings.embedding_threads
        )
        async with qdrant_client(settings) as client:
            qdrant = QdrantIndex(client, embedder.dimensions, embedder.fingerprint)
            worker = MatchSyncWorker(db, qdrant, embedder, settings)
            try:
                while True:
                    try:
                        logger.info("Match sync: %s", await worker.run_once())
                    except (PyMongoError, httpx.HTTPError, ValueError):
                        # Expected infrastructure failures retry; programming errors still exit.
                        logger.exception("Match sync failed; retrying")
                    await asyncio.sleep(settings.match_sync_seconds)
            finally:
                await worker.release()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
