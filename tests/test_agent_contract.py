import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.persistence import get_store
from app.contracts.models import AcceptedProfileSync, ChatReceipt, GeneratePackRequest
from app.cv.worker import CVWorker
from app.db.store import Store
from app.demo import create_demo_app, simulated_response
from app.domain import ProfileCreate
from app.identity import Principal, get_principal
from app.main import create_app
from app.matching.models import SyntheticCorpus
from app.matching.synthetic import SyntheticOfferSearch
from app.worker import DraftWorker

DATA = Path(__file__).resolve().parents[1] / "data/contracts"


@pytest.mark.mongodb
async def test_consumer_example_exercises_all_three_facades_with_demo_workers(
    settings, mongo_db, monkeypatch
):
    from examples.agent_consumer import run

    @asynccontextmanager
    async def connection(config):
        yield mongo_db

    monkeypatch.setattr("app.main.mongo_connection", connection)
    app = create_demo_app(settings, live=False)
    async with app.router.lifespan_context(app):
        await run("http://127.0.0.1", transport=httpx.ASGITransport(app=app))
    assert await mongo_db.sid_cv_imports.count_documents({"status": "accepted"}) == 1
    assert await mongo_db.sid_drafts.count_documents({"status": "draft"}) == 1
    assert await mongo_db.sid_approvals.count_documents({}) == 0


@pytest.mark.mongodb
async def test_advice_branch_returns_typed_references_through_facade(
    settings, mongo_db, monkeypatch
):
    from test_conversations import Knowledge

    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        app.state.knowledge_search = Knowledge()
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        answer = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "expected_revision": 0,
                "message": "Help me improve Python skills",
            },
            headers={"Idempotency-Key": "facade-advice-1"},
        )
        assert answer.status_code == 200
        turn = ChatReceipt.model_validate(answer.json())
        assert turn.response.status == "answered" and turn.response.references[0].cited
        assert turn.response.knowledge_generation == "test-generation"
        assert turn.response.search_mode == "disabled" and turn.response.results == []


def test_facade_openapi_has_typed_requests_outputs_errors_and_combined_auth(settings):
    schema = create_app(settings).openapi()
    for path in ("parse-cv", "generate-pack", "chat"):
        operation = schema["paths"][f"/agent/{path}"]["post"]
        status = "200" if path == "chat" else "202"
        assert "$ref" in operation["responses"][status]["content"]["application/json"]["schema"]
        assert operation["security"] == [{"APIKeyHeader": [], "PlatformUserToken": []}]
        assert "anyOf" in operation["responses"]["422"]["content"]["application/json"]["schema"]
        assert any(p["name"].casefold() == "idempotency-key" for p in operation["parameters"])
    assert "application/pdf" in schema["paths"]["/agent/parse-cv"]["post"]["requestBody"]["content"]
    for path in (
        "/api/v1/tasks/{task_id}",
        "/api/v1/cv-imports/{import_id}",
        "/api/v1/application-drafts/{draft_id}",
    ):
        assert (
            "$ref"
            in schema["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
                "schema"
            ]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "RECRUITER"),
        ("job_description", "fake job"),
        ("offer_version", True),
        ("profile_version", 0),
    ],
)
def test_pack_contract_rejects_client_role_text_and_invalid_versions(field, value):
    payload = {
        "profile_id": "profile",
        "profile_version": 1,
        "offer_id": "offer",
        "offer_version": 1,
    }
    with pytest.raises(ValidationError):
        GeneratePackRequest.model_validate({**payload, field: value})


def test_profile_sync_contract_needs_exact_acceptance_and_boolean_confirmation():
    payload = json.loads((DATA / "requests-v1.json").read_text("utf-8"))["accepted_profile_sync"]
    assert AcceptedProfileSync.model_validate(payload).confirmed is True
    for value in (False, 1, "true"):
        with pytest.raises(ValidationError):
            AcceptedProfileSync.model_validate({**payload, "confirmed": value})


