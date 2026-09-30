from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument

from app.db.connection import server_time


class LeaseLost(Exception):
    pass


class MongoQueue:
    """At-least-once delivery with a unique fencing token for every claim."""

    def __init__(self, db, collection="sid_tasks", lease_seconds=90):
        if collection not in {"sid_tasks", "sid_outbox"}:
            raise ValueError("Only owned task/outbox collections can be leased")
        self.db = db
        self.collection = db[collection]
        self.lease_seconds = lease_seconds

    async def claim(self):
        now = await server_time(self.db)
        return await self.collection.find_one_and_update(
            {
                "$or": [
                    {"state": "pending", "available_at": {"$lte": now}},
                    {"state": "running", "lease_until": {"$lte": now}},
                ]
            },
            {
                "$set": {
                    "state": "running",
                    "lease_token": str(uuid4()),
                    "lease_until": now + timedelta(seconds=self.lease_seconds),
                },
                "$inc": {"attempts": 1},
            },
            sort=[("available_at", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER,
        )

    def fence(self, task, now):
        return {
            "_id": task["_id"],
            "state": "running",
            "lease_token": task["lease_token"],
            "lease_until": {"$gt": now},
        }

    async def heartbeat(self, task):
        now = await server_time(self.db)
        result = await self.collection.update_one(
            self.fence(task, now),
            {"$set": {"lease_until": now + timedelta(seconds=self.lease_seconds)}},
        )
        if not result.matched_count:
            raise LeaseLost()

    async def finish(self, task, *, state="done", error_code=None, retry_after=0, session=None):
        if state not in {"done", "failed", "pending"}:
            raise ValueError("Invalid task outcome")
        now = await server_time(self.db)
        result = await self.collection.update_one(
            self.fence(task, now),
            {
                "$set": {
                    "state": state,
                    "error_code": error_code,
                    "available_at": now + timedelta(seconds=retry_after),
                    "updated_at": now,
                },
                "$unset": {"lease_token": "", "lease_until": ""},
            },
            session=session,
        )
        if not result.matched_count:
            raise LeaseLost()
