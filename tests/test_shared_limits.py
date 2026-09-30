import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.ai.circuit import CircuitOpen
from app.ai.mongo_circuit import MongoCircuitBreaker, SharedPermit

pytestmark = pytest.mark.mongodb


async def pair(db, settings):
    clock = [datetime(2026, 10, 1, tzinfo=UTC)]
    first = MongoCircuitBreaker(db, "groq", settings, lambda: clock[0])
    second = MongoCircuitBreaker(db, "groq", settings, lambda: clock[0])
    await first.initialize()
    await second.initialize()
    return first, second, clock


async def test_global_request_budget_survives_new_instances_and_no_boundary_burst(
    mongo_db, settings
):
    config = settings.model_copy(update={"groq_requests_per_minute": 2})
    one, two, clock = await pair(mongo_db, config)
    first = await one.acquire(reserved_tokens=1000)
    await one.success(first)
    clock[0] += timedelta(seconds=59)
    second = await two.acquire(reserved_tokens=1000)
    await two.success(second)
    clock[0] += timedelta(seconds=1)
    # Only the first reservation expires; both processes still share the second.
    third = await one.acquire(reserved_tokens=1000)
    await one.success(third)
    with pytest.raises(CircuitOpen):
        await two.acquire(reserved_tokens=1000)
    assert (await two.snapshot())["requests_reserved"] == 2


async def test_concurrent_instances_atomically_reserve_only_available_quota(mongo_db, settings):
    one, two, _ = await pair(mongo_db, settings.model_copy(update={"groq_requests_per_minute": 2}))
    results = await asyncio.gather(
        *((one if n % 2 else two).acquire(reserved_tokens=1000) for n in range(8)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SharedPermit) for r in results) == 2
    assert sum(isinstance(r, CircuitOpen) for r in results) == 6
    assert (await one.snapshot())["requests_reserved"] == 2


async def test_token_budget_is_not_refunded_on_timeout_or_cancellation(mongo_db, settings):
    one, two, clock = await pair(
        mongo_db, settings.model_copy(update={"groq_tokens_per_minute": 1500})
    )
    permit = await one.acquire(reserved_tokens=1000)
    await one.abandon(permit)
    with pytest.raises(CircuitOpen):
        await two.acquire(reserved_tokens=600)
    assert (await two.snapshot())["tokens_reserved"] == 1000
    clock[0] += timedelta(seconds=61)
    await two.success(await two.acquire(reserved_tokens=1000))
    assert (await one.snapshot())["tokens_reserved"] == 1000


async def test_daily_limit_is_shared_and_expired_windows_are_pruned(mongo_db, settings):
    one, two, clock = await pair(mongo_db, settings.model_copy(update={"groq_requests_per_day": 1}))
    await one.success(await one.acquire(reserved_tokens=1000))
    clock[0] += timedelta(seconds=61)
    with pytest.raises(CircuitOpen) as error:
        await two.acquire(reserved_tokens=1000)
    assert error.value.retry_after > 86000
    clock[0] += timedelta(days=1)
    await two.success(await two.acquire(reserved_tokens=1000))
    assert (await one.snapshot())["daily_requests_reserved"] == 1


async def test_global_concurrency_lease_releases_and_crashed_calls_expire(mongo_db, settings):
    one, two, clock = await pair(
        mongo_db, settings.model_copy(update={"ai_shared_max_concurrent_requests": 1})
    )
    permit = await one.acquire(reserved_tokens=1000)
    with pytest.raises(CircuitOpen):
        await two.acquire(reserved_tokens=1000)
    clock[0] += timedelta(seconds=14)
    newer = await two.acquire(reserved_tokens=1000)
    await one.success(permit)  # Late response cannot mutate the replacement's lease.
    assert (await one.snapshot())["active_leases"] == 1
    await two.success(newer)
    assert (await one.snapshot())["active_leases"] == 0


