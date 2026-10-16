"""Versioned match collections in Qdrant plus per-record phrase vectors in MongoDB.

Projections come from the catalog, which excludes identity, so neither Qdrant
payloads nor `sid_match_records` ever hold names or contact details.
"""

import asyncio
from contextlib import suppress
from uuid import uuid4

import httpx
import numpy as np
from bson import Binary

from app.db.connection import server_time
from app.knowledge.qdrant import point_id

KINDS = ("offers", "candidates")
PAYLOAD_FIELDS = ("record_id", "version", "kind", "contract", "company_id", "city")
INDEXED_FIELDS = ("record_id", "version", "contract", "company_id", "city")
BATCH = 16


def pack(vector):
    return Binary(np.asarray(vector, dtype=np.float32).tobytes())


def unpack(blob):
    return np.frombuffer(bytes(blob), dtype=np.float32)


def match(key, value):
    return {"key": key, "match": {"value": value}}


def search_texts(kind, doc) -> list[str]:
    if kind == "offers":
        headline = f"{doc.title}. {', '.join(doc.requirements)}. {doc.company.sector}"
        return [headline, doc.description]
    return [", ".join(doc.skills), " ".join(doc.statements)]


def phrase_groups(kind, doc) -> dict[str, list[str]]:
    if kind == "offers":
        return {"requirements": doc.requirements}
    return {"skills": doc.skills, "statements": doc.statements}


def payload(kind, doc):
    base = {"record_id": doc.id, "version": doc.version, "kind": kind}
    if kind == "offers":
        return {
            **base,
            "contract": doc.contract,
            "company_id": doc.company_id,
            "city": doc.company.city,
        }
    return {**base, "city": doc.city}


