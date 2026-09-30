import os
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from pymongo.errors import AutoReconnect

from app.domain import DomainError
from app.main import create_app
from app.platform import PlatformReader
from app.worker import DraftWorker


async def verify_test_user(request):
    # Test fixture only. Production requires the existing platform's verifier.
    user_id = request.headers.get("X-Test-User")
    if user_id is None:
        raise HTTPException(401, detail={"code": "test_unauthenticated"})
    return user_id


def success(request):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": "A synthetic letter requiring candidate review and approval."
                    },
                }
            ]
        },
    )


@pytest.fixture
async def platform_records(mongo_db):
    await mongo_db.users.insert_many(
        [
            {"id": "alice", "role": "CANDIDATE", "isActive": True},
            {"id": "bob", "role": "CANDIDATE", "isActive": True},
            {"id": "inactive", "role": "CANDIDATE", "isActive": False},
            {"id": "recruiter", "role": "RECRUITER", "isActive": True},
            {"id": "recruiter-two", "role": "RECRUITER", "isActive": True},
            {"id": "admin", "role": "ADMIN", "isActive": True},
            {"id": "unlinked-recruiter", "role": "RECRUITER", "isActive": True},
        ]
    )
    await mongo_db.candidate_profiles.insert_many(
        [
            {"id": "candidate-profile-alice", "userId": "alice", "fullName": "Synthetic Alice"},
            {"id": "candidate-profile-bob", "userId": "bob", "fullName": "Synthetic Bob"},
        ]
    )
    await mongo_db.company_profiles.insert_many(
        [
            {"id": "company-one", "userId": "recruiter", "companyName": "Synthetic company"},
            {
                "id": "company-two",
                "userId": "recruiter-two",
                "companyName": "Another synthetic company",
            },
        ]
    )
    await mongo_db.job_offers.insert_many(
        [
            {"id": "offer-one", "companyId": "company-one", "status": "OPEN"},
            {"id": "offer-two", "companyId": "company-two", "status": "OPEN"},
        ]
    )
    return mongo_db


@asynccontextmanager
async def api(settings, db, handler=success, verifier=verify_test_user):
    config = settings.model_copy(
        update={
            "mongodb_uri": SecretStr(os.environ["SID_TEST_MONGODB_URI"]),
            "mongodb_database": db.name,
        }
    )
    app = create_app(config, transport=httpx.MockTransport(handler), verify_user_id=verifier)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Test-User": "alice", "X-Service-Key": "test-service-key"},
        ) as client:
            yield app, client


async def make_draft(app, client, settings):
    profile = (
        await client.post(
            "/api/v1/profiles",
            json={"content": "Student with synthetic Python university projects."},
        )
    ).json()
    accepted = await client.post(
        f"/api/v1/profiles/{profile['id']}/accept", json={"expected_version": 1, "confirmed": True}
    )
    assert accepted.status_code == 200
    request = await client.post(
        "/api/v1/application-drafts",
        headers={"Idempotency-Key": "request-one"},
        json={
            "profile_id": profile["id"],
            "profile_version": 1,
            "job_description": "PFE internship in Sfax with Python web development.",
            "language": "en",
        },
    )
    assert request.status_code == 202
    response = request.json()
    assert await DraftWorker(app.state.store.db, app.state.ai_gateway, settings).run_once()
    return profile, response


@pytest.mark.mongodb
async def test_persistent_draft_end_to_end_with_shared_gateway_and_human_approval(
    platform_records, settings
):
    async with api(settings, platform_records) as (app, client):
        profile, task = await make_draft(app, client, settings)
        draft_url = f"/api/v1/application-drafts/{task['draft_id']}"
        draft = (await client.get(draft_url)).json()
        assert draft["status"] == "draft" and draft["version"] == 1
        assert draft["requires_human_approval"] is True
        approved = await client.post(
            draft_url + "/approvals",
            json={"expected_version": 1, "confirmed": True, "destination_id": "offer-one"},
        )
        assert approved.status_code == 201
        assert approved.json()["draft_version"] == 1
        assert (await client.get(draft_url)).json()["requires_human_approval"] is False
        edited = await client.put(
            draft_url,
            json={
                "expected_version": 1,
                "cover_letter": "A candidate-edited letter that requires fresh approval.",
            },
        )
        assert edited.status_code == 200 and edited.json()["requires_human_approval"] is True
        historical = await client.get(draft_url + "/versions/1")
        assert historical.status_code == 200
        assert historical.json()["cover_letter"] != edited.json()["cover_letter"]
        assert (await client.get(f"/api/v1/profiles/{profile['id']}/versions/1")).status_code == 200
        paths = (await client.get("/openapi.json")).json()["paths"]
        assert not any(path.endswith("/submit") for path in paths)
        task_response = (await client.get(f"/api/v1/tasks/{task['task_id']}")).json()
        assert task_response["state"] == "done"
        assert "lease_token" not in task_response and "request_hash" not in task_response


@pytest.mark.mongodb
async def test_candidate_isolation_and_recruiter_and_admin_cannot_access_workspace(
    platform_records, settings
):
    async with api(settings, platform_records) as (app, client):
        profile, task = await make_draft(app, client, settings)
        client.headers["X-Test-User"] = "bob"
        assert (await client.get(f"/api/v1/profiles/{profile['id']}")).status_code == 404
        assert (
            await client.get(f"/api/v1/application-drafts/{task['draft_id']}")
        ).status_code == 404
        assert (
            await client.get(f"/api/v1/application-drafts/{task['draft_id']}/versions/1")
        ).status_code == 404
        assert (await client.get(f"/api/v1/tasks/{task['task_id']}")).status_code == 404
        response = await client.post(
            f"/api/v1/application-drafts/{task['draft_id']}/approvals",
            json={"expected_version": 1, "confirmed": True, "destination_id": "offer-one"},
        )
        assert response.status_code == 404
        for user in ("recruiter", "recruiter-two", "admin"):
            client.headers["X-Test-User"] = user
            assert (await client.get(f"/api/v1/profiles/{profile['id']}")).status_code == 403


