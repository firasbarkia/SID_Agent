import asyncio
from datetime import timedelta

import pytest

from app.cv.store import CVStore
from app.db.connection import server_time, transaction
from app.db.queue import MongoQueue
from app.db.store import Store
from app.domain import DomainError, DraftCreate, ProfileCreate
from app.identity import Principal

pytestmark = pytest.mark.mongodb
DATA = b"%PDF- synthetic admission fixture"


async def upload(store, user, key):
    return await store.upload(user, DATA, key, "en")


async def draft(store, user, key):
    profile = await store.create_profile(
        user, ProfileCreate(content="A student with a Python university project.")
    )
    await store.accept_profile(user, profile["_id"], 1)
    return await store.request_draft(
        user,
        DraftCreate(
            profile_id=profile["_id"],
            profile_version=1,
            job_description="An internship requiring Python and SQL.",
            language="en",
        ),
        key,
    )


async def test_concurrent_upload_cap_idempotency_and_deletion(mongo_db, candidate_user, settings):
    settings.admission_user_pending_jobs = 2
    store = CVStore(mongo_db, settings=settings)
    results = await asyncio.gather(
        *(upload(store, candidate_user, f"request-{i}") for i in range(8)),
        return_exceptions=True,
    )
    heads = [r for r in results if isinstance(r, dict)]
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(heads) == 2
    assert len(errors) == 6 and all(
        isinstance(e, DomainError) and e.code == "user_pending_job_limit" for e in errors
    )
    assert await mongo_db.sid_cv_tasks.count_documents({}) == 2
    assert await mongo_db.sid_cv_files.count_documents({}) == 2
    assert (await upload(store, candidate_user, heads[0]["idempotency_key"]))["_id"] == heads[0][
        "_id"
    ]
    rate = await mongo_db.sid_admission.find_one({"_id": "user:" + candidate_user.user_id})
    assert rate["count"] == 2
    await store.delete(candidate_user, heads[0]["_id"])
    await upload(store, candidate_user, "after-delete")
    assert await mongo_db.sid_cv_files.count_documents({}) == 2


async def test_storage_counts_finished_failed_and_existing_uploads(
    mongo_db, candidate_user, settings
):
    # Existing pre-upgrade rows count without a reservation migration.
    old = await upload(CVStore(mongo_db), candidate_user, "old-request")
    await mongo_db.sid_cv_tasks.update_one({"import_id": old["_id"]}, {"$set": {"state": "failed"}})
    settings.admission_user_cv_bytes = len(DATA)
    store = CVStore(mongo_db, settings=settings)
    with pytest.raises(DomainError, match="user_cv_storage_limit"):
        await upload(store, candidate_user, "too-much-storage")
    assert await mongo_db.sid_cv_tasks.count_documents({}) == 1
    await store.delete(candidate_user, old["_id"])
    await upload(store, candidate_user, "fits-after-delete")


async def test_global_ceiling_and_owner_isolation_across_cv_and_drafts(
    mongo_db, candidate_user, settings
):
    settings.admission_global_pending_jobs = 2
    settings.admission_user_pending_jobs = 1
    cvs, drafts = CVStore(mongo_db, settings=settings), Store(mongo_db, settings=settings)
    await upload(cvs, candidate_user, "cv-first")
    with pytest.raises(DomainError, match="user_pending_job_limit"):
        await draft(drafts, candidate_user, "same-user-draft")
    other = Principal(user_id="second-user", role="CANDIDATE")
    requested = await draft(drafts, other, "second-user-draft")
    third = Principal(user_id="third-user", role="CANDIDATE")
    with pytest.raises(DomainError, match="global_pending_job_limit"):
        await upload(cvs, third, "global-overflow")
    queue = MongoQueue(mongo_db)
    task = await queue.claim()
    assert task["_id"] == requested["task_id"]
    with pytest.raises(DomainError, match="global_pending_job_limit"):
        await upload(cvs, third, "running-counts")
    await transaction(mongo_db, lambda session: queue.finish(task, state="failed", session=session))
    await upload(cvs, third, "after-failure")


async def test_rate_shared_by_queues_and_window_reset(mongo_db, candidate_user, settings):
    settings.admission_user_requests_per_minute = 2
    cvs, drafts = CVStore(mongo_db, settings=settings), Store(mongo_db, settings=settings)
    head = await upload(cvs, candidate_user, "cv-first")
    await draft(drafts, candidate_user, "draft-second")
    await cvs.delete(candidate_user, head["_id"])
    with pytest.raises(DomainError, match="user_enqueue_rate_limit"):
        await upload(cvs, candidate_user, "rate-overflow")
    await mongo_db.sid_admission.update_one(
        {"_id": "user:" + candidate_user.user_id},
        {"$set": {"window": await server_time(mongo_db) - timedelta(minutes=2)}},
    )
    await upload(cvs, candidate_user, "new-window")
    assert (await mongo_db.sid_admission.find_one({"_id": "user:" + candidate_user.user_id}))[
        "count"
    ] == 1


async def test_failed_enqueue_rolls_back_rate_and_storage(
    mongo_db, candidate_user, settings, monkeypatch
):
    store = CVStore(mongo_db, settings=settings)
    insert = type(mongo_db.sid_cv_files).insert_one

    async def fail(collection, *args, **kwargs):
        if collection.name == "sid_cv_files":
            raise RuntimeError("synthetic insert failure")
        return await insert(collection, *args, **kwargs)

    monkeypatch.setattr(type(mongo_db.sid_cv_files), "insert_one", fail)
    with pytest.raises(RuntimeError, match="synthetic insert failure"):
        await upload(store, candidate_user, "rollback")
    assert await mongo_db.sid_cv_imports.count_documents({}) == 0
    assert await mongo_db.sid_cv_tasks.count_documents({}) == 0
    assert (
        await mongo_db.sid_admission.count_documents({"_id": "user:" + candidate_user.user_id}) == 0
    )


async def test_concurrent_global_cap_across_queues(mongo_db, settings):
    settings.admission_global_pending_jobs = 3
    cvs, drafts = CVStore(mongo_db, settings=settings), Store(mongo_db, settings=settings)
    users = [Principal(user_id=f"user-{i}", role="CANDIDATE") for i in range(8)]
    results = await asyncio.gather(
        *(
            upload(cvs, user, "first") if i % 2 else draft(drafts, user, "first")
            for i, user in enumerate(users)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 3
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 5 and all(
        isinstance(e, DomainError) and e.code == "global_pending_job_limit" for e in errors
    )
    assert (
        await mongo_db.sid_tasks.count_documents({})
        + await mongo_db.sid_cv_tasks.count_documents({})
        == 3
    )


async def test_concurrent_storage_cap(mongo_db, candidate_user, settings):
    settings.admission_user_cv_bytes = len(DATA) * 2
    store = CVStore(mongo_db, settings=settings)
    results = await asyncio.gather(
        *(upload(store, candidate_user, f"bytes-{i}") for i in range(5)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 2
    assert all(
        isinstance(r, DomainError) and r.code == "user_cv_storage_limit"
        for r in results
        if isinstance(r, Exception)
    )
    assert await mongo_db.sid_cv_files.count_documents({}) == 2
