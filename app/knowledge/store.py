"""MongoDB canonical reference records and atomic versioned indexing jobs."""

from uuid import uuid4

from pymongo import ReplaceOne, UpdateOne

from app.db.connection import server_time, transaction
from app.knowledge.models import digest

VISIBLE = {"public": True, "deleted": False}


class KnowledgeStore:
    def __init__(self, db):
        self.db = db

    async def _epoch(self, session):
        await self.db.sid_kb_catalog.update_one(
            {"_id": "catalog"},
            {"$inc": {"epoch": 1}, "$setOnInsert": {"active": None}},
            upsert=True,
            session=session,
        )

    async def enqueue(self, generation, records, now, session=None):
        operations = []
        for record in records:
            key = digest([generation, record["_id"], record["version"]])
            operations.append(
                UpdateOne(
                    {"_id": key},
                    {
                        "$setOnInsert": {
                            "generation": generation,
                            "record_id": record["_id"],
                            "version": record["version"],
                            "type": "reference.index",
                            "state": "pending",
                            "attempts": 0,
                            "available_at": now,
                        }
                    },
                    upsert=True,
                )
            )
        if operations:
            await self.db.sid_index_jobs.bulk_write(operations, session=session)

    async def import_batch(self, records):
        now = await server_time(self.db)

        async def write(session):
            heads = {
                r["_id"]: r
                for r in await self.db.sid_kb_records.find(
                    {"_id": {"$in": [r.id for r in records]}},
                    session=session,
                ).to_list()
            }
            updates = []
            for record in records:
                old = heads.get(record.id)
                if old and old["content_hash"] == record.content_hash:
                    # Re-import never reverses an explicit deletion or visibility decision.
                    continue
                head = {
                    "_id": record.id,
                    **record.model_dump(),
                    "content_hash": record.content_hash,
                    "version": old["version"] + 1 if old else 1,
                    "public": old["public"] if old else True,
                    "deleted": old["deleted"] if old else False,
                    "updated_at": now,
                }
                updates.append(head)
            if not updates:
                return 0
            await self._epoch(session)
            await self.db.sid_kb_records.bulk_write(
                [ReplaceOne({"_id": r["_id"]}, r, upsert=True) for r in updates],
                session=session,
            )
            for generation in await self.db.sid_kb_generations.find(
                {"state": {"$in": ["building", "active"]}},
                session=session,
            ).to_list():
                await self.enqueue(generation["_id"], updates, now, session)
            return len(updates)

        return await transaction(self.db, write)

    async def import_snapshot(self, snapshot):
        if snapshot.rejected:
            raise ValueError("Rejected rows must be reviewed before importing; nothing imported")
        changed = 0
        for start in range(0, len(snapshot.records), 100):
            changed += await self.import_batch(snapshot.records[start : start + 100])
        manifest = snapshot.manifest()
        await self.db.sid_kb_imports.update_one(
            {"_id": digest(manifest)},
            {"$setOnInsert": {"manifest": manifest}},
            upsert=True,
        )
        return {"changed": changed, "total": len(snapshot.records), "manifest": manifest}

    async def set_visibility(self, record_id, *, public=None, deleted=None):
        """CLI/admin operation; no user-provided CVs are accepted by this store."""
        now = await server_time(self.db)

        async def write(session):
            old = await self.db.sid_kb_records.find_one({"_id": record_id}, session=session)
            if old is None:
                raise ValueError("Reference record does not exist")
            changes = {k: v for k, v in (("public", public), ("deleted", deleted)) if v is not None}
            if any(type(v) is not bool for v in changes.values()):
                raise ValueError("Visibility requires booleans")
            if all(old[k] == v for k, v in changes.items()):
                return
            head = {**old, **changes, "version": old["version"] + 1, "updated_at": now}
            await self._epoch(session)
            await self.db.sid_kb_records.replace_one({"_id": record_id}, head, session=session)
            for generation in await self.db.sid_kb_generations.find(
                {"state": {"$in": ["building", "active"]}},
                session=session,
            ).to_list():
                await self.enqueue(generation["_id"], [head], now, session)

        await transaction(self.db, write)

    async def begin_generation(self, fingerprint, dimensions, *, prefix="sid_kb_"):
        if prefix not in {"sid_kb_", "sid_test_"}:
            raise ValueError("Invalid collection prefix")
        generation = prefix + uuid4().hex

        async def write(session):
            await self._epoch(session)
            await self.db.sid_kb_generations.insert_one(
                {
                    "_id": generation,
                    "state": "building",
                    "fingerprint": fingerprint,
                    "dimensions": dimensions,
                    "seeded": False,
                },
                session=session,
            )

        await transaction(self.db, write)
        return generation

    async def seed_generation(self, generation):
        # Running again safely fills gaps after interruption. Concurrent imports already
        # enqueue into this generation because its metadata is registered first.
        now = await server_time(self.db)
        cursor = self.db.sid_kb_records.find({}).sort("_id", 1)
        batch = []
        async for head in cursor:
            batch.append(head)
            if len(batch) >= 100:
                await self.enqueue(generation, batch, now)
                batch = []
        await self.enqueue(generation, batch, now)
        await self.db.sid_kb_generations.update_one(
            {"_id": generation, "state": "building"},
            {"$set": {"seeded": True}},
        )

    async def promote(self, generation, index):
        meta = await self.db.sid_kb_generations.find_one({"_id": generation})
        if not meta or meta["state"] != "building" or not meta["seeded"]:
            raise ValueError("Generation is not a fully seeded build")
        if meta["fingerprint"] != index.fingerprint or meta["dimensions"] != index.dimensions:
            raise ValueError("Embedding contract mismatch")
        await index.verify(generation)
        catalog = await self.db.sid_kb_catalog.find_one({"_id": "catalog"})
        if await self.db.sid_index_jobs.count_documents(
            {
                "generation": generation,
                "state": {"$ne": "done"},
            }
        ):
            raise ValueError("Pending/running/failed indexing jobs block promotion")
        total_points, batch = 0, []
        cursor = self.db.sid_kb_records.find(VISIBLE, {"version": 1, "content_hash": 1}).batch_size(
            100
        )
        async for head in cursor:
            batch.append(head)
            if len(batch) >= 100:
                total_points += await self._check_parity(generation, batch)
                batch = []
        total_points += await self._check_parity(generation, batch)
        if await index.count(generation) != total_points:
            raise ValueError("Physical vector count mismatch; reconcile before promotion")

        async def write(session):
            result = await self.db.sid_kb_catalog.update_one(
                {"_id": "catalog", "epoch": catalog["epoch"]},
                {"$set": {"active": generation}, "$inc": {"epoch": 1}},
                session=session,
            )
            if not result.matched_count:
                raise ValueError("Catalog changed during verification; retry promotion")
            await self.db.sid_kb_generations.update_many(
                {"state": "active"},
                {"$set": {"state": "retired"}},
                session=session,
            )
            result = await self.db.sid_kb_generations.update_one(
                {"_id": generation, "state": "building", "seeded": True},
                {"$set": {"state": "active"}},
                session=session,
            )
            if not result.matched_count:
                raise ValueError("Generation changed during promotion")

        await transaction(self.db, write)

    async def _check_parity(self, generation, heads):
        indexed = {
            r["record_id"]: r
            for r in await self.db.sid_kb_indexed.find(
                {
                    "generation": generation,
                    "record_id": {"$in": [h["_id"] for h in heads]},
                },
                {"record_id": 1, "version": 1, "content_hash": 1, "chunks": 1},
            ).to_list()
        }
        total = 0
        for head in heads:
            entry = indexed.get(head["_id"])
            if (
                not entry
                or entry["version"] != head["version"]
                or entry["content_hash"] != head["content_hash"]
            ):
                raise ValueError("Reference/index version parity failed")
            total += entry["chunks"]
        return total

    async def active(self, fingerprint):
        catalog = await self.db.sid_kb_catalog.find_one({"_id": "catalog"})
        meta = (
            await self.db.sid_kb_generations.find_one(
                {
                    "_id": catalog["active"],
                    "state": "active",
                    "fingerprint": fingerprint,
                }
            )
            if catalog and catalog.get("active")
            else None
        )
        if meta is None:
            raise ValueError("No active reference index with this embedding contract")
        return meta["_id"]

    async def hydrate(self, generation, hits, limit, source=None, kind=None):
        results = []
        seen = set()
        for hit in hits:
            payload = hit.get("payload", {})
            record_id = payload.get("record_id")
            if not isinstance(record_id, str) or record_id in seen:
                continue
            query = {"_id": record_id, "version": payload.get("version"), **VISIBLE}
            if source:
                query["source"] = source
            if kind:
                query["kind"] = kind
            indexed = await self.db.sid_kb_indexed.find_one(
                {
                    "generation": generation,
                    "record_id": record_id,
                    "version": payload.get("version"),
                }
            )
            # Resolve the current public head after looking up index metadata.
            head = await self.db.sid_kb_records.find_one(query)
            if not head or not indexed or indexed["content_hash"] != head["content_hash"]:
                continue
            if hit["id"] not in indexed["point_ids"]:
                continue
            seen.add(record_id)
            results.append(
                {
                    "id": record_id,
                    "version": head["version"],
                    "source": head["source"],
                    "kind": head["kind"],
                    "labels": head["labels"],
                    "descriptions": head["descriptions"],
                    "aliases": head["aliases"],
                    "location": {
                        key: head["attributes"].get(key)
                        for key in (
                            "feature_class",
                            "feature_code",
                            "country_code",
                            "admin1_code",
                            "latitude",
                            "longitude",
                            "population",
                            "modified",
                        )
                    }
                    if head["source"] == "geonames" and head["kind"] == "location"
                    else None,
                    "provenance": head["provenance"],
                    "similarity": hit["score"],
                }
            )
            if len(results) >= limit:
                break
        return results