@pytest.mark.mongodb
async def test_platform_reader_maps_diagram_and_denies_cross_company_offer_access(
    platform_records, settings
):
    reader = PlatformReader(platform_records, settings)
    principal = await reader.principal("recruiter")
    assert principal.company_id == "company-one" and principal.role == "RECRUITER"
    assert (await reader.company_offer(principal, "offer-one"))["companyId"] == principal.company_id
    with pytest.raises(DomainError) as error:
        await reader.company_offer(principal, "offer-two")
    assert error.value.status == 404
    with pytest.raises(DomainError) as error:
        await reader.principal("unlinked-recruiter")
    assert error.value.status == 403


@pytest.mark.mongodb
async def test_external_candidate_reference_is_read_only_and_owner_checked(
    platform_records, settings
):
    async with api(settings, platform_records) as (_, client):
        payload = {
            "content": "Candidate-reviewed profile content with Python skills.",
            "external_profile_id": "candidate-profile-bob",
        }
        assert (await client.post("/api/v1/profiles", json=payload)).status_code == 404
        payload["external_profile_id"] = "candidate-profile-alice"
        assert (await client.post("/api/v1/profiles", json=payload)).status_code == 201
        source = await platform_records.candidate_profiles.find_one(
            {"id": "candidate-profile-alice"}
        )
        assert "content" not in source


@pytest.mark.mongodb
async def test_account_disable_is_checked_again_on_every_request(platform_records, settings):
    async with api(settings, platform_records) as (_, client):
        profile = (
            await client.post(
                "/api/v1/profiles",
                json={"content": "Synthetic candidate profile with university projects."},
            )
        ).json()
        await platform_records.users.update_one({"id": "alice"}, {"$set": {"isActive": False}})
        response = await client.get(f"/api/v1/profiles/{profile['id']}")
        assert response.status_code == 401
        client.headers["X-Test-User"] = "unknown"
        assert (await client.get(f"/api/v1/profiles/{profile['id']}")).status_code == 401
        client.headers.pop("X-Test-User")
        assert (await client.get(f"/api/v1/profiles/{profile['id']}")).status_code == 401


@pytest.mark.mongodb
@pytest.mark.parametrize("confirmation", [False, 1, "true"])
async def test_confirmation_requires_explicit_boolean_and_ownership_cannot_be_injected(
    platform_records, settings, confirmation
):
    async with api(settings, platform_records) as (_, client):
        payload = {
            "content": "Synthetic profile with a Python university project.",
            "owner_id": "bob",
        }
        assert (await client.post("/api/v1/profiles", json=payload)).status_code == 422
        payload.pop("owner_id")
        profile = (await client.post("/api/v1/profiles", json=payload)).json()
        accepted = await client.post(
            f"/api/v1/profiles/{profile['id']}/accept",
            json={"expected_version": 1, "confirmed": confirmation},
        )
        assert accepted.status_code == 422


@pytest.mark.mongodb
async def test_database_failure_is_sanitized_at_api_boundary(
    platform_records, settings, monkeypatch
):
    async with api(settings, platform_records) as (app, client):

        async def unavailable(*args):
            raise AutoReconnect("SECRET_PASSWORD PRIVATE_CV")

        monkeypatch.setattr(app.state.store, "profile", unavailable)
        response = await client.get("/api/v1/profiles/any-id")
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "database_unavailable"
        assert "SECRET_PASSWORD" not in response.text and "PRIVATE_CV" not in response.text


@pytest.mark.mongodb
async def test_two_api_instances_share_provider_cooldown(platform_records, settings, draft_payload):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "api.groq.com":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "Fallback synthetic draft."}]},
                    }
                ]
            },
        )

    async with api(settings, platform_records, handler) as (_, first):
        async with api(settings, platform_records, handler) as (_, second):
            for client in (first, second):
                response = await client.post(
                    "/api/v1/application-drafts/preview", json=draft_payload
                )
                assert response.status_code == 200 and response.json()["fallback_used"] is True
            status = (await second.get("/api/v1/ai/status")).json()
            assert status["scope"] == "mongodb" and status["providers"][0]["state"] == "open"
    assert (
        hosts.count("api.groq.com") == 1 and hosts.count("generativelanguage.googleapis.com") == 2
    )


@pytest.mark.mongodb
async def test_readiness_fails_until_identity_and_quotas_are_configured(platform_records, settings):
    zero = settings.model_copy(
        update={"groq_requests_per_minute": 0, "gemini_requests_per_minute": 0}
    )
    async with api(zero, platform_records, verifier=None) as (_, client):
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"] == {
            "mongodb": True,
            "identity": False,
            "provider_limits": False,
        }
        assert (await client.get("/health")).status_code == 200
        response = await client.post(
            "/api/v1/profiles", json={"content": "Profile without a verified platform login."}
        )
        assert (
            response.status_code == 503
            and response.json()["detail"]["code"] == "platform_identity_not_integrated"
        )


async def test_phase2_auth_is_disabled_by_default_even_with_internal_service_key(settings):
    app = create_app(
        settings,
        transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected provider call")),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/profiles",
                headers={"X-Service-Key": "test-service-key"},
                json={"content": "A service key cannot identify a candidate user."},
            )
            assert response.status_code == 503
            assert response.json()["detail"]["code"] == "platform_identity_not_integrated"
