import asyncio
from datetime import timedelta

import pytest

from app.ai.errors import ProvidersUnavailable
from app.ai.providers import Generation
from app.db.connection import server_time
from app.db.queue import LeaseLost, MongoQueue
from app.db.store import Store
from app.domain import ApproveDraft, DomainError, DraftCreate, DraftEdit, ProfileCreate, ProfileEdit
from app.identity import Principal
from app.worker import DraftWorker

pytestmark = pytest.mark.mongodb


class FakeGateway:
    def __init__(self, action=None):
        self.calls = 0
        self.action = action

    async def generate(self, system, prompt):
        self.calls += 1
        if isinstance(self.action, Exception):
            raise self.action
        if self.action:
            await self.action()
        return Generation("A synthetic cover letter ready for human review.", "groq", "test-model")


async def accepted_profile(store, principal):
    await store.db.users.update_one(
        {"id": principal.user_id},
        {"$setOnInsert": {"role": "CANDIDATE", "isActive": True}},
        upsert=True,
    )
    profile = await store.create_profile(
        principal, ProfileCreate(content="Student with a Python university project.")
    )
    await store.accept_profile(principal, profile["_id"], 1)
    return profile


def draft_request(profile):
    return DraftCreate(
        profile_id=profile["_id"],
        profile_version=1,
        job_description="PFE internship requiring Python in Sfax.",
        language="en",
    )


async def generated_draft(db, user, settings):
    store = Store(db)
    profile = await accepted_profile(store, user)
    requested = await store.request_draft(user, draft_request(profile), "request-one")
    gateway = FakeGateway()
    assert await DraftWorker(db, gateway, settings).run_once()
    return store, profile, requested


async def test_profile_history_acceptance_ownership_and_stale_write(mongo_db, candidate_user):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    stranger = Principal(user_id="another-candidate", role="CANDIDATE")
    with pytest.raises(DomainError) as error:
        await store.profile(stranger, profile["_id"])
    assert error.value.status == 404
    changed = await store.edit_profile(
        candidate_user,
        profile["_id"],
        ProfileEdit(
            expected_version=1, content="Updated profile with a second verified university project."
        ),
    )
    assert changed["version"] == 2 and changed["validated_version"] is None
    versions = (
        await mongo_db.sid_profile_versions.find({"profile_id": profile["_id"]})
        .sort("version", 1)
        .to_list()
    )
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["content"] != versions[1]["content"]
    with pytest.raises(DomainError, match="stale_profile_version"):
        await store.accept_profile(candidate_user, profile["_id"], 1)


async def test_draft_requires_accepted_profile_and_concurrent_enqueue_is_idempotent(
    mongo_db, candidate_user
):
    store = Store(mongo_db)
    profile = await store.create_profile(
        candidate_user, ProfileCreate(content="Python student with university projects.")
    )
    payload = draft_request(profile)
    with pytest.raises(DomainError, match="profile_version_not_validated"):
        await store.request_draft(candidate_user, payload, "same-request")
    assert await mongo_db.sid_tasks.count_documents({}) == 0
    await store.accept_profile(candidate_user, profile["_id"], 1)
    responses = await asyncio.gather(
        *(store.request_draft(candidate_user, payload, "same-request") for _ in range(8))
    )
    assert len({r["task_id"] for r in responses}) == 1
    assert await mongo_db.sid_tasks.count_documents({}) == 1
    assert await mongo_db.sid_drafts.count_documents({}) == 1
    with pytest.raises(DomainError, match="idempotency_key_reused"):
        await store.request_draft(
            candidate_user, payload.model_copy(update={"language": "fr"}), "same-request"
        )


