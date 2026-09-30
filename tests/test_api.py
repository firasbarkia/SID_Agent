from contextlib import asynccontextmanager

import httpx
import pytest

from app.main import create_app


@asynccontextmanager
async def api_client(settings, handler):
    app = create_app(settings, transport=httpx.MockTransport(handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Service-Key": "test-service-key"},
        ) as client:
            yield client


def groq_success(request):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "stop", "message": {"content": "Review this draft letter."}}
            ]
        },
    )


async def test_draft_preview_is_explicitly_unsaved_and_requires_approval(settings, draft_payload):
    async with api_client(settings, groq_success) as client:
        response = await client.post("/api/v1/application-drafts/preview", json=draft_payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "draft"
        assert data["requires_human_approval"] is True
        assert data["persisted"] is False
        assert data["provider"] == "groq"
        assert data["fallback_used"] is False
        assert (await client.post("/api/v1/application-drafts/submit")).status_code == 404


async def test_real_adapters_fallback_and_quota_cooldown_through_api(settings, draft_payload):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "api.groq.com":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "STOP", "content": {"parts": [{"text": "Fallback draft."}]}}
                ]
            },
        )

    async with api_client(settings, handler) as client:
        for _ in range(2):
            response = await client.post("/api/v1/application-drafts/preview", json=draft_payload)
            assert response.status_code == 200
            assert response.json()["provider"] == "gemini"
            assert response.json()["fallback_used"] is True
        status = (await client.get("/api/v1/ai/status")).json()
        assert status["scope"] == "process"
        assert status["providers"][0]["state"] == "open"
        assert "test-groq-key" not in str(status)
    assert hosts.count("api.groq.com") == 1
    assert hosts.count("generativelanguage.googleapis.com") == 2


async def test_both_down_returns_controlled_503_without_provider_details(settings, draft_payload):
    def handler(request):
        return httpx.Response(503, text="secret-key PRIVATE_CV")

    async with api_client(settings, handler) as client:
        response = await client.post("/api/v1/application-drafts/preview", json=draft_payload)
    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert response.json()["detail"]["code"] == "ai_unavailable"
    assert "secret-key" not in response.text
    assert "PRIVATE_CV" not in response.text


@pytest.mark.parametrize("key", [None, "wrong"])
async def test_authentication_blocks_provider_calls(settings, draft_payload, key):
    def forbidden(request):
        pytest.fail("Unauthenticated request reached an AI provider")

    async with api_client(settings, forbidden) as client:
        client.headers.pop("X-Service-Key")
        headers = {"X-Service-Key": key} if key else {}
        response = await client.post(
            "/api/v1/application-drafts/preview", json=draft_payload, headers=headers
        )
        assert response.status_code == 401
        assert (await client.get("/api/v1/ai/status", headers=headers)).status_code == 401


async def test_missing_service_key_fails_closed(settings, draft_payload):
    from pydantic import SecretStr

    config = settings.model_copy(update={"sid_service_api_key": SecretStr("")})
    async with api_client(
        config, lambda request: pytest.fail("Unexpected provider call")
    ) as client:
        response = await client.post("/api/v1/application-drafts/preview", json=draft_payload)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "service_auth_not_configured"


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_profile": "   "},
        {"candidate_profile": "x" * 12001},
        {"language": "invalid"},
        {"submit": True},
    ],
)
async def test_invalid_input_never_calls_provider(settings, draft_payload, change):
    async with api_client(
        settings, lambda request: pytest.fail("Invalid input reached provider")
    ) as c:
        response = await c.post("/api/v1/application-drafts/preview", json=draft_payload | change)
        assert response.status_code == 422


async def test_health_and_docs_do_not_spend_quota(settings):
    async with api_client(
        settings, lambda request: pytest.fail("Health called provider")
    ) as client:
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/health/ready")).status_code == 200
        assert (await client.get("/docs")).status_code == 200
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert "/api/v1/application-drafts/preview" in paths


async def test_readiness_distinguishes_no_provider_configuration(settings):
    from pydantic import SecretStr

    config = settings.model_copy(
        update={"groq_api_key": SecretStr(""), "gemini_api_key": SecretStr("")}
    )
    async with api_client(config, lambda request: pytest.fail("Unexpected call")) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503


@pytest.mark.parametrize("status,expected", [(400, 502), (200, 422)])
async def test_rejections_never_reroute_at_http_boundary(settings, draft_payload, status, expected):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(
            status, json={"choices": [{"finish_reason": "content_filter", "message": {}}]}
        )

    async with api_client(settings, handler) as client:
        response = await client.post("/api/v1/application-drafts/preview", json=draft_payload)
        assert response.status_code == expected
    assert calls == ["api.groq.com"]
