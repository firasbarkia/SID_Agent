import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from app.ai.errors import ProviderFailure, ProviderRefused, ProviderRequestRejected


@dataclass(frozen=True)
class Generation:
    text: str
    provider: str
    model: str
    fallback_used: bool = False


class Provider(Protocol):
    name: str
    model: str
    configured: bool

    async def generate(self, system: str, prompt: str, max_tokens: int) -> Generation: ...


def retry_delay(response: httpx.Response) -> float | None:
    """Support HTTP seconds/date and Gemini RetryInfo; bound malformed extremes."""
    value = response.headers.get("retry-after")
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                seconds = (date - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = float("nan")
        if math.isfinite(seconds):
            return min(86400.0, max(0.0, seconds))
    try:
        details = response.json()["error"]["details"]
        for detail in details:
            if detail.get("@type", "").endswith("google.rpc.RetryInfo"):
                seconds = float(detail["retryDelay"].removesuffix("s"))
                if math.isfinite(seconds):
                    return min(86400.0, max(0.0, seconds))
    except (ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


async def post_json(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict
) -> dict:
    try:
        response = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise ProviderFailure("transient") from exc
    status = response.status_code
    if status == 429:
        raise ProviderFailure("rate_limited", retry_delay(response))
    if status in (401, 403, 404):
        raise ProviderFailure("configuration")
    if status in (408, 425) or status >= 500:
        raise ProviderFailure("transient", retry_delay(response))
    if not response.is_success:
        # Some APIs report unavailable models / invalid API keys as HTTP 400.
        try:
            error = response.json().get("error", {})
            codes = {str(error.get("code")), str(error.get("type"))}
            codes.update(str(item.get("reason")) for item in error.get("details", []))
        except (ValueError, TypeError, AttributeError):
            codes = set()
        if codes & {
            "model_decommissioned",
            "model_not_found",
            "invalid_api_key",
            "API_KEY_INVALID",
        }:
            raise ProviderFailure("configuration")
        if codes & {"content_policy_violation", "content_filter"}:
            raise ProviderRefused()
        raise ProviderRequestRejected()
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Expected an object")
        return result
    except ValueError as exc:
        raise ProviderFailure("invalid_response") from exc


def checked_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 20000:
        raise ProviderFailure("invalid_response")
    return value.strip()


class GroqProvider:
    name = "groq"

    def __init__(self, client: httpx.AsyncClient, api_key: str, model: str) -> None:
        self.client = client
        self._api_key = api_key
        self.model = model
        self.configured = bool(api_key)

    async def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        data = await post_json(
            self.client,
            "https://api.groq.com/openai/v1/chat/completions",
            {"Authorization": f"Bearer {self._api_key}"},
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_completion_tokens": max_tokens,
                "stream": False,
            },
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                raise ProviderRefused()
            if choice.get("finish_reason") != "stop":
                raise ProviderFailure("invalid_response")
            text = checked_text(message.get("content"))
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderFailure("invalid_response") from exc
        return Generation(text, self.name, self.model)


class GeminiProvider:
    name = "gemini"

    def __init__(self, client: httpx.AsyncClient, api_key: str, model: str) -> None:
        self.client = client
        self._api_key = api_key
        self.model = model
        self.configured = bool(api_key)

    async def generate(self, system: str, prompt: str, max_tokens: int) -> Generation:
        data = await post_json(
            self.client,
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            {"x-goog-api-key": self._api_key},
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens},
            },
        )
        try:
            if data.get("promptFeedback", {}).get("blockReason"):
                raise ProviderRefused()
            candidate = data["candidates"][0]
            reason = candidate.get("finishReason")
            if reason in {
                "SAFETY",
                "RECITATION",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
                "SPII",
                "IMAGE_SAFETY",
            }:
                raise ProviderRefused()
            if reason != "STOP":
                raise ProviderFailure("invalid_response")
            parts = candidate["content"]["parts"]
            text = checked_text(
                "".join(
                    part["text"] for part in parts if "text" in part and not part.get("thought")
                )
            )
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderFailure("invalid_response") from exc
        return Generation(text, self.name, self.model)