async def test_shared_cooldown_one_probe_and_recovery(mongo_db, settings):
    one, two, clock = await pair(mongo_db, settings)
    await one.failure(await one.acquire(reserved_tokens=1000))
    await two.failure(await two.acquire(reserved_tokens=1000))
    with pytest.raises(CircuitOpen):
        await one.acquire(reserved_tokens=1000)
    clock[0] += timedelta(seconds=30)
    probes = await asyncio.gather(
        *((one if n % 2 else two).acquire(reserved_tokens=1000) for n in range(6)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SharedPermit) for r in probes) == 1
    probe = next(r for r in probes if isinstance(r, SharedPermit))
    assert probe.probe
    await two.success(probe)
    assert (await one.snapshot())["state"] == "closed"


async def test_quota_opens_immediately_and_late_hint_extends_existing_outage(mongo_db, settings):
    one, two, clock = await pair(mongo_db, settings)
    slow = await one.acquire(reserved_tokens=1000)
    await two.failure(await two.acquire(reserved_tokens=1000), force_open=True, retry_after=90)
    clock[0] += timedelta(seconds=1)
    await one.failure(slow, force_open=True, retry_after=120)
    assert (await two.snapshot())["retry_after_seconds"] == 120


async def test_cancelled_or_crashed_probe_never_sticks_or_closes_newer_probe(mongo_db, settings):
    one, two, clock = await pair(mongo_db, settings)
    await one.failure(await one.acquire(reserved_tokens=1000), force_open=True)
    clock[0] += timedelta(seconds=30)
    abandoned = await one.acquire(reserved_tokens=1000)
    await one.abandon(abandoned)
    crashed = await two.acquire(reserved_tokens=1000)
    clock[0] += timedelta(seconds=14)
    newer = await one.acquire(reserved_tokens=1000)
    await two.success(crashed)
    assert (await one.snapshot())["state"] == "half_open"
    await one.success(newer)
    assert (await two.snapshot())["state"] == "closed"


async def test_missing_quota_configuration_disables_calls_and_mismatch_fails_closed(
    mongo_db, settings
):
    disabled = MongoCircuitBreaker(
        mongo_db, "groq", settings.model_copy(update={"groq_requests_per_minute": 0})
    )
    await disabled.initialize()
    with pytest.raises(CircuitOpen):
        await disabled.acquire(reserved_tokens=1000)
    assert (await disabled.snapshot())["quota_configured"] is False
    one, _, _ = await pair(mongo_db, settings)
    different = MongoCircuitBreaker(
        mongo_db, "groq", settings.model_copy(update={"groq_requests_per_minute": 99})
    )
    await different.initialize()
    with pytest.raises(CircuitOpen):
        await different.acquire(reserved_tokens=1000)
    assert (await different.snapshot())["quota_configured"] is False
    with pytest.raises(ValueError):
        await one.acquire()


async def test_configuration_budget_storage_stays_bounded_over_many_windows(mongo_db, settings):
    one, two, clock = await pair(mongo_db, settings)
    for _ in range(70):
        await one.success(await one.acquire(reserved_tokens=1000))
        clock[0] += timedelta(seconds=1)
    state = await mongo_db.sid_provider_state.find_one({"_id": one.key})
    assert len(state["minute_buckets"]) <= 61
    assert len(state["day_buckets"]) <= 25
    assert (await two.snapshot())["daily_requests_reserved"] == 70


async def test_policy_changes_preserve_quota_and_cooldown_and_refuse_active_traffic(
    mongo_db, settings
):
    one, _, clock = await pair(mongo_db, settings)
    permit = await one.acquire(reserved_tokens=1000)
    new_config = settings.model_copy(update={"groq_tokens_per_minute": 500})
    changed = MongoCircuitBreaker(mongo_db, "groq", new_config, lambda: clock[0])
    with pytest.raises(RuntimeError, match="Stop account traffic"):
        await changed.apply_policy()
    await one.failure(permit, force_open=True, retry_after=90)
    await changed.apply_policy()
    snapshot = await changed.snapshot()
    assert snapshot["tokens_reserved"] == 1000 and snapshot["requests_reserved"] == 1
    assert snapshot["state"] == "open" and snapshot["retry_after_seconds"] == 90
    assert snapshot["quota_configured"] is True
    assert (await one.snapshot())["quota_configured"] is False
