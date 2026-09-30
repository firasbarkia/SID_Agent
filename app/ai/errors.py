from typing import Literal


class ProviderFailure(Exception):
    """Only safe, normalized failure metadata may escape an adapter."""

    def __init__(
        self,
        kind: Literal["transient", "rate_limited", "configuration", "invalid_response"],
        retry_after: float | None = None,
    ) -> None:
        self.kind = kind
        self.retry_after = retry_after
        super().__init__(kind)


class ProviderRefused(Exception):
    """Do not retry a content refusal through another provider."""


class ProviderRequestRejected(Exception):
    """A non-retryable request error, not an outage."""


class ProvidersUnavailable(Exception):
    def __init__(self, retry_after: float = 30) -> None:
        self.retry_after = retry_after
        super().__init__("AI providers are temporarily unavailable")


class ServiceBusy(Exception):
    pass