class MatchIndex:
    def __init__(self, db, qdrant, embedder):
        self.db, self.qdrant, self.embedder = db, qdrant, embedder

    async def active(self, kind) -> str:
        state = await self.db.sid_match_state.find_one({"_id": kind})
        if (
            not state
            or not state.get("active")
            or state.get("fingerprint") != (self.embedder.fingerprint)
        ):
            raise ValueError("matching_index_not_ready")
        return state["active"]

    async def sync(self, kind, docs, *, skipped=0) -> dict:
        fingerprint = self.embedder.fingerprint
        state = await self.db.sid_match_state.find_one({"_id": kind}) or {}
        previous, collection = None, None
        if state.get("active") and state.get("fingerprint") == fingerprint:
            collection = await self._existing(state["active"])
        if collection is None:
            previous = state.get("active")
            if state.get("building_fingerprint") == fingerprint:
                # Resume a build that failed earlier instead of leaking a new collection.
                collection = await self._existing(state.get("building"))
            if collection is None:
                # A new model gets a fresh collection; the old one serves until the switch.
                collection = f"sid_match_{kind}_{uuid4().hex}"
                await self.qdrant.create(collection, fields=INDEXED_FIELDS)
                await self.db.sid_match_state.update_one(
                    {"_id": kind},
                    {"$set": {"building": collection, "building_fingerprint": fingerprint}},
                    upsert=True,
                )
        known = {
            row["record_id"]: row["version"]
            async for row in self.db.sid_match_records.find(
                {"kind": kind, "collection": collection}, {"record_id": 1, "version": 1}
            )
        }
        current = {doc.id: doc for doc in docs}
        changed = [doc for doc in current.values() if known.get(doc.id) != doc.version]
        removed = [record_id for record_id in known if record_id not in current]
        for start in range(0, len(changed), BATCH):
            await self._index(kind, collection, changed[start : start + BATCH])
        for record_id in removed:
            await self.qdrant.delete_where(collection, [match("record_id", record_id)])
            await self.db.sid_match_records.delete_one({"_id": f"{collection}:{record_id}"})
        await self.db.sid_match_state.update_one(
            {"_id": kind},
            {
                "$set": {
                    "active": collection,
                    "fingerprint": fingerprint,
                    "last_sync_at": await server_time(self.db),
                    "records": len(current),
                    "last_indexed": len(changed),
                    "last_removed": len(removed),
                    "skipped": skipped,
                },
                "$unset": {"building": "", "building_fingerprint": ""},
            },
            upsert=True,
        )
        if previous and previous != collection:
            with suppress(httpx.HTTPError):
                await self.qdrant.drop(previous)
            await self.db.sid_match_records.delete_many({"collection": previous})
        return {"indexed": len(changed), "removed": len(removed), "total": len(current)}

    async def _existing(self, collection):
        """The collection if Qdrant still has it with this model, else None (and forget it)."""
        if not collection:
            return None
        try:
            await self.qdrant.verify(collection)
            return collection
        except (httpx.HTTPStatusError, ValueError):
            # Lost or replaced in Qdrant (e.g. a volume reset): its records must be rebuilt.
            await self.db.sid_match_records.delete_many({"collection": collection})
            return None

    async def _index(self, kind, collection, docs):
        texts, owners = [], []
        for doc in docs:
            for text in search_texts(kind, doc):
                if text.strip():
                    for chunk in self.embedder.chunks(text):
                        texts.append(chunk)
                        owners.append((doc, "search", None))
            for group, phrases in phrase_groups(kind, doc).items():
                for phrase in phrases:
                    texts.append(phrase)
                    owners.append((doc, "phrase", group))
        vectors = await asyncio.to_thread(self.embedder.encode, texts)
        points, stored = [], {}
        for (doc, role, group), text, vector in zip(owners, texts, vectors, strict=True):
            record = stored.setdefault(doc.id, {"doc": doc, "phrases": {}, "chunks": 0})
            if role == "search":
                points.append(
                    {
                        "id": point_id(collection, doc.id, doc.version, record["chunks"]),
                        "vector": vector,
                        "payload": payload(kind, doc),
                    }
                )
                record["chunks"] += 1
            else:
                record["phrases"].setdefault(group, []).append(
                    {"text": text, "vector": pack(vector)}
                )
        await self.qdrant.upsert(collection, points)
        now = await server_time(self.db)
        for record_id, record in stored.items():
            doc = record["doc"]
            # New points are written first, so the record is never missing from search.
            await self.qdrant.delete_where(
                collection, [match("record_id", record_id)], [match("version", doc.version)]
            )
            phrases = {group: [] for group in phrase_groups(kind, doc)}
            phrases.update(record["phrases"])
            await self.db.sid_match_records.replace_one(
                {"_id": f"{collection}:{record_id}"},
                {
                    "_id": f"{collection}:{record_id}",
                    "kind": kind,
                    "collection": collection,
                    "record_id": record_id,
                    "version": doc.version,
                    "doc": doc.model_dump(),
                    "phrases": phrases,
                    "indexed_at": now,
                },
                upsert=True,
            )

    async def query(self, kind, vector, *, limit, filters) -> list[dict]:
        collection = await self.active(kind)
        must = [match(key, value) for key, value in filters.items() if value is not None]
        points = await self.qdrant.query(collection, vector, limit=limit * 4, must=must)
        best = {}
        for point in points:
            record_id = point["payload"]["record_id"]
            # Points arrive best first; keep each record's best chunk.
            best.setdefault(
                record_id,
                {
                    "record_id": record_id,
                    "version": point["payload"]["version"],
                    "score": point["score"],
                },
            )
        return list(best.values())[:limit]

    async def records(self, kind, record_ids) -> dict:
        collection = await self.active(kind)
        rows = await self.db.sid_match_records.find(
            {"collection": collection, "record_id": {"$in": list(record_ids)}}
        ).to_list()
        return {
            row["record_id"]: {
                "version": row["version"],
                "doc": row["doc"],
                "phrases": {
                    group: [(item["text"], unpack(item["vector"])) for item in items]
                    for group, items in row["phrases"].items()
                },
            }
            for row in rows
        }

    async def status(self) -> dict:
        result = {}
        for kind in KINDS:
            state = await self.db.sid_match_state.find_one({"_id": kind}) or {}
            ready = bool(state.get("active")) and state.get("fingerprint") == (
                self.embedder.fingerprint
            )
            result[kind] = {
                "ready": ready,
                "collection": state.get("active"),
                "records": state.get("records", 0),
                "skipped": state.get("skipped", 0),
                "last_sync_at": state.get("last_sync_at"),
                "last_indexed": state.get("last_indexed", 0),
                "last_removed": state.get("last_removed", 0),
            }
        return result
