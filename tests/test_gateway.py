import asyncio
from collections import deque

import pytest

from app.ai.circuit import CircuitBreaker
from app.ai.errors import (
    ProviderFailure,
    ProviderRefused,
    ProviderRequestRejected,
    ProvidersUnavailable,
    ServiceBusy,
)
from app.ai.gateway import AIGateway
from app.ai.providers import Generation


class StubProvider:
    def __init__(self, name, actions, configured=True):
        self.name = name
        self.model = "test-model"
        self.configured = configured
        self.actions = deque(actions)
        self.calls = 0

    async def generate(self, system, prompt, max_tokens):
        self.calls += 1
        action = self.actions.popleft()
        if isinstance(action, Exception):
            raise action
        if callable(action):
            action = await action()
        return Generation(action, self.name, self.model)


async def test_primary_success_never_calls_fallback(settings):
    primary = StubProvider("groq", ["letter"])
    fallback = StubProvider("gemini", [])
    result = await AIGateway([primary, fallback], settings).generate("system", "data")
    assert result.provider == "groq"
    assert not result.fallback_used
    assert fallback.calls == 0


async def test_failures_fall_back_then_open_circuit_skips_primary(settings):
    primary = StubProvider("groq", [ProviderFailure("transient"), ProviderFailure("transient")])
    fallback = StubProvider("gemini", ["one", "two", "three"])
    gateway = AIGateway([primary, fallback], settings)
    for _ in range(3):
        assert (await gateway.generate("system", "data")).fallback_used
    assert primary.calls == 2
    assert fallback.calls == 3
    assert (await gateway.status())[0]["state"] == "open"


@pytest.mark.parametrize("kind,cooldown", [("rate_limited", 90), ("configuration", 300)])
async def test_quota_and_configuration_failures_open_immediately(settings, kind, cooldown):
    primary = StubProvider("groq", [ProviderFailure(kind, 90)])
    fallback = StubProvider("gemini", ["one", "two"])
    gateway = AIGateway([primary, fallback], settings)
    await gateway.generate("system", "data")
    await gateway.generate("system", "data")
    status = (await gateway.status())[0]
    assert primary.calls == 1
    assert status["state"] == "open"
    assert cooldown - 2 <= status["retry_after_seconds"] <= cooldown


async def test_both_providers_have_independent_circuits(settings):
    primary = StubProvider("groq", [ProviderFailure("rate_limited", 60)])
    fallback = StubProvider("gemini", [ProviderFailure("rate_limited", 120)])
    gateway = AIGateway([primary, fallback], settings)
    for _ in range(2):
        with pytest.raises(ProvidersUnavailable):
            await gateway.generate("system", "data")
    assert primary.calls == fallback.calls == 1
    assert all(x["state"] == "open" for x in await gateway.status())


@pytest.mark.parametrize("failure", [ProviderRefused(), ProviderRequestRejected()])
async def test_non_retryable_failures_do_not_trigger_fallback(settings, failure):
    primary = StubProvider("groq", [failure])
    fallback = StubProvider("gemini", [])
    gateway = AIGateway([primary, fallback], settings)
    with pytest.raises(type(failure)):
        await gateway.generate("system", "data")
    assert fallback.calls == 0
    assert (await gateway.status())[0]["consecutive_failures"] == 0


async def test_missing_primary_key_uses_configured_fallback(settings):
    primary = StubProvider("groq", [], configured=False)
    fallback = StubProvider("gemini", ["letter"])
    result = await AIGateway([primary, fallback], settings).generate("system", "data")
    assert result.provider == "gemini"
    assert result.fallback_used
    assert primary.calls == 0


async def test_no_providers_returns_unavailable_without_network(settings):
    gateway = AIGateway([StubProvider("groq", [], False)], settings)
    with pytest.raises(ProvidersUnavailable):
        await gateway.generate("system", "data")


async def test_provider_timeout_uses_fallback(settings):
    async def slow():
        await asyncio.Event().wait()

    primary = StubProvider("groq", [slow])
    fallback = StubProvider("gemini", ["letter"])
    config = settings.model_copy(update={"ai_provider_timeout_seconds": 0.01})
    assert (await AIGateway([primary, fallback], config).generate("s", "p")).fallback_used


async def test_total_timeout_bounds_work_and_releases_capacity(settings):
    async def slow():
        await asyncio.Event().wait()

    primary = StubProvider("groq", [slow, "recovered"])
    config = settings.model_copy(update={"ai_request_timeout_seconds": 0.01})
    gateway = AIGateway([primary], config)
    with pytest.raises(ProvidersUnavailable):
        await gateway.generate("s", "p")
    assert (await gateway.generate("s", "p")).text == "recovered"


async def test_concurrency_limit_rejects_excess_work(settings):
    started, finish = asyncio.Event(), asyncio.Event()

    async def pending():
        started.set()
        await finish.wait()
        return "letter"

    provider = StubProvider("groq", [pending])
    config = settings.model_copy(update={"ai_max_concurrent_requests": 1})
    gateway = AIGateway([provider], config)
    first = asyncio.create_task(gateway.generate("s", "p"))
    await started.wait()
    with pytest.raises(ServiceBusy):
        await gateway.generate("s", "p")
    finish.set()
    await first
    assert provider.calls == 1


async def test_cancelled_half_open_request_releases_probe_and_capacity(settings):
    started = asyncio.Event()

    async def pending():
        started.set()
        await asyncio.Event().wait()

    provider = StubProvider("groq", [pending, "recovered"])
    gateway = AIGateway([provider], settings)
    clock = [0.0]
    breaker = CircuitBreaker(1, 1, lambda: clock[0])
    gateway.breakers["groq"] = breaker
    await breaker.failure(await breaker.acquire())
    clock[0] = 1
    task = asyncio.create_task(gateway.generate("s", "p"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await gateway.generate("s", "p")).text == "recovered"
    assert (await breaker.snapshot())["state"] == "closed"


async def test_logs_do_not_contain_prompt_or_upstream_body(settings, caplog):
    gateway = AIGateway([StubProvider("groq", [ProviderFailure("transient")])], settings)
    with pytest.raises(ProvidersUnavailable):
        await gateway.generate("private-system", "PRIVATE_CV_123")
    assert "PRIVATE_CV_123" not in caplog.text
    assert "private-system" not in caplog.text
    assert "transient" in caplog.text
