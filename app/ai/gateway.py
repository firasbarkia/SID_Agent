import asyncio
import logging
from collections.abc import Callable
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
    def __init__(self, providers: list[Provider], settings: Settings, *, breakers=None) -> None:
        self.providers = providers
        self.settings = settings
        self.scope = "mongodb" if breakers is not None else "process"
        self.breakers = (
            breakers
            if breakers is not None
            else {
                provider.name: CircuitBreaker(
                    settings.ai_circuit_failure_threshold, settings.ai_circuit_recovery_seconds
                )
                for provider in providers
            }
        )
        self._slots = asyncio.BoundedSemaphore(settings.ai_max_concurrent_requests)

    async def generate(
        self, system: str, prompt: str, *, validate: Callable[[str], object] | None = None
    ) -> Generation:
        # Fail fast instead of building an unbounded queue of expensive requests.
        if self._slots.locked():
            raise ServiceBusy()
        async with self._slots:
            try:
                async with asyncio.timeout(self.settings.ai_request_timeout_seconds):
                    return await self._generate(system, prompt, validate)
            except TimeoutError as exc:
                raise ProvidersUnavailable() from exc

    async def _generate(self, system: str, prompt: str, validate=None) -> Generation:
        retry_delays = []
        for index, provider in enumerate(self.providers):
            if not provider.configured:
                continue
            breaker = self.breakers[provider.name]
            try:
                # Conservative byte-based reservation plus output cap and framing.
                # No refunds after an ambiguous timeout: the provider may charge it.
                reserved_tokens = (
                    len(system.encode())
                    + len(prompt.encode())
                    + self.settings.ai_max_output_tokens
                    + 256
                )
                permit = await breaker.acquire(reserved_tokens=reserved_tokens)
            except CircuitOpen as exc:
                retry_delays.append(exc.retry_after)
                continue
            try:
                async with asyncio.timeout(self.settings.ai_provider_timeout_seconds):
                    result = await provider.generate(
                        system, prompt, self.settings.ai_max_output_tokens
                    )
                    if validate is not None:
                        try:
                            validate(result.text)
                        except ValueError as exc:
                            raise ProviderFailure("invalid_response") from exc
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