async def test_worker_generates_once_with_immutable_history_and_outbox(
    mongo_db, candidate_user, settings
):
    store, profile, requested = await generated_draft(mongo_db, candidate_user, settings)
    head = await store.draft(candidate_user, requested["draft_id"])
    assert head["version"] == 1 and head["status"] == "draft"
    assert head["requires_human_approval"] is True
    assert await mongo_db.sid_draft_versions.count_documents({"draft_id": head["_id"]}) == 1
    assert await mongo_db.sid_outbox.count_documents({"type": "draft.generated"}) == 1
    gateway = FakeGateway()
    assert not await DraftWorker(mongo_db, gateway, settings).run_once()
    assert gateway.calls == 0
    assert (await mongo_db.sid_tasks.find_one({"_id": requested["task_id"]}))["state"] == "done"


async def test_approval_is_atomic_idempotent_and_letter_edit_invalidates_it(
    mongo_db, candidate_user, settings
):
    store, _, requested = await generated_draft(mongo_db, candidate_user, settings)
    draft_id = requested["draft_id"]
    approval = ApproveDraft(expected_version=1, confirmed=True, destination_id="offer-one")
    results = await asyncio.gather(
        *(store.approve_draft(candidate_user, draft_id, approval) for _ in range(5))
    )
    assert len({r["_id"] for r in results}) == 1
    assert results[0]["draft_version"] == 1 and results[0]["destination_id"] == "offer-one"
    assert await mongo_db.sid_outbox.count_documents({"type": "draft.approved"}) == 1
    # Approval does not create a submission task or platform Application record.
    assert await mongo_db.sid_tasks.count_documents({"type": {"$ne": "draft.generate"}}) == 0
    assert await mongo_db.applications.count_documents({}) == 0
    updated = await store.edit_draft(
        candidate_user,
        draft_id,
        DraftEdit(
            expected_version=1, cover_letter="The candidate's reviewed and edited cover letter."
        ),
    )
    assert updated["version"] == 2 and updated["approval_id"] is None
    assert updated["requires_human_approval"] is True
    assert await mongo_db.sid_approvals.count_documents({"active": True}) == 0
    with pytest.raises(DomainError, match="stale_draft_version"):
        await store.approve_draft(candidate_user, draft_id, approval)


async def test_approval_transaction_rolls_back_if_outbox_write_fails(
    mongo_db, candidate_user, settings, monkeypatch
):
    store, _, requested = await generated_draft(mongo_db, candidate_user, settings)
    count = await mongo_db.sid_outbox.count_documents({})

    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic outbox failure")

    monkeypatch.setattr("app.db.store.emit", fail)
    with pytest.raises(RuntimeError, match="Synthetic"):
        await store.approve_draft(
            candidate_user,
            requested["draft_id"],
            ApproveDraft(expected_version=1, confirmed=True, destination_id="offer-one"),
        )
    assert await mongo_db.sid_approvals.count_documents({}) == 0
    assert await mongo_db.sid_outbox.count_documents({}) == count
    assert (await store.draft(candidate_user, requested["draft_id"]))["status"] == "draft"


async def test_profile_edit_invalidates_approval_and_prevents_approval_of_old_sources(
    mongo_db, candidate_user, settings
):
    store, profile, requested = await generated_draft(mongo_db, candidate_user, settings)
    body = ApproveDraft(expected_version=1, confirmed=True, destination_id="offer-one")
    await store.approve_draft(candidate_user, requested["draft_id"], body)
    await store.edit_profile(
        candidate_user,
        profile["_id"],
        ProfileEdit(
            expected_version=1, content="A revised candidate profile with corrected facts."
        ),
    )
    head = await store.draft(candidate_user, requested["draft_id"])
    assert head["status"] == "draft" and head["requires_human_approval"] is True
    assert await mongo_db.sid_approvals.count_documents({"active": True}) == 0
    await store.accept_profile(candidate_user, profile["_id"], 2)
    with pytest.raises(DomainError, match="profile_version_not_validated"):
        await store.approve_draft(candidate_user, requested["draft_id"], body)


