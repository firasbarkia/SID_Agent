from contextlib import asynccontextmanager
from datetime import UTC

from pymongo import AsyncMongoClient
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern

from app.config import Settings


@asynccontextmanager
async def mongo_connection(settings: Settings):
    uri = settings.mongodb_uri.get_secret_value()
    if not uri:
        yield None
        return
    async with AsyncMongoClient(
        uri,
        tz_aware=True,
        serverSelectionTimeoutMS=int(settings.mongodb_timeout_seconds * 1000),
        timeoutMS=int(settings.mongodb_timeout_seconds * 1000),
        maxPoolSize=50,
    ) as client:
        hello = await client.admin.command("hello")
        if not (hello.get("setName") or hello.get("msg") == "isdbgrid"):
            raise RuntimeError("Phase 2 requires a MongoDB replica set or sharded cluster")
        yield client[settings.mongodb_database].with_options(
            write_concern=WriteConcern("majority"), read_concern=ReadConcern("majority")
        )


async def server_time(db):
    # Command responses use PyMongo's default codec even with tz_aware clients.
    now = (await db.client.admin.command("hello"))["localTime"]
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


async def transaction(db, callback):
    async with db.client.start_session() as session:
        return await session.with_transaction(
            callback,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
        )


async def ensure_indexes(db):
    # Dedicated collections preserve the platform's existing schemas.
    specs = {
        "sid_profiles": [([("scope_id", 1), ("owner_id", 1)], {})],
        "sid_profile_versions": [([("profile_id", 1), ("version", 1)], {"unique": True})],
        "sid_drafts": [([("scope_id", 1), ("owner_id", 1), ("profile_id", 1)], {})],
        "sid_draft_versions": [([("draft_id", 1), ("version", 1)], {"unique": True})],
        "sid_approvals": [([("draft_id", 1), ("active", 1)], {})],
        "sid_tasks": [
            ([("scope_id", 1), ("owner_id", 1), ("idempotency_key", 1)], {"unique": True}),
            ([("state", 1), ("available_at", 1), ("lease_until", 1)], {}),
        ],
        "sid_outbox": [
            ([("aggregate_id", 1), ("event_key", 1)], {"unique": True}),
            ([("state", 1), ("available_at", 1), ("lease_until", 1)], {}),
        ],
        "sid_provider_state": [],
    }
    existing = set(await db.list_collection_names())
    for name, indexes in specs.items():
        if name not in existing:
            await db.create_collection(name)
        for keys, options in indexes:
            await db[name].create_index(keys, **options)
