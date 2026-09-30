import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from app.ai.errors import ProviderFailure, ProviderRefused, ProviderRequestRejected
from app.ai.providers import GeminiProvider, GroqProvider, retry_delay


async def test_groq_request_and_response_contract():
    def handler(request):
        payload = json.loads(request.content)
        assert request.url == "https://api.groq.com/openai/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        assert payload["model"] == "llama-3.1-8b-instant"
        assert payload["messages"] == [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "data"},
        ]
        assert payload["max_completion_tokens"] == 100
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": " letter "}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GroqProvider(client, "secret", "llama-3.1-8b-instant").generate(
            "system", "data", 100
        )
    assert result.text == "letter"


async def test_gemini_request_uses_header_auth_and_excludes_thought_parts():
    def handler(request):
        payload = json.loads(request.content)
        assert request.headers["x-goog-api-key"] == "secret"
        assert "secret" not in str(request.url)
        assert payload["systemInstruction"]["parts"][0]["text"] == "system"
        assert payload["contents"][0]["parts"][0]["text"] == "data"
        assert payload["generationConfig"]["maxOutputTokens"] == 100
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"text": "internal thought", "thought": True},
                                {"text": "Cover "},
                                {"text": "letter"},
                            ]
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GeminiProvider(client, "secret", "gemini-3.1-flash-lite").generate(
            "system", "data", 100
        )
    assert result.text == "Cover letter"


@pytest.mark.parametrize(
    "status,kind",
    [
        (429, "rate_limited"),
        (500, "transient"),
        (503, "transient"),
        (408, "transient"),
        (401, "configuration"),
        (403, "configuration"),
        (404, "configuration"),
    ],
)
async def test_http_failures_are_classified_without_exposing_body(status, kind):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            json={"error": {"message": "PRIVATE PROVIDER BODY"}},
            headers={"Retry-After": "60"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderFailure) as error:
            await GroqProvider(client, "secret", "model").generate("s", "p", 100)
    assert error.value.kind == kind
    assert "PRIVATE" not in str(error.value)
    if status == 429:
        assert error.value.retry_after == 60


async def test_bad_request_is_not_treated_as_outage():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": {"message": "bad request"}})
        )
    ) as client:
        with pytest.raises(ProviderRequestRejected):
            await GroqProvider(client, "secret", "model").generate("s", "p", 100)


async def test_invalid_gemini_key_reported_as_http_400_is_configuration_error():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            400, json={"error": {"details": [{"reason": "API_KEY_INVALID"}]}}
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ProviderFailure, match="configuration"):
            await GeminiProvider(client, "secret", "model").generate("s", "p", 100)


async def test_network_failure_is_transient():
    def handler(request):
        raise httpx.ConnectError("network failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderFailure, match="transient"):
            await GroqProvider(client, "secret", "model").generate("s", "p", 100)


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"choices": []},
        {"choices": [{"finish_reason": "length", "message": {"content": "truncated"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": " "}}]},
    ],
)
async def test_invalid_or_truncated_groq_responses_are_rejected(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ProviderFailure, match="invalid_response"):
            await GroqProvider(client, "secret", "model").generate("s", "p", 100)


@pytest.mark.parametrize(
    "provider,body",
    [
        (GroqProvider, {"choices": [{"finish_reason": "stop", "message": {"refusal": "blocked"}}]}),
        (GeminiProvider, {"promptFeedback": {"blockReason": "SAFETY"}}),
        (GeminiProvider, {"candidates": [{"finishReason": "SAFETY"}]}),
    ],
)
async def test_content_refusals_are_distinct_from_outages(provider, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as client:
        with pytest.raises(ProviderRefused):
            await provider(client, "secret", "model").generate("s", "p", 100)


async def test_gemini_truncated_output_is_not_returned_as_complete():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"candidates": [{"finishReason": "MAX_TOKENS"}]}
            )
        )
    ) as client:
        with pytest.raises(ProviderFailure, match="invalid_response"):
            await GeminiProvider(client, "secret", "model").generate("s", "p", 100)


@pytest.mark.parametrize("header,expected", [("NaN", None), ("bad", None), ("-2", 0), ("10", 10)])
def test_retry_after_header_validation(header, expected):
    assert retry_delay(httpx.Response(429, headers={"Retry-After": header})) == expected


def test_retry_after_http_date_and_gemini_retry_info():
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=60))
    assert 58 <= retry_delay(httpx.Response(429, headers={"Retry-After": date})) <= 60
    response = httpx.Response(
        429,
        json={
            "error": {
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12.5s"}
                ]
            }
        },
    )
    assert retry_delay(response) == 12.5
