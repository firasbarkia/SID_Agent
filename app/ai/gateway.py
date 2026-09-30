import asyncio
import logging
from dataclasses import replace

from app.ai.circuit import CircuitBreaker, CircuitOpen
from app.ai.errors import (
    ProviderFailure,
    ProviderRefused,
    ProviderRequestRejected,
    ProvidersUnavailable,
    ServiceBusy,
)
from app.ai.providers import Generation, Provider
from app.config import Settings

logger = logging.getLogger(__name__)


class AIGateway:
    def __init__(self, providers: list[Provider], settings: Settings) -> None:
        self.providers = providers
        self.settings = settings
        self.breakers = {
            provider.name: CircuitBreaker(
                settings.ai_circuit_failure_threshold, settings.ai_circuit_recovery_seconds
            )
            for provider in providers
        }
        self._slots = asyncio.BoundedSemaphore(settings.ai_max_concurrent_requests)

    async def generate(self, system: str, prompt: str) -> Generation:
        # Fail fast instead of building an unbounded queue of expensive requests.
        if self._slots.locked():
            raise ServiceBusy()
        async with self._slots:
            try:
                async with asyncio.timeout(self.settings.ai_request_timeout_seconds):
                    return await self._generate(system, prompt)
            except TimeoutError as exc:
                raise ProvidersUnavailable() from exc

    async def _generate(self, system: str, prompt: str) -> Generation:
        retry_delays = []
        for index, provider in enumerate(self.providers):
            if not provider.configured:
                continue
            breaker = self.breakers[provider.name]
            try:
                permit = await breaker.acquire()
            except CircuitOpen as exc:
                retry_delays.append(exc.retry_after)
                continue
            try:
                async with asyncio.timeout(self.settings.ai_provider_timeout_seconds):
                    result = await provider.generate(
                        system, prompt, self.settings.ai_max_output_tokens
                    )
            except (ProviderFailure, TimeoutError) as exc:
                failure = exc if isinstance(exc, ProviderFailure) else ProviderFailure("transient")
                cooldown = failure.retry_after
                if failure.kind == "configuration":
                    cooldown = self.settings.ai_configuration_cooldown_seconds
                await breaker.failure(
                    permit,
                    force_open=failure.kind in {"rate_limited", "configuration"},
                    retry_after=cooldown,
                )
                snapshot = await breaker.snapshot()
                retry_delays.append(float(snapshot["retry_after_seconds"]) or 1.0)
                logger.warning("AI provider %s failed (%s)", provider.name, failure.kind)
            except (ProviderRefused, ProviderRequestRejected):
                # Provider answered: this is neither an outage nor a reason to reroute.
                await breaker.success(permit)
                raise
            except BaseException:
                # Includes request cancellation; never leave half-open probes stuck.
                await breaker.abandon(permit)
                raise
            else:
                await breaker.success(permit)
                return replace(result, fallback_used=index > 0)
        raise ProvidersUnavailable(min(retry_delays) if retry_delays else 30)

    async def status(self) -> list[dict]:
        return [
            {
                "provider": provider.name,
                "model": provider.model,
                "configured": provider.configured,
                **await self.breakers[provider.name].snapshot(),
            }
            for provider in self.providers
        ]
