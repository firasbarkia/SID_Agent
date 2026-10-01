import asyncio

from app.knowledge.store import KnowledgeStore


class KnowledgeSearch:
    def __init__(self, db, index, embedder, max_concurrent=2):
        self.store = KnowledgeStore(db)
        self.index = index
        self.embedder = embedder
        self.max_concurrent = max_concurrent
        self.running = 0
        self.tasks = set()

    async def search(self, query, limit=5, source=None, kind=None):
        if self.running >= self.max_concurrent:
            raise RuntimeError("knowledge_busy")
        self.running += 1
        work = asyncio.create_task(self._search(query, limit, source, kind))
        self.tasks.add(work)
        work.add_done_callback(self._release)
        # A cancelled HTTP request cannot stop native CPU inference. Retain admission
        # until that work really finishes, preventing an unbounded abandoned backlog.
        return await asyncio.shield(work)

    def _release(self, work):
        self.running -= 1
        self.tasks.discard(work)
        if not work.cancelled():
            work.exception()

    async def close(self):
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _search(self, query, limit, source, kind):
        try:
            generation = await self.store.active(self.embedder.fingerprint)
            await self.index.verify(generation)
            chunks = await asyncio.to_thread(self.embedder.chunks, query)
            if len(chunks) != 1:
                raise ValueError("Query exceeds the embedding token limit")
            vectors = await asyncio.to_thread(self.embedder.encode, chunks)
            hits = await self.index.query(
                generation,
                vectors[0],
                limit=min(200, limit * 20),
                source=source,
                kind=kind,
            )
            records = await self.store.hydrate(generation, hits, limit, source, kind)
            return {
                "generation": generation,
                "results": records,
                "score_type": "cosine_similarity",
                "is_compatibility_percentage": False,
            }
        except Exception:
            raise
