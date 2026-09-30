import asyncio

import pytest

from app.ai.circuit import CircuitBreaker, CircuitOpen, Permit


async def test_success_resets_failures_before_threshold():
    circuit = CircuitBreaker(failure_threshold=2)
    await circuit.failure(await circuit.acquire())
    assert (await circuit.snapshot())["consecutive_failures"] == 1
    await circuit.success(await circuit.acquire())
    await circuit.failure(await circuit.acquire())
    assert (await circuit.snapshot())["state"] == "closed"


async def test_open_skips_calls_and_successful_probe_recovers():
    clock = [100.0]
    circuit = CircuitBreaker(2, 30, lambda: clock[0])
    await circuit.failure(await circuit.acquire())
    await circuit.failure(await circuit.acquire())
    with pytest.raises(CircuitOpen) as error:
        await circuit.acquire()
    assert error.value.retry_after == 30
    clock[0] += 30
    probe = await circuit.acquire()
    assert probe.probe
    await circuit.success(probe)
    assert (await circuit.snapshot())["state"] == "closed"
    assert not (await circuit.acquire()).probe


async def test_retry_after_opens_immediately_and_delays_probe():
    clock = [0.0]
    circuit = CircuitBreaker(3, 30, lambda: clock[0])
    await circuit.failure(await circuit.acquire(), force_open=True, retry_after=90)
    clock[0] = 35
    with pytest.raises(CircuitOpen) as error:
        await circuit.acquire()
    assert error.value.retry_after == 55
    clock[0] = 90
    assert (await circuit.acquire()).probe


async def test_only_one_concurrent_half_open_probe():
    clock = [0.0]
    circuit = CircuitBreaker(1, 1, lambda: clock[0])
    await circuit.failure(await circuit.acquire())
    clock[0] = 1
    permits = await asyncio.gather(*(circuit.acquire() for _ in range(10)), return_exceptions=True)
    assert sum(isinstance(value, Permit) for value in permits) == 1
    assert sum(isinstance(value, CircuitOpen) for value in permits) == 9


async def test_failed_probe_reopens_and_abandoned_probe_can_retry():
    clock = [0.0]
    circuit = CircuitBreaker(1, 1, lambda: clock[0])
    await circuit.failure(await circuit.acquire())
    clock[0] = 1
    probe = await circuit.acquire()
    await circuit.abandon(probe)
    replacement = await circuit.acquire()
    assert replacement.probe
    await circuit.failure(replacement)
    assert (await circuit.snapshot())["state"] == "open"


async def test_late_success_cannot_close_newly_opened_circuit():
    circuit = CircuitBreaker(1)
    slow = await circuit.acquire()
    fast = await circuit.acquire()
    await circuit.failure(fast)
    await circuit.success(slow)
    assert (await circuit.snapshot())["state"] == "open"


async def test_late_failure_cannot_reopen_recovered_circuit():
    clock = [0.0]
    circuit = CircuitBreaker(1, 1, lambda: clock[0])
    slow = await circuit.acquire()
    await circuit.failure(await circuit.acquire())
    clock[0] = 1
    await circuit.success(await circuit.acquire())
    await circuit.failure(slow, force_open=True, retry_after=90)
    assert (await circuit.snapshot())["state"] == "closed"


async def test_concurrent_quota_response_extends_existing_cooldown():
    clock = [0.0]
    circuit = CircuitBreaker(1, 30, lambda: clock[0])
    slow = await circuit.acquire()
    await circuit.failure(await circuit.acquire())
    clock[0] = 10
    await circuit.failure(slow, force_open=True, retry_after=90)
    clock[0] = 30
    with pytest.raises(CircuitOpen) as error:
        await circuit.acquire()
    assert error.value.retry_after == 70
    clock[0] = 100
    assert (await circuit.acquire()).probe


@pytest.mark.parametrize("threshold,recovery", [(0, 1), (1, 0), (-1, 30)])
def test_invalid_circuit_configuration(threshold, recovery):
    with pytest.raises(ValueError):
        CircuitBreaker(threshold, recovery)
