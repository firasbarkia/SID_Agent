import asyncio
import logging
from collections import defaultdict
from contextlib import suppress

import httpx
from pymongo import ReplaceOne

from app.db.connection import transaction
from app.db.queue import LeaseLost, MongoQueue
from app.knowledge.models import KnowledgeRecord
from app.knowledge.qdrant import point_id
from app.knowledge.store import VISIBLE, KnowledgeStore

logger = logging.getLogger(__name__)


class IndexWorker:
    def __init__(self, db, index, embedder, settings):
        self.db = db
        self.index = index
        self.embedder = embedder
        self.settings = settings
        self.store = KnowledgeStore(db)
        self.queue = MongoQueue(db, "sid_index_jobs", settings.worker_lease_seconds)

    async def _heartbeat(self, tasks):
        while True:
            await asyncio.sleep(self.settings.worker_lease_seconds / 3)
            for task in tasks:
                try:
                    await self.queue.heartbeat(task)
                except LeaseLost:
                    head = await self.queue.collection.find_one({"_id": task["_id"]})
                    if head and head["state"] in {"done", "failed"}:
                        continue
                    raise

    async def run_once(self):
        tasks = []
        for _ in range(self.settings.indexing_batch_size):
            task = await self.queue.claim()
            if task is None:
                break
            tasks.append(task)
        if not tasks:
            return False
        work = asyncio.create_task(self.process_batch(tasks))
        heartbeat = asyncio.create_task(self._heartbeat(tasks))
        try:
            done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
            await (work if work in done else heartbeat)
        except LeaseLost:
            logger.warning("Reference index lease lost; next claim will recover")
        except (httpx.HTTPError, ValueError):
            for task in tasks:
                with suppress(LeaseLost):
                    await self.queue.finish(
                        task,
                        state="failed"
                        if task["attempts"] >= self.settings.worker_max_attempts
                        else "pending",
                        error_code="index_unavailable_or_invalid",
                        retry_after=5,
                    )
        finally:
            for running in (work, heartbeat):
                running.cancel()
            await asyncio.gather(work, heartbeat, return_exceptions=True)
        return True

    async def process(self, task):
        await self.process_batch([task])

    async def process_batch(self, tasks):
        heads = {
            r["_id"]: r
            for r in await self.db.sid_kb_records.find(
                {
                    "_id": {"$in": [t["record_id"] for t in tasks]},
                }
            ).to_list()
        }
        generations = {
            g["_id"]: g
            for g in await self.db.sid_kb_generations.find(
                {
                    "_id": {"$in": list({t["generation"] for t in tasks})},
                }
            ).to_list()
        }
        prepared, texts, owners = [], [], []
        verified = set()
        for task in tasks:
            if task["attempts"] > self.settings.worker_max_attempts:
                await self.queue.finish(task, state="failed", error_code="attempts_exhausted")
                continue
            meta = generations.get(task["generation"])
            head = heads.get(task["record_id"])
            if (
                not meta
                or meta["state"] == "retired"
                or not head
                or head["version"] != task["version"]
            ):
                await self.queue.finish(task)
                continue
            if (
                task["type"] != "reference.index"
                or meta["fingerprint"] != self.embedder.fingerprint
                or meta["dimensions"] != self.embedder.dimensions
            ):
                raise ValueError("Embedding/job contract mismatch")
            if task["generation"] not in verified:
                await self.index.verify(task["generation"])
                verified.add(task["generation"])
            visible = all(head[k] == v for k, v in VISIBLE.items())
            item = {"task": task, "head": head, "visible": visible, "points": []}
            prepared.append(item)
            if visible:
                record = KnowledgeRecord.model_validate(
                    {k: head[k] for k in KnowledgeRecord.model_fields}
                )
                chunks = await asyncio.to_thread(self.embedder.chunks, record.search_text())
                texts.extend(chunks)
                owners.extend((item, chunk) for chunk in range(len(chunks)))
        for start in range(0, len(texts), 16):
            vectors = await asyncio.to_thread(self.embedder.encode, texts[start : start + 16])
            if len(vectors) != len(texts[start : start + 16]):
                raise ValueError("Embedding batch cardinality mismatch")
            for offset, vector in enumerate(vectors):
                item, chunk = owners[start + offset]
                task, head = item["task"], item["head"]
                item["points"].append(
                    {
                        "id": point_id(task["generation"], head["_id"], head["version"], chunk),
                        "vector": vector,
                        "payload": {
                            "record_id": head["_id"],
                            "version": head["version"],
                            "chunk": chunk,
                            "source": head["source"],
                            "kind": head["kind"],
                        },
                    }
                )
        by_generation = defaultdict(list)
        for item in prepared:
            by_generation[item["task"]["generation"]].extend(item["points"])
        for generation, points in by_generation.items():
            for start in range(0, len(points), 64):
                await self.index.upsert(generation, points[start : start + 64])
        for item in prepared:
            head, task = item["head"], item["task"]
            if head["version"] > 1 or not item["visible"]:
                # Never delete future versions when an old writer finishes late.
                await self.index.delete_record(
                    task["generation"],
                    head["_id"],
                    through_version=head["version"],
                    keep_version=head["version"] if item["visible"] else None,
                )
        if prepared:
            await self._publish(prepared)

    async def _publish(self, prepared):
        async def publish(session):
            await self.store._epoch(session)
            current = {
                r["_id"]: r
                for r in await self.db.sid_kb_records.find(
                    {
                        "_id": {"$in": [item["head"]["_id"] for item in prepared]},
                    },
                    session=session,
                ).to_list()
            }
            operations = []
            for item in prepared:
                task, head, points = item["task"], item["head"], item["points"]
                if current.get(head["_id"], {}).get("version") != head["version"]:
                    continue
                operations.append(
                    ReplaceOne(
                        {"_id": f"{task['generation']}:{head['_id']}"},
                        {
                            "generation": task["generation"],
                            "record_id": head["_id"],
                            "version": head["version"],
                            "content_hash": head["content_hash"],
                            "chunks": len(points),
                            "point_ids": [p["id"] for p in points],
                        },
                        upsert=True,
                    )
                )
            if operations:
                await self.db.sid_kb_indexed.bulk_write(operations, session=session)
            await self.queue.finish_many([item["task"] for item in prepared], session=session)

        await transaction(self.db, publish)

    async def reconcile(self, generation):
        """Remove stale vectors left by a crash or a late, fenced-out writer."""
        await self.index.verify(generation)
        deleted, offset = 0, None
        while True:
            page = await self.index.scroll(generation, offset)
            stale = []
            heads = {
                r["_id"]: r
                for r in await self.db.sid_kb_records.find(
                    {
                        "_id": {
                            "$in": [p.get("payload", {}).get("record_id") for p in page["points"]]
                        },
                    }
                ).to_list()
            }
            for point in page["points"]:
                payload = point.get("payload", {})
                version, head = payload.get("version"), heads.get(payload.get("record_id"))
                if type(version) is not int or head is None:
                    stale.append(point["id"])
                elif version < head["version"] or (
                    version <= head["version"] and not all(head[k] == v for k, v in VISIBLE.items())
                ):
                    stale.append(point["id"])
            await self.index.delete_points(generation, stale)
            deleted += len(stale)
            offset = page["next_page_offset"]
            if offset is None:
                return deleted
