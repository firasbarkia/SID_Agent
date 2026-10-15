"""Transactional admission for candidate CV and generation queues."""

from app.db.connection import server_time
from app.domain import DomainError


async def reserve(db, principal, settings, session, *, byte_count=0):
    # Serialize admissions across both collections. Counts include pre-upgrade work;
    # finishing/deleting work releases capacity through the same authoritative rows,
    # so no separate reservation counters can drift after a crash or lease recovery.
    guard = await db.sid_admission.update_one(
        {"_id": "queue"}, {"$inc": {"revision": 1}}, session=session
    )
    if not guard.matched_count:
        raise RuntimeError("Initialize admission collections before accepting work")
    owner = {"owner_id": principal.user_id}
    active = {"state": {"$in": ["pending", "running"]}}
    user_jobs = global_jobs = 0
    for name in ("sid_tasks", "sid_cv_tasks"):
        user_jobs += await db[name].count_documents({**owner, **active}, session=session)
        global_jobs += await db[name].count_documents(active, session=session)
    if user_jobs >= settings.admission_user_pending_jobs:
        raise DomainError(429, "user_pending_job_limit")
    if global_jobs >= settings.admission_global_pending_jobs:
        raise DomainError(503, "global_pending_job_limit")
    if byte_count:
        totals = await (
            await db.sid_cv_imports.aggregate(
                [
                    {"$match": {**owner, "status": {"$ne": "deleted"}}},
                    {"$group": {"_id": None, "bytes": {"$sum": "$byte_count"}}},
                ],
                session=session,
            )
        ).to_list()
        stored = totals[0]["bytes"] if totals else 0
        if stored + byte_count > settings.admission_user_cv_bytes:
            raise DomainError(429, "user_cv_storage_limit")
    # One bounded row per user, shared by CV and draft creation. Only new accepted
    # work consumes the minute window; idempotent replays bypass reserve entirely.
    key = "user:" + principal.user_id
    # Re-read time on every transaction attempt: a retry can cross a minute.
    window = (await server_time(db)).replace(second=0, microsecond=0)
    rate = await db.sid_admission.find_one({"_id": key}, session=session)
    count = rate["count"] if rate and rate["window"] == window else 0
    if count >= settings.admission_user_requests_per_minute:
        raise DomainError(429, "user_enqueue_rate_limit")
    await db.sid_admission.replace_one(
        {"_id": key},
        {"_id": key, "window": window, "count": count + 1},
        upsert=True,
        session=session,
    )