async def test_default_facade_fails_closed_without_identity_or_canonical_integration(settings):
    app = create_app(settings)
    payload = {
        "profile_id": "profile",
        "profile_version": 1,
        "offer_id": "offer",
        "offer_version": 1,
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post("/agent/generate-pack", json=payload)).status_code == 401
            client.headers["X-Service-Key"] = "test-service-key"
            blocked = await client.post("/agent/generate-pack", json=payload)
            assert blocked.json()["detail"]["code"] == "platform_identity_not_integrated"
            caps = (await client.get("/agent/capabilities")).json()
            assert caps["offer_search"] == "disabled" and not caps["real_private_matching_enabled"]
            app.dependency_overrides[get_principal] = lambda: Principal(
                user_id="one", role="CANDIDATE"
            )
            app.dependency_overrides[get_store] = lambda: SimpleNamespace()
            blocked = await client.post(
                "/agent/generate-pack", json=payload, headers={"Idempotency-Key": "fixture-pack-1"}
            )
            assert blocked.json()["detail"]["code"] == "canonical_offers_not_integrated"


async def test_parse_facade_retains_pdf_body_allowance_and_size_cap(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            medium = b"%PDF-1.7\n" + b"x" * (settings.http_json_max_bytes + 1)
            # It reaches auth, rather than mistakenly receiving the smaller JSON cap.
            assert (await client.post("/agent/parse-cv", content=medium)).status_code == 401
            too_large = await client.post("/agent/parse-cv", content=b"x" * (5 * 1024 * 1024 + 1))
            assert too_large.status_code == 413


@asynccontextmanager
async def facade(settings, db, monkeypatch, *, synthetic=True, handler=simulated_response):
    @asynccontextmanager
    async def connection(config):
        yield db

    async def verify(request):
        token = request.headers.get("Authorization", "")
        identities = {
            "Bearer fixture-candidate": "candidate-one",
            "Bearer fixture-other": "candidate-two",
            "Bearer fixture-recruiter": "recruiter",
        }
        if token not in identities:
            raise HTTPException(401, detail={"code": "fixture_token_invalid"})
        return identities[token]

    for user, role in (
        ("candidate-one", "CANDIDATE"),
        ("candidate-two", "CANDIDATE"),
        ("recruiter", "RECRUITER"),
    ):
        await db.users.insert_one({"id": user, "isActive": True, "role": role})
    await db.company_profiles.insert_one(
        {"id": "company", "userId": "recruiter", "companyName": "Fixture"}
    )
    monkeypatch.setattr("app.main.mongo_connection", connection)
    app = create_app(
        settings,
        verify_user_id=verify,
        transport=httpx.MockTransport(handler),
        synthetic_offer_search=SyntheticOfferSearch.from_fixture() if synthetic else None,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={
                "X-Service-Key": "test-service-key",
                "Authorization": "Bearer fixture-candidate",
            },
        ) as client:
            yield client, app


@pytest.mark.mongodb
async def test_pack_facade_reuses_worker_versions_and_source_bound_idempotency(
    settings, mongo_db, monkeypatch, candidate_user
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        store = Store(mongo_db)
        profile = await store.create_profile(
            candidate_user, ProfileCreate(content="Student with Python and SQL projects.")
        )
        await store.accept_profile(candidate_user, profile["_id"], 1)
        payload = {
            "profile_id": profile["_id"],
            "profile_version": 1,
            "offer_id": "fixture-python-pfe",
            "offer_version": 1,
        }
        headers = {"Idempotency-Key": "fixture-pack-1"}
        first = await client.post("/agent/generate-pack", json=payload, headers=headers)
        assert first.status_code == 202
        receipt = first.json()
        assert first.headers["Location"] == receipt["poll_url"]
        assert receipt["sources"]["kind"] == "synthetic_offer" and not receipt["submitted"]
        assert (
            await client.post("/agent/generate-pack", json=payload, headers=headers)
        ).json() == receipt
        assert await mongo_db.sid_tasks.count_documents({}) == 1
        conflict = await client.post(
            "/agent/generate-pack",
            json={**payload, "offer_id": "fixture-react-pfe", "offer_version": 2},
            headers=headers,
        )
        assert conflict.status_code == 409
        assert await DraftWorker(mongo_db, app.state.ai_gateway, settings).run_once()
        assert (await client.get(receipt["poll_url"])).json()["state"] == "done"
        draft = (await client.get(receipt["draft_url"])).json()
        assert draft["source_snapshot"]["offer"] == receipt["sources"]
        assert draft["requires_human_approval"] and draft["dossier_analysis"]
        assert await mongo_db.sid_approvals.count_documents({}) == 0


@pytest.mark.mongodb
async def test_parse_receipt_replay_review_and_isolation(settings, mongo_db, monkeypatch):
    from test_cv import pdf

    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        headers = {"Content-Type": "application/pdf", "Idempotency-Key": "fixture-cv-001"}
        response = await client.post(
            "/agent/parse-cv",
            content=pdf(["Name: Synthetic Candidate", "Skill: Python"]),
            headers=headers,
        )
        assert response.status_code == 202
        receipt = response.json()
        assert receipt["sync_status"] == "not_integrated" and receipt["requires_human_approval"]
        assert await CVWorker(mongo_db, app.state.ai_gateway, settings).run_once()
        review = await client.get(receipt["poll_url"])
        assert review.status_code == 200 and review.json()["status"] == "review"
        assert review.json()["facts"] and review.json()["evaluation"]["kind"] == "completeness"
        assert (
            await client.get(receipt["poll_url"], headers={"Authorization": "Bearer fixture-other"})
        ).status_code == 404
        replay = await client.post(
            "/agent/parse-cv",
            content=pdf(["Name: Synthetic Candidate", "Skill: Python"]),
            headers=headers,
        )
        assert replay.status_code == 202 and replay.json()["import_id"] == receipt["import_id"]
        assert replay.json()["status"] == "review"


@pytest.mark.mongodb
async def test_candidate_chat_filters_followup_replay_and_recruiter_gate(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        conversation = (await client.post("/api/v1/conversations", json={"language": "fr"})).json()
        payload = {
            "conversation_id": conversation["id"],
            "expected_revision": 0,
            "message": "Je cherche un stage PFE Python à Sfax",
        }
        first = await client.post(
            "/agent/chat", json=payload, headers={"Idempotency-Key": "fixture-chat-1"}
        )
        assert first.status_code == 200
        turn = ChatReceipt.model_validate(first.json())
        assert turn.response.status == "matches_found"
        assert [offer.id for offer in turn.response.results] == ["fixture-python-pfe"]
        assert turn.response.search_mode == "synthetic" and not turn.submitted
        replay = await client.post(
            "/agent/chat", json=payload, headers={"Idempotency-Key": "fixture-chat-1"}
        )
        assert replay.json() == first.json()
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 1
        changed = await client.post(
            "/agent/chat",
            json={**payload, "expected_revision": 1, "message": "Tunis instead"},
            headers={"Idempotency-Key": "fixture-chat-2"},
        )
        assert changed.status_code == 200
        assert [offer["id"] for offer in changed.json()["response"]["results"]] == [
            "fixture-tunis-pfe"
        ]
        assert changed.json()["response"]["filters"]["contract"] == "PFE"
        assert (
            await client.post(
                "/agent/chat",
                json=payload,
                headers={
                    "Idempotency-Key": "fixture-chat-3",
                    "Authorization": "Bearer fixture-other",
                },
            )
        ).status_code == 404
        recruiter = {"Authorization": "Bearer fixture-recruiter"}
        other = (
            await client.post("/api/v1/conversations", json={"language": "en"}, headers=recruiter)
        ).json()
        gated = await client.post(
            "/agent/chat",
            json={
                "conversation_id": other["id"],
                "expected_revision": 0,
                "message": "Find Python candidates",
            },
            headers={**recruiter, "Idempotency-Key": "recruiter-chat-1"},
        )
        assert gated.json()["response"]["status"] == "matching_unavailable"
        assert gated.json()["response"]["target"] == "candidates"


@pytest.mark.mongodb
async def test_changed_index_during_generation_cannot_commit_stale_matches(
    settings, mongo_db, monkeypatch
):
    app_holder = []

    def handler(request):
        data = json.loads(request.content)["messages"][1]["content"]
        if json.loads(data).get("schema", {}).get("title") == "MatchAnswer":
            app_holder[0].state.synthetic_offer_search.replace(
                SyntheticCorpus(synthetic=True, version="withdrawn", offers=[])
            )
        return simulated_response(request)

    async with facade(settings, mongo_db, monkeypatch, handler=handler) as (client, app):
        app_holder.append(app)
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        response = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "expected_revision": 0,
                "message": "Find Python PFE Sfax",
            },
            headers={"Idempotency-Key": "changed-search-1"},
        )
        assert (
            response.status_code == 409
            and response.json()["detail"]["code"] == "search_generation_changed"
        )
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 0
        assert (await mongo_db.sid_conversations.find_one({"_id": conversation["id"]}))[
            "revision"
        ] == 0


@pytest.mark.mongodb
async def test_replay_does_not_return_withdrawn_matches(settings, mongo_db, monkeypatch):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        payload = {
            "conversation_id": conversation["id"],
            "expected_revision": 0,
            "message": "Find Python PFE Sfax",
        }
        headers = {"Idempotency-Key": "withdrawn-replay-1"}
        assert (await client.post("/agent/chat", json=payload, headers=headers)).status_code == 200
        app.state.synthetic_offer_search.replace(
            SyntheticCorpus(synthetic=True, version="empty", offers=[])
        )
        replay = await client.post("/agent/chat", json=payload, headers=headers)
        assert replay.status_code == 409
        assert replay.json()["detail"]["code"] == "search_generation_changed"
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 1


@pytest.mark.mongodb
@pytest.mark.parametrize(
    "evidence",
    [
        {"id": "not-returned", "quote": "fabricated"},
        {"id": "fixture-python-pfe", "quote": "fabricated"},
    ],
)
async def test_matching_rejects_invented_ids_and_quotes_without_committing(
    settings, mongo_db, monkeypatch, evidence
):
    def invalid(request):
        if request.url.host == "generativelanguage.googleapis.com":
            return httpx.Response(503)
        data = json.loads(json.loads(request.content)["messages"][1]["content"])
        if data.get("schema", {}).get("title") == "MatchAnswer":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "text": "Invented match",
                                        "evidence": [evidence],
                                    }
                                )
                            },
                        }
                    ]
                },
            )
        return simulated_response(request)

    async with facade(settings, mongo_db, monkeypatch, handler=invalid) as (client, app):
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        failed = await client.post(
            "/agent/chat",
            json={
                "conversation_id": conversation["id"],
                "expected_revision": 0,
                "message": "Find Python PFE Sfax",
            },
            headers={"Idempotency-Key": "invalid-match-1"},
        )
        assert failed.status_code == 503
        assert await mongo_db.sid_conversation_turns.count_documents({}) == 0
        head = await mongo_db.sid_conversations.find_one({"_id": conversation["id"]})
        assert head["revision"] == 0 and head["keywords"] == [] and "pending" not in head


@pytest.mark.mongodb
async def test_no_results_and_ambiguous_filters_have_explicit_statuses(
    settings, mongo_db, monkeypatch
):
    async with facade(settings, mongo_db, monkeypatch) as (client, app):
        conversation = (await client.post("/api/v1/conversations", json={"language": "en"})).json()
        payload = {
            "conversation_id": conversation["id"],
            "expected_revision": 0,
            "message": "Find Python PFE Sousse",
        }
        empty = await client.post(
            "/agent/chat", json=payload, headers={"Idempotency-Key": "no-matches-1"}
        )
        assert empty.status_code == 200 and empty.json()["response"]["status"] == "no_matches"
        assert empty.json()["response"]["generation"] is None
        ambiguous = await client.post(
            "/agent/chat",
            json={**payload, "expected_revision": 1, "message": "Find Python PFE in Sfax or Tunis"},
            headers={"Idempotency-Key": "ambiguous-1"},
        )
        assert ambiguous.json()["response"]["status"] == "clarification_required"
        assert ambiguous.json()["response"]["results"] == []