async def test_concurrent_edit_has_one_winner_and_no_overwritten_history(
    mongo_db, candidate_user, settings
):
    store, _, requested = await generated_draft(mongo_db, candidate_user, settings)
    edit = DraftEdit(
        expected_version=1, cover_letter="A reviewed cover letter with corrected facts."
    )
    results = await asyncio.gather(
        *(store.edit_draft(candidate_user, requested["draft_id"], edit) for _ in range(5)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert all(
        isinstance(r, dict) or isinstance(r, DomainError) and r.status == 409 for r in results
    )
    assert await mongo_db.sid_draft_versions.count_documents({}) == 2


async def test_concurrent_profile_edit_and_approval_cannot_leave_stale_active_approval(
    mongo_db, candidate_user, settings
):
    store, profile, requested = await generated_draft(mongo_db, candidate_user, settings)
    results = await asyncio.gather(
        store.approve_draft(
            candidate_user,
            requested["draft_id"],
            ApproveDraft(expected_version=1, confirmed=True, destination_id="offer-one"),
        ),
        store.edit_profile(
            candidate_user,
            profile["_id"],
            ProfileEdit(
                expected_version=1, content="Corrected candidate profile with new verified skills."
            ),
        ),
        return_exceptions=True,
    )
    assert all(isinstance(r, dict) or isinstance(r, DomainError) for r in results)
    assert await mongo_db.sid_approvals.count_documents({"active": True}) == 0
    assert (await store.draft(candidate_user, requested["draft_id"]))[
        "requires_human_approval"
    ] is True


async def test_expired_lease_recovery_fences_old_worker_and_acknowledgement(
    mongo_db, candidate_user, settings
):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    requested = await store.request_draft(candidate_user, draft_request(profile), "lease-test")
    queue = MongoQueue(mongo_db)
    claims = await asyncio.gather(*(queue.claim() for _ in range(5)))
    old = next(c for c in claims if c)
    assert sum(c is not None for c in claims) == 1
    await mongo_db.sid_tasks.update_one(
        {"_id": old["_id"]},
        {"$set": {"lease_until": await server_time(mongo_db) - timedelta(seconds=1)}},
    )
    replacement = await queue.claim()
    assert replacement["lease_token"] != old["lease_token"] and replacement["attempts"] == 2
    with pytest.raises(LeaseLost):
        await queue.heartbeat(old)
    with pytest.raises(LeaseLost):
        await DraftWorker(mongo_db, FakeGateway(), settings)._complete(
            old,
            candidate_user,
            await store.draft(candidate_user, requested["draft_id"]),
            Generation("Stale result", "groq", "test"),
        )
    assert await mongo_db.sid_draft_versions.count_documents({}) == 0
    await queue.heartbeat(replacement)
    await queue.finish(replacement)


async def test_worker_retries_transient_failure_and_exhausts_budget(
    mongo_db, candidate_user, settings
):
    store = Store(mongo_db, max_attempts=2)
    profile = await accepted_profile(store, candidate_user)
    requested = await store.request_draft(candidate_user, draft_request(profile), "retry-test")
    worker = DraftWorker(mongo_db, FakeGateway(ProvidersUnavailable(60)), settings)
    assert await worker.run_once()
    task = await mongo_db.sid_tasks.find_one({"_id": requested["task_id"]})
    assert task["state"] == "pending" and task["attempts"] == 1
    assert not await worker.run_once()
    await mongo_db.sid_tasks.update_one(
        {"_id": task["_id"]}, {"$set": {"available_at": await server_time(mongo_db)}}
    )
    assert await worker.run_once()
    assert (await mongo_db.sid_tasks.find_one({"_id": task["_id"]}))["state"] == "failed"
    assert (await store.draft(candidate_user, requested["draft_id"]))["status"] == "failed"


async def test_profile_change_during_generation_discards_result(mongo_db, candidate_user, settings):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    requested = await store.request_draft(
        candidate_user, draft_request(profile), "stale-generation"
    )
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await finish.wait()

    task = asyncio.create_task(DraftWorker(mongo_db, FakeGateway(slow), settings).run_once())
    await started.wait()
    await store.edit_profile(
        candidate_user,
        profile["_id"],
        ProfileEdit(
            expected_version=1, content="The corrected profile supersedes the generation's source."
        ),
    )
    finish.set()
    await task
    assert await mongo_db.sid_draft_versions.count_documents({}) == 0
    assert (await store.draft(candidate_user, requested["draft_id"]))["status"] == "failed"


async def test_outbox_delivery_is_leased_and_stale_ack_is_rejected(mongo_db, candidate_user):
    await Store(mongo_db).create_profile(
        candidate_user, ProfileCreate(content="Synthetic profile with Python university projects.")
    )
    first_queue, second_queue = (
        MongoQueue(mongo_db, "sid_outbox"),
        MongoQueue(mongo_db, "sid_outbox"),
    )
    event = await first_queue.claim()
    assert event["type"] == "profile.created"
    assert await second_queue.claim() is None
    await mongo_db.sid_outbox.update_one(
        {"_id": event["_id"]},
        {"$set": {"lease_until": await server_time(mongo_db) - timedelta(seconds=1)}},
    )
    newer = await second_queue.claim()
    with pytest.raises(LeaseLost):
        await first_queue.finish(event)
    await second_queue.finish(newer)


async def test_worker_cancellation_leaves_recoverable_task(mongo_db, candidate_user, settings):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    requested = await store.request_draft(candidate_user, draft_request(profile), "cancel-test")
    started = asyncio.Event()

    async def pending():
        started.set()
        await asyncio.Event().wait()

    worker_task = asyncio.create_task(
        DraftWorker(mongo_db, FakeGateway(pending), settings).run_once()
    )
    await started.wait()
    worker_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker_task
    assert await mongo_db.sid_draft_versions.count_documents({}) == 0
    await mongo_db.sid_tasks.update_one(
        {"_id": requested["task_id"]},
        {"$set": {"lease_until": await server_time(mongo_db) - timedelta(seconds=1)}},
    )
    assert await DraftWorker(mongo_db, FakeGateway(), settings).run_once()
    assert (await store.draft(candidate_user, requested["draft_id"]))["version"] == 1


async def test_crashed_final_attempt_is_failed_without_more_provider_calls(
    mongo_db, candidate_user, settings
):
    store = Store(mongo_db, max_attempts=1)
    profile = await accepted_profile(store, candidate_user)
    requested = await store.request_draft(candidate_user, draft_request(profile), "exhausted-crash")
    task = await MongoQueue(mongo_db).claim()
    await mongo_db.sid_tasks.update_one(
        {"_id": task["_id"]},
        {"$set": {"lease_until": await server_time(mongo_db) - timedelta(seconds=1)}},
    )
    gateway = FakeGateway()
    assert await DraftWorker(mongo_db, gateway, settings).run_once()
    assert gateway.calls == 0
    assert (await mongo_db.sid_tasks.find_one({"_id": requested["task_id"]}))[
        "error_code"
    ] == "attempts_exhausted"


async def test_disabled_account_stops_queued_generation(mongo_db, candidate_user, settings):
    store = Store(mongo_db)
    profile = await accepted_profile(store, candidate_user)
    requested = await store.request_draft(
        candidate_user, draft_request(profile), "account-disabled"
    )
    await mongo_db.users.update_one({"id": candidate_user.user_id}, {"$set": {"isActive": False}})
    gateway = FakeGateway()
    assert await DraftWorker(mongo_db, gateway, settings).run_once()
    assert gateway.calls == 0
    assert (await mongo_db.sid_tasks.find_one({"_id": requested["task_id"]}))[
        "error_code"
    ] == "inactive_or_unknown_user"
