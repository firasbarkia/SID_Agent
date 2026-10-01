from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument, UpdateOne

from app.db.connection import server_time


class LeaseLost(Exception):
    pass


class MongoQueue:
    """At-least-once delivery with a unique fencing token for every claim."""

    def __init__(self, db, collection="sid_tasks", lease_seconds=90):
        if collection not in {"sid_tasks", "sid_outbox", "sid_index_jobs"}:
            raise ValueError("Only owned task/outbox collections can be leased")
        self.db = db
        self.collection = db[collection]
        self.lease_seconds = lease_seconds

    async def claim(self):
        now = await server_time(self.db)
        # Separate indexed scans avoid sorting every pending job in the old OR query.
        # Recover expired work first, then claim available pending work atomically.
        for state, field in (("running", "lease_until"), ("pending", "available_at")):
            claimed = await self.collection.find_one_and_update(
                {"state": state, field: {"$lte": now}},
                {
                    "$set": {
                        "state": "running",
                        "lease_token": str(uuid4()),
                        "lease_until": now + timedelta(seconds=self.lease_seconds),
                    },
                    "$inc": {"attempts": 1},
                },
                sort=[(field, 1), ("_id", 1)],
                hint=[("state", 1), (field, 1), ("_id", 1)],
                return_document=ReturnDocument.AFTER,
            )
            if claimed is not None:
                return claimed
        return None

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

    async def finish_many(self, tasks, *, session):
        """Caller owns the transaction: one lost lease rolls back the entire batch."""
        now = await server_time(self.db)
        result = await self.collection.bulk_write(
            [
                UpdateOne(
                    self.fence(task, now),
                    {
                        "$set": {"state": "done", "error_code": None, "updated_at": now},
                        "$unset": {"lease_token": "", "lease_until": ""},
                    },
                )
                for task in tasks
            ],
            session=session,
        )
        if result.matched_count != len(tasks):
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
